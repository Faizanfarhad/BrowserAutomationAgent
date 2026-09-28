import argparse
import asyncio

from playwright.async_api import async_playwright

from config import *
import config
from Executor.action import Executor
from Planner.planner import Planner



async def run(task: str):
    """Run the complete planning, validation, execution, and replanning pipeline."""
    if not task or not task.strip():
        raise ValueError("Task cannot be empty")

    planner = Planner(mode=MODE, replanning=False)

    if MODE == "Api":
        provider = "deepseek"
        model_name = DEEPSEEK_MODEL
    else:
        provider = None
        model_name = MODEL_NAME

    print("############################ PLANNING ############################")
    plan = await planner.generate_plan(
        task=task,
        model_name=model_name,
        think="high",
        provider=provider,
    )

    if not isinstance(plan, dict):
        raise ValueError("Planner did not return a valid plan")

    print("############################ EXECUTION ############################")
    async with async_playwright() as playwright:
        await Executor(playwright, plan, original_task=task).do_task()


def main():
    global TASK
    parser = argparse.ArgumentParser(
        description="Run the browser automation planning pipeline"
    )
    parser.add_argument(
        "task",
        nargs="+",
        help="The browser task, for example: search Wikipedia for Python",
    )
    args = parser.parse_args()
    config.TASK = " ".join(args.task)
    asyncio.run(run(" ".join(args.task)))


if __name__ == "__main__":
    main()
