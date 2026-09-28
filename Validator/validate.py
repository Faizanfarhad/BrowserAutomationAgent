import json
import playwright
from typing import Optional 
from urllib.parse import urlparse
from playwright.async_api import Page

class Validater:
    VALIDATION_RULES = {
        "navigate": "URL is valid",
        "fill": "target exists + visible + enabled + editable",
        "click": "target exists + visible + enabled",
        "press": "target exists + visible + enabled + keyboard-capable",
        "select": "target exists + visible + enabled + is selectable",
        "wait": "duration/condition is valid",
        "extract": "target exists + readable",
    }

    def __init__(self,plan:Optional[str | dict],page:Page):
        super().__init__()
        self.plan = plan
        # Validation only reads the current DOM, so use the page supplied by the executor.
        self.page = page
        self.valid_info = {
            "valid": False,
            "table": self.validation_table(),
            "steps": [],
        }
        self.status = ["not found","found","ambiguous"]
        self.candidates = []

    @classmethod
    def validation_table(cls):
        """Return the validation rules in the format used by the planner."""
        rows = [
            "| Action | Validation |",
            "| ------ | ---------- |",
        ]
        for action, validation in cls.VALIDATION_RULES.items():
            rows.append(f"| `{action}` | {validation} |")
        return "\n".join(rows)
    
    # 
    async def resolve_target(self, page:Page, target:str, role:Optional[str] = None):
        """_summary_
            founds the target candidate based in different approaches ex:
            on name ,
            on placeholder ,
            on label ,
            on role

            if locator count greater then 0 just process it further stages (dom_candidate -> candidate_filtering)
        Args:
            page (playwright): _description_
            target (str): _description_

        Returns:
            _type_: _description_
        """
        self.candidates = []
        if role:
            self.candidates.append(page.get_by_role(role, name=target))

        self.candidates.extend([
            page.locator(f"[name='{target}']"),
            page.locator(f"#{target}"),
            page.get_by_placeholder(target),
            page.get_by_label(target),
            page.get_by_role("textbox", name=target),
        ])

        for locator in self.candidates:
            if await locator.count() > 0:
                return locator

        return None
    
    
    async def candidate_filetring(self,candidates,action):
        """_summary_
            filter the candidate based on the stauts of the candiadate ex:
                is candidate visible ?
                is candidate enabled ? 
                is candidate editable ? 

        Args:
            locator (_type_): _description_
        
        """
        filtered = []
        
        for candidate in candidates:
            if await candidate.count() != 1:
                continue
            if not await candidate.is_visible():
                continue
            if action != "extract" and not await candidate.is_enabled():
                continue
            if action == "fill" and not await candidate.is_editable():
                continue
            if action == "press" and not await self.is_keyboard_capable(candidate):
                continue
            if action == "select" and not await self.is_selectable(candidate):
                continue
            if action == "extract" and not await self.is_readable(candidate):
                continue

            filtered.append(candidate)

        if len(filtered) == 1:
            return filtered[0]
        return None

    async def is_keyboard_capable(self, locator):
        """Return whether an element can receive keyboard input."""
        return await locator.evaluate("""
            element => {
                const tag = element.tagName.toLowerCase();
                const role = element.getAttribute('role');
                return element.isContentEditable ||
                    ['input', 'textarea', 'select', 'button'].includes(tag) ||
                    ['textbox', 'combobox', 'listbox', 'button', 'option'].includes(role) ||
                    element.tabIndex >= 0;
            }
        """)

    async def is_selectable(self, locator):
        """Return whether an element represents a selectable control."""
        return await locator.evaluate("""
            element => {
                const tag = element.tagName.toLowerCase();
                const role = element.getAttribute('role');
                return tag === 'select' || ['combobox', 'listbox', 'option'].includes(role);
            }
        """)

    async def is_readable(self, locator):
        """Return whether an element has readable, non-hidden content."""
        if not await locator.is_visible():
            return False
        return await locator.evaluate("""
            element => element.getAttribute('aria-hidden') !== 'true' &&
                !element.hidden &&
                !element.hasAttribute('inert')
        """)

    async def dom_candidate(self,locator:playwright):
        """Return a locator only when it identifies one DOM element."""
        count = await locator.count()
        if count == 0:
            return None
        if count == 1:
            return locator
        return self.status[2]

    async def is_target_available(self,page:playwright,target:str,role:str,
                                  action:str = "fill"):
        """Check a target using the rule for the requested action."""
        locator = await self.resolve_target(page, target, role)
        if locator is None:
            return False

        candidate = await self.dom_candidate(locator)
        if candidate == self.status[2]:
            candidate = await self.candidate_filetring(self.candidates, action)
        if candidate is None:
            return False

        if not await candidate.is_visible():
            return False
        if action != "extract" and not await candidate.is_enabled():
            return False
        if action == "fill" and not await candidate.is_editable():
            return False
        if action == "press" and not await self.is_keyboard_capable(candidate):
            return False
        if action == "select" and not await self.is_selectable(candidate):
            return False
        if action == "extract" and not await self.is_readable(candidate):
            return False
        return True

    def is_url_valid(self,url) -> bool:
        """Return whether a URL has an HTTP or HTTPS scheme and a host."""
        if not isinstance(url, str) or not url.strip():
            return False

        parser = urlparse(url)
        return parser.scheme in ("http", "https") and bool(parser.netloc)

    async def validate_wait(self, step):
        """Validate a wait duration or a wait condition."""
        duration = step.get("duration")
        condition = step.get("condition")

        valid_duration = isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0
        valid_condition = isinstance(condition, str) and bool(condition.strip())
        return valid_duration or valid_condition

    async def validate_step(self, step):
        """Validate one step according to its action rule."""
        action = step.get("action")
        if action not in self.VALIDATION_RULES:
            return False

        if action == "navigate":
            return self.is_url_valid(step.get("url"))
        if action == "wait":
            return await self.validate_wait(step)

        target = step.get("target")
        if not isinstance(target, dict):
            return False

        name = target.get("name")
        role = target.get("role")
        if not isinstance(name, str) or not name.strip() or not isinstance(role, str) or not role.strip():
            return False

        return await self.is_target_available(self.page, name, role, action)

    def add_step_info(self, step_number, action,role, step_valid):
        self.valid_info["steps"].append({
            "step": step_number,
            "action": action,
            "role":role,
            "validation": self.VALIDATION_RULES.get(action, "unknown action"),
            "valid": step_valid,
        })

    async def validate(self):
        plan_valid = True
        self.valid_info["steps"] = []
        self.valid_info.pop("error", None)
        self.valid_info.pop("errors", None)

        try:
            plan = json.loads(self.plan) if isinstance(self.plan, str) else self.plan
            steps = plan["steps"]
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
            self.valid_info["valid"] = False
            self.valid_info["error"] = str(error)
            return

        if not isinstance(steps, list):
            self.valid_info["valid"] = False
            self.valid_info["error"] = "Plan steps must be a list"
            return

        for step_number, step in enumerate(steps, start=1):
            role = None
            if not isinstance(step, dict):
                step_valid = False
                action = "unknown"
            else:
                action = step.get("action", "unknown")
                target = step.get("target")
                if isinstance(target, dict):
                    role = target.get("role")
                try:
                    step_valid = await self.validate_step(step)
                except Exception as error:
                    step_valid = False
                    self.valid_info.setdefault("errors", []).append(
                        f"Step {step_number}: {error}"
                    )

            self.add_step_info(step_number, action,role, step_valid)
            plan_valid = plan_valid and step_valid

        self.valid_info["valid"] = plan_valid
    
