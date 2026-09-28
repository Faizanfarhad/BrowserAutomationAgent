import asyncio
import json
from typing import Optional
from playwright.async_api import Playwright, async_playwright, Page

from tools.dom_observation import find_relevant_element
from Validator.validate import Validater
from Planner.planner import Planner
from tools.load_json import dir_load_json
from config import *
from state.agent_state import save_agent_state



class Executor:
    def __init__(
        self,
        playwright: Playwright,
        planning: Optional[str | dict],
        original_task: Optional[str] = None,
    ):
        self.playwright = playwright
        self.planning = planning
        self.original_task = original_task or replanner_taks
        self.agent_state = {"task": self.original_task}
        self.replan_attempt  = 0
        self.validation_result = None

    def _persist_state(self):
        save_agent_state(self.agent_state)

    def _new_attempt_state(self, attempt_number, plan):
        steps = plan.get("steps", []) if isinstance(plan, dict) else []
        if not isinstance(steps, list):
            steps = []
        return {
            "attempt": attempt_number,
            "replan_attempt": self.replan_attempt,
            "task": self.original_task,
            "status": "validating",
            "plan": plan,
            "validation": None,
            "execution": {
                "current_step": None,
                "steps": [
                    {
                        "step": index,
                        "action": step.get("action") if isinstance(step, dict) else None,
                        "status": "pending",
                        "result": None,
                        "error": None,
                    }
                    for index, step in enumerate(steps, start=1)
                ],
            },
            "error": None,
        }

    def get_plan(self):
        """Return the plan as a dictionary whether input is JSON or already parsed."""
        if isinstance(self.planning, str):
            return json.loads(self.planning)
        return self.planning

    async def get_target_locator(self, validator:Validater, page, step):
        """Resolve the same target that the validator checked."""
        target = step["target"]
        locator = await validator.resolve_target(
            page,
            target["name"],
            target["role"],
        )

        if locator is None:
            return None

        candidate = await validator.dom_candidate(locator)
        if candidate == validator.status[2]:
            candidate = await validator.candidate_filetring(
                validator.candidates,
                step["action"],
            )
        return candidate


    async def do_task(self):
        self.agent_state = {"task": self.original_task}
        self._persist_state()
        try:
            plan = self.get_plan()
        except Exception as error:
            attempt_state = self._new_attempt_state(1, self.planning)
            attempt_state.update({
                "status": "failed",
                "failure_stage": "planning",
                "error": str(error),
            })
            self.agent_state["Attempt1"] = attempt_state
            self._persist_state()
            raise

        self.replan_attempt = 0

        while True:
            attempt_number = self.replan_attempt + 1
            attempt_key = f"Attempt{attempt_number}"
            attempt_state = self._new_attempt_state(attempt_number, plan)
            self.agent_state[attempt_key] = attempt_state
            self._persist_state()

            print(
                f"############################ ATTEMPT {attempt_number} "
                f"############################"
            )
            try:
                self.validation_result = await self.validate_plan(plan)
                attempt_state["validation"] = self.validation_result.get("valid_info")
                self._persist_state()

                if self.validation_result["valid"]:
                    attempt_state["status"] = "executing"
                    self._persist_state()
                    await self.execute_valid_plan(
                        plan,
                        self.validation_result["storage_state"],
                        attempt_state,
                    )
                    attempt_state["status"] = "succeeded"
                    attempt_state["execution"]["current_step"] = None
                    self._persist_state()
                    print("Plan executed successfully.")
                    return

                attempt_state["status"] = "validation_failed"
                attempt_state["replan_data"] = self.validation_result.get("replan_data")
                self._persist_state()

                if self.replan_attempt >= MAX_REPLAN_ATTEMPTS:
                    raise RuntimeError(
                        f"Plan is still invalid after {MAX_REPLAN_ATTEMPTS} replans"
                    )

                next_replan = self.replan_attempt + 1
                attempt_state["status"] = "replanning"
                self._persist_state()
                print(
                    f"Validation failed. Creating replan {next_replan} "
                    f"of {MAX_REPLAN_ATTEMPTS}."
                )
                new_plan = await self.create_replan(plan, self.validation_result)
                attempt_state["replan"] = {
                    "status": "succeeded",
                    "next_attempt": attempt_number + 1,
                    "plan": new_plan,
                }
                attempt_state["status"] = "replanned"
                self._persist_state()
                plan = new_plan
                self.planning = new_plan
                self.replan_attempt = next_replan
            except Exception as error:
                if attempt_state["status"] != "failed":
                    previous_status = attempt_state["status"]
                    attempt_state["status"] = "failed"
                    attempt_state["failure_stage"] = {
                        "validating": "validation",
                        "validation_failed": "validation",
                        "replanning": "replanning",
                        "executing": "execution",
                    }.get(previous_status, previous_status)
                    attempt_state["error"] = str(error)
                self._persist_state()
                raise

    async def validate_plan(self, plan):
        """Validate one complete plan and return its validation result."""
        validation_browser = await self.playwright.chromium.launch(headless=True)
        try:
            validation_context = await validation_browser.new_context()
            try:
                validation_page = await validation_context.new_page()
                validator = Validater(plan, validation_page)

                start_url = next(
                    (
                        step.get("url")
                        for step in plan.get("steps", [])
                        if step.get("action") == "navigate"
                    ),
                    None,
                )
                if start_url and validator.is_url_valid(start_url):
                    await validation_page.goto(start_url)

                await validator.validate()
                valid_info = validator.valid_info

                print("#" * 20, "Validating the plan", "#" * 20)
                print(f"Is valid: {valid_info['valid']}")
                print(f"Validation information:\n{valid_info['table']}")
                print("Validation results:")
                print(valid_info)

                if not valid_info["valid"]:
                    replan_data = await self.collect_replan_data(
                        plan,
                        valid_info,
                        validation_page,
                    )
                    return {
                        "valid": False,
                        "valid_info": valid_info,
                        "replan_data": replan_data,
                    }

                storage_state = await validation_context.storage_state()
                return {
                    "valid": True,
                    "valid_info": valid_info,
                    "storage_state": storage_state,
                }
            finally:
                await validation_context.close()
        finally:
            await validation_browser.close()

    async def execute_valid_plan(self, plan, storage_state, attempt_state):
        """Execute a plan after it passes complete validation."""
        execution_browser = await self.playwright.chromium.launch(headless=False)
        try:
            execution_context = await execution_browser.new_context(
                storage_state=storage_state
            )
            try:
                execution_page = await execution_context.new_page()
                execution_validator = Validater(plan, execution_page)

                await self.execute_plan(
                    plan,
                    execution_page,
                    execution_validator,
                    attempt_state,
                )
            finally:
                await execution_context.close()
        finally:
            await execution_browser.close()

    async def execute_plan(self, plan, page, validator, attempt_state):
        """Execute the plan only after the complete plan is valid."""

        for index, step in enumerate(plan["steps"]):
            action = step["action"]
            step_state = attempt_state["execution"]["steps"][index]
            attempt_state["execution"]["current_step"] = index + 1
            step_state["status"] = "running"
            self._persist_state()

            try:
                result = None
                if action == "navigate":
                    url = step["url"]
                    print(f"Navigating to {url}")
                    await page.goto(url)
                    await page.wait_for_timeout(1000)
                    await page.screenshot(path="traces/opened.png")

                elif action in {"fill", "click", "press", "select", "extract"}:
                    locator = await self.get_target_locator(validator, page, step)
                    if locator is None:
                        raise ValueError("The validated target could not be resolved")

                    if action == "fill":
                        await locator.fill(step["value"])
                    elif action == "click":
                        await locator.click()
                    elif action == "press":
                        await locator.press(step["key"])
                    elif action == "select":
                        await locator.select_option(step["value"])
                    elif action == "extract":
                        try:
                            result = await locator.input_value()
                        except Exception:
                            result = await locator.inner_text()
                        print(f"Extracted value: {result}")

                    await page.screenshot(path="traces/action.png")

                elif action == "wait":
                    if "duration" not in step:
                        raise NotImplementedError(
                            "Condition-based waits are not implemented yet"
                        )
                    await page.wait_for_timeout(step["duration"])

                step_state["status"] = "succeeded"
                step_state["result"] = result
                attempt_state["execution"]["current_step"] = None
                self._persist_state()
            except Exception as error:
                step_state["status"] = "failed"
                step_state["error"] = str(error)
                self._persist_state()
                raise

    async def collect_replan_data(self, plan, valid_info, page):
        """Collect the information needed to create a corrected plan."""
        invalid_steps = {}

        for index, step_result in enumerate(valid_info.get("steps", []), start=1):
            step_name = f"step{index}"
            if not step_result["valid"]:
                original_step = plan["steps"][index - 1]
                invalid_steps[step_name] = {
                    "plan_step": original_step,
                    "validation": step_result,
                }

        current_url = page.url

        dom_observations = {}
        if current_url:
            for step_name, step_result in invalid_steps.items():
                role = step_result["validation"].get("role")
                if role:
                    dom_observations[f"{step_name}_{role}"] = (
                        await find_relevant_element(self.playwright, current_url, role)
                    )

        return {
            "executed_steps": {},
            "failed_step": invalid_steps,
            "current_url": current_url,
            "dom_observations": dom_observations,
        }

    async def create_replan(self, plan, validation_result):
        """Ask the planner for a corrected plan."""
        replan_data = validation_result["replan_data"]
        planner = Planner(
            mode=MODE,
            replanning=True,
        )

        if MODE == "Api":
            model_name = DEEPSEEK_MODEL
            provider = "deepseek"
        else:
            model_name = MODEL_NAME
            provider = None

        new_plan = await planner.generate_plan(
            task=self.original_task,
            model_name=model_name,
            think="high",
            provider=provider,
            previous_plan=plan,
            executed_steps=replan_data["executed_steps"],
            failed_step=replan_data["failed_step"],
            validation_info=validation_result["valid_info"],
            current_url=replan_data["current_url"],
            dom_observations=replan_data["dom_observations"],
        )

        if not isinstance(new_plan, dict) or not isinstance(new_plan.get("steps"), list):
            raise ValueError("Replanner did not return a valid plan")

        return new_plan

async def main(planning):
    async with async_playwright() as playwright:
        await Executor(playwright, planning).do_task()


if __name__ == "__main__":
    path = PLAN_PATH
    plan = dir_load_json(path)
    asyncio.run(main(plan))
