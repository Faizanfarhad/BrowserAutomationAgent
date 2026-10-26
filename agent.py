import argparse
import asyncio
import shutil
import subprocess
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

from config import *
import config
from Executor.action import Executor
from Planner.planner import Planner



async def run(task: str):
    """Run the complete planning, validation, execution, and replanning pipeline."""
    if not task or not task.strip():
        raise ValueError("Task cannot be empty")

    planner = Planner(mode=config.MODE, replanning=False)

    if config.MODE == "Api":
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


async def setup_chrome_login(login_url: str | None = None):
    """Start regular Chrome for manual sign-in, ready for a later CDP attach."""
    endpoint = config.CHROME_CDP_ENDPOINT.rstrip("/")
    try:
        with urllib.request.urlopen(f"{endpoint}/json/version", timeout=1):
            chrome_is_running = True
    except Exception:
        chrome_is_running = False

    if not chrome_is_running:
        executable = shutil.which(config.CHROME_EXECUTABLE)
        if not executable:
            raise RuntimeError(
                f"Chrome executable not found: {config.CHROME_EXECUTABLE}"
            )

        profile_dir = Path(config.CHROME_PROFILE_DIR).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        command = [
            executable,
            f"--remote-debugging-port={config.PORT}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        command.append(login_url or "about:blank")
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        for _ in range(40):
            try:
                with urllib.request.urlopen(f"{endpoint}/json/version", timeout=1):
                    chrome_is_running = True
                break
            except Exception:
                await asyncio.sleep(0.25)
        else:
            raise RuntimeError(
                f"Chrome did not start its remote debugging endpoint at {endpoint}. "
                "Close other Chrome windows using the agent profile and try again."
            )
    elif login_url:
        print(
            "Chrome is already listening for the agent. Open the requested login "
            f"page manually in that Chrome window: {login_url}"
        )

    print(
        "Sign in manually in the regular Chrome window. Do not enter credentials "
        "into the terminal. Leave Chrome open; the agent will attach to it at "
        f"{endpoint}."
    )
    await asyncio.to_thread(input, "Press Enter when sign-in is complete... ")


def main():
    global TASK
    parser = argparse.ArgumentParser(
        description="Run the browser automation planning pipeline"
    )
    parser.add_argument(
        "task",
        nargs="*",
        help="The browser task, for example: search Wikipedia for Python",
    )
    parser.add_argument("-m","--mode",type=str,default="Local",help="mode of the planner")
    parser.add_argument(
        "--persistent-chrome",
        action="store_true",
        help="Use the saved Chrome profile in config.CHROME_PROFILE_DIR",
    )
    parser.add_argument(
        "--setup-chrome-login",
        action="store_true",
        help="Open the persistent Chrome profile for manual sign-in, then exit",
    )
    parser.add_argument(
        "--login-url",
        type=str,
        help="Optional sign-in page to open with --setup-chrome-login",
    )
    parser.add_argument(
        "--setup-gmail",
        action="store_true",
        help="Authorize the read-only Gmail API and save its local token",
    )
    args = parser.parse_args()

    if args.setup_chrome_login:
        asyncio.run(setup_chrome_login(args.login_url))
        return
    if args.setup_gmail:
        from integrations.gmail import authorize_gmail

        authorize_gmail()
        return
    if not args.task:
        parser.error(
            "task is required unless --setup-chrome-login or --setup-gmail is used"
        )

    if args.persistent_chrome:
        config.PERSISTENT_CHROME = True
    
    config.TASK = " ".join(args.task)
    config.MODE = "".join(str.capitalize(args.mode))
    
    asyncio.run(run(" ".join(args.task)))


if __name__ == "__main__":
    main()
