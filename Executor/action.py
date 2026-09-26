import asyncio
import json
from typing import Optional
from playwright.async_api import Playwright, async_playwright, Page

from tools.dom_observation import find_relevant_element
from Validator.validate import Validater
from Planner.planner import Planner
from tools.load_json import dir_load_json
from config import *


class Executor:
    def __init__(self, playwright: Playwright, planning: Optional[str | dict]):
        self.playwright = playwright
        self.planning = planning
        

    def get_plan(self):
        """Return the plan as a dictionary whether input is JSON or already parsed."""
        if isinstance(self.planning, str):
            return json.loads(self.planning)
        return self.planning

    async def get_target_locator(self, validator, page, step):
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
        plan = self.get_plan()
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
                    print("Validation failed; sending the plan to the replanner.")
                    await self.replan(plan, valid_info)
                    return

                storage_state = await validation_context.storage_state()
            finally:
                await validation_context.close()
        finally:
            await validation_browser.close()

        execution_browser = await self.playwright.chromium.launch(headless=False)
        try:
            execution_context = await execution_browser.new_context(
                storage_state=storage_state
            )
            try:
                execution_page = await execution_context.new_page()
                execution_validator = Validater(plan, execution_page)
                await self.execute_plan(plan, execution_page, execution_validator)
            finally:
                await execution_context.close()
        finally:
            await execution_browser.close()

    async def execute_plan(self, plan, page, validator):
        """Execute the plan only after the complete plan is valid."""
        for step in plan["steps"]:
            action = step["action"]

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
                        extracted_value = await locator.input_value()
                    except Exception:
                        extracted_value = await locator.inner_text()
                    print(f"Extracted value: {extracted_value}")

                await page.screenshot(path="traces/action.png")

            elif action == "wait":
                if "duration" not in step:
                    raise NotImplementedError(
                        "Condition-based waits are not implemented yet"
                    )
                await page.wait_for_timeout(step["duration"])

    async def replan(self, plan, valid_info):
        """Send invalid-plan information and DOM observations to the replanner."""
        invalid_steps = {}
        valid_steps = {}

        for index, step_result in enumerate(valid_info.get("steps", []), start=1):
            step_name = f"step{index}"
            if step_result["valid"]:
                valid_steps[step_name] = step_result
            else:
                invalid_steps[step_name] = step_result

        current_url = ""
        for step in plan["steps"]:
            if step.get("action") == "navigate":
                current_url = step.get("url", "")
                break

        dom_observations = {}
        if current_url:
            for step_name, step_result in invalid_steps.items():
                role = step_result.get("role")
                if role:
                    dom_observations[f"{step_name}_{role}"] = (
                        await find_relevant_element(self.playwright,current_url, role)
                    )

        planner = Planner(
            mode=MODE,
            replanning=True,
        )

        print("############################prev plan############################")
        print(plan,end="\n")
        print("############################valid steps############################")
        print(valid_steps,end="\n")
        print("############################invalid steps############################")
        print(invalid_steps,end="\n")
        print("############################Valid information############################")
        print(valid_info,end="\n")
        print("############################current url############################")
        print(current_url)
        print("############################dom observation############################")
        print(dom_observations)
        
        await planner.generate_plan(
            task=replanner_taks,
            model_name=DEEPSEEK_MODEL,
            think="high",
            provider="deepseek",
            previous_plan=plan,
            executed_steps=valid_steps,
            failed_step=invalid_steps,
            validation_info=valid_info,
            current_url=current_url,
            dom_observations=dom_observations,
        )


async def main(planning):
    async with async_playwright() as playwright:
        await Executor(playwright, planning).do_task()


if __name__ == "__main__":
    path = "Planner/saved_plan/plan0.json"
    plan = dir_load_json(path)
    asyncio.run(main(plan))
