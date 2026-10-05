import asyncio
import json
import re
import config
from config import * 
from typing import Optional
from urllib.parse import unquote, urlencode, urlparse
from playwright.async_api import Playwright, async_playwright, Page, TimeoutError as PlaywrightTimeoutError

from Planner.planner import Planner
from Validator.validate import Validater
from state.agent_state import save_agent_state
from config import *

from tools.dom_observation import find_relevant_element
from tools.load_json import dir_load_json
from tools.download import download_url
from tools.extractor import extract_categorized
from tools.api_client import execute_api_request


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
        self.failed_downloads = []
        self.product_search_query = None
        self.execution_browser = None
        self.execution_context = None
        self.execution_page = None
        self.execution_context_owned = False

    def _persist_state(self):
        save_agent_state(self.agent_state)

    async def _get_persistent_chrome_context(self):
        if self.execution_context is None:
            try:
                self.execution_browser = await self.playwright.chromium.connect_over_cdp(
                    config.CHROME_CDP_ENDPOINT
                )
            except Exception as error:
                raise RuntimeError(
                    "Could not connect to Chrome. Start Chrome for the agent profile with "
                    "--setup-chrome-login before using --persistent-chrome."
                ) from error
            if not self.execution_browser.contexts:
                await self.execution_browser.close()
                self.execution_browser = None
                raise RuntimeError("Connected Chrome has no available browser context")
            self.execution_context = self.execution_browser.contexts[0]
            self.execution_context_owned = False
        return self.execution_context

    @staticmethod
    async def _capture_screenshot(page, path):
        try:
            await page.screenshot(path=path)
        except Exception as error:
            print(f"Screenshot unavailable for {path}: {error}")

    @staticmethod
    async def _press_key(page, locator, step):
        target = step.get("target", {})
        key = step["key"]
        if key.lower() == "enter" and isinstance(target, dict) and target.get("role") == "searchbox":
            previous_url = page.url
            try:
                submitted = await locator.evaluate(
                    "element => { if (!element.form) return false; element.form.requestSubmit(); return true; }"
                )
            except PlaywrightTimeoutError:
                submitted = False

            if not submitted:
                await locator.press(key, no_wait_after=True)

            try:
                await page.wait_for_url(
                    lambda url: str(url) != previous_url,
                    timeout=5000,
                )
            except PlaywrightTimeoutError:
                pass
            return

        await locator.press(key, no_wait_after=True)

    @staticmethod
    def _host(url):
        if not isinstance(url, str):
            return ""
        return (urlparse(url or "").hostname or "").removeprefix("www.")

    @staticmethod
    def _target_is_current_page(page, target):
        """Return whether a page URL already represents a requested article link."""
        if not isinstance(target, dict) or not isinstance(target.get("name"), str):
            return False

        page_slug = unquote(urlparse(page.url).path.rstrip("/").rsplit("/", 1)[-1])
        normalize = lambda value: re.sub(r"[^a-z0-9]", "", value.lower())
        return bool(page_slug) and normalize(page_slug) == normalize(target["name"])

    @staticmethod
    def _extracted_value(attempt_state, step_number, field, expected_category=None):
        plan_steps = attempt_state.get("plan", {}).get("steps", [])
        execution_steps = attempt_state.get("execution", {}).get("steps", [])
        if step_number > len(plan_steps) or step_number > len(execution_steps):
            return None

        plan_step = plan_steps[step_number - 1]
        execution_step = execution_steps[step_number - 1]
        if (
            not isinstance(plan_step, dict)
            or plan_step.get("action") != "extract"
            or field not in plan_step.get("fields", [])
            or execution_step.get("status") != "succeeded"
        ):
            return None
        if expected_category and plan_step.get("category") != expected_category:
            return None

        result = execution_step.get("result") or {}
        data = result.get("data") if isinstance(result, dict) else None
        return data.get(field) if isinstance(data, dict) else None

    def _resolve_step_reference(self, value, attempt_state):
        """Resolve a {from: stepN.data.field} reference from completed extraction."""
        if not isinstance(value, dict):
            return value

        reference = value.get("from")
        match = re.fullmatch(
            r"step([1-9]\d*)\.data\.([A-Za-z_][A-Za-z0-9_]*)",
            reference if isinstance(reference, str) else "",
        )
        if not match or set(value) != {"from"}:
            raise ValueError(f"Invalid step reference: {value}")

        source_number = int(match.group(1))
        field = match.group(2)
        current_plan = attempt_state.get("plan", {}).get("steps", [])
        expected_category = None
        if source_number <= len(current_plan):
            source_plan_step = current_plan[source_number - 1]
            if isinstance(source_plan_step, dict):
                expected_category = source_plan_step.get("category")

        resolved = self._extracted_value(
            attempt_state,
            source_number,
            field,
            expected_category,
        )
        if resolved is None:
            prior_attempts = sorted(
                (
                    state for key, state in self.agent_state.items()
                    if key.startswith("Attempt") and state is not attempt_state
                ),
                key=lambda state: state.get("attempt", 0),
                reverse=True,
            )
            resolved = next(
                (
                    previous_value
                    for previous in prior_attempts
                    if (previous_value := self._extracted_value(
                        previous,
                        source_number,
                        field,
                        expected_category,
                    )) is not None
                ),
                None,
            )
        if not isinstance(resolved, str) or not resolved.strip():
            raise ValueError(
                f"step{source_number}.data.{field} has no successful extraction value"
            )
        return resolved.strip()

    def _materialize_prior_references(self, plan, previous_attempt):
        """Resolve replan URLs that point to extraction results from an earlier plan."""
        steps = plan.get("steps", []) if isinstance(plan, dict) else []
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict) or step.get("action") not in {
                "navigate", "download", "api_request"
            }:
                continue
            reference = step.get("url")
            if not isinstance(reference, dict):
                continue

            match = re.fullmatch(
                r"step([1-9]\d*)\.data\.([A-Za-z_][A-Za-z0-9_]*)",
                reference.get("from", "") if isinstance(reference.get("from"), str) else "",
            )
            if not match or set(reference) != {"from"}:
                continue

            source_index = int(match.group(1))
            field = match.group(2)
            if source_index < index and source_index <= len(steps):
                new_source = steps[source_index - 1]
                if (
                    isinstance(new_source, dict)
                    and new_source.get("action") == "extract"
                    and field in new_source.get("fields", [])
                ):
                    continue

            prior_value = self._extracted_value(previous_attempt, source_index, field)
            if isinstance(prior_value, str) and prior_value.strip():
                step["url"] = prior_value.strip()
        return plan

    async def _observe_extraction_page(self, page):
        """Capture concise page evidence without reading form values."""
        return await page.evaluate("""() => {
            const root = document.querySelector('main, [role="main"], article') || document.body;
            const links = Array.from(root.querySelectorAll('a[href]'))
                .map(link => ({
                    text: (link.innerText || '').trim().slice(0, 200),
                    href: link.href,
                }))
                .filter(link => link.text)
                .slice(0, 30);
            return {
                url: location.href,
                title: document.title,
                headings: Array.from(document.querySelectorAll('h1, h2, h3'))
                    .map(heading => (heading.innerText || '').trim())
                    .filter(Boolean)
                    .slice(0, 20),
                text_excerpt: (root.innerText || '').trim().slice(0, 5000),
                links,
            };
        }""")

    def _task_requires_download(self):
        return re.search(r"\bdownload(?:s|ed|ing)?\b", self.original_task, re.IGNORECASE) is not None

    @staticmethod
    def _plan_requires_browser(plan):
        """Return whether any step needs a Playwright page."""
        return any(
            not isinstance(step, dict)
            or step.get("action") not in {
                "api_request", "gmail_search", "gmail_send"
            }
            for step in plan.get("steps", [])
        )

    def _route_product_search(self, plan, fallback_attempt=0):
        """Route India-market product lookups through local retailer/search sources."""
        steps = plan.get("steps", []) if isinstance(plan, dict) else []
        extract_steps = [
            step
            for step in steps
            if isinstance(step, dict)
            and step.get("action") == "extract"
            and "product" in str(step.get("category", "")).lower()
        ]
        if config.MARKET != "IN" or not extract_steps:
            return plan

        query = (
            self.product_search_query
            or extract_steps[0].get("query")
            or self.original_task
        )
        self.product_search_query = query
        for step in extract_steps:
            step["query"] = query
        search_query = f"site:flipkart.com {query} price INR"
        search_urls = [
            f"https://www.flipkart.com/search?{urlencode({'q': query})}",
            f"https://html.duckduckgo.com/html/?{urlencode({'q': search_query, 'kl': 'in-en'})}",
            f"https://www.bing.com/search?{urlencode({'q': search_query, 'setmkt': 'en-IN', 'setlang': 'en-IN'})}",
            f"https://search.brave.com/search?{urlencode({'q': search_query, 'country': 'IN', 'source': 'web'})}",
        ]
        if fallback_attempt >= len(search_urls):
            raise RuntimeError("No more configured product search sources remain")

        return {
            "steps": [
                {"action": "navigate", "url": search_urls[fallback_attempt]},
                *[step for step in steps if step.get("action") != "navigate"],
            ]
        }

    def _prepare_extraction_replan(
        self,
        plan,
        failure,
        current_url,
        fallback_attempt,
        page_observation=None,
    ):
        """Default to the current page, but allow evidence-based source changes."""
        current_host = self._host(current_url)
        navigate_steps = [
            step for step in plan["steps"]
            if isinstance(step, dict) and step.get("action") == "navigate"
        ]

        if failure.get("failure_type") == "blocked":
            category = failure.get("plan_step", {}).get("category", "")
            if "product" in str(category).lower():
                plan = self._route_product_search(plan, fallback_attempt)
                navigate_steps = [
                    step for step in plan["steps"]
                    if isinstance(step, dict) and step.get("action") == "navigate"
                ]
            new_hosts = {self._host(step.get("url")) for step in navigate_steps}
            if current_host and not any(
                host and host != current_host for host in new_hosts
            ):
                raise ValueError(
                    "Blocked extraction must be replanned on a different host"
                )
            return plan

        failed_plan_step = failure.get("plan_step", {})
        failed_result = failure.get("result") or {}
        missing_fields = {
            str(field).lower()
            for field in failed_result.get("missing_fields", [])
        }
        observed_links = (
            page_observation.get("links", [])
            if isinstance(page_observation, dict)
            else []
        )
        download_page = next(
            (
                link
                for link in observed_links
                if isinstance(link, dict)
                and isinstance(link.get("href"), str)
                and re.search(
                    r"download.{0,40}(?:documentation|docs)|(?:documentation|docs).{0,40}download",
                    link.get("text", ""),
                    re.IGNORECASE,
                )
            ),
            None,
        )
        if "resource_url" in missing_fields and download_page:
            resource_step = dict(failed_plan_step)
            resource_step["fields"] = failed_plan_step.get(
                "fields",
                ["resource_name", "resource_url"],
            )
            resource_url_field = next(
                (
                    field
                    for field in resource_step["fields"]
                    if str(field).lower() in {"resource_url", "download_url"}
                ),
                None,
            )
            retry_steps = [
                {"action": "navigate", "url": download_page["href"]},
                resource_step,
            ]
            if self._task_requires_download() and resource_url_field:
                retry_steps.append({
                    "action": "download",
                    "url": {"from": f"step2.data.{resource_url_field}"},
                })
            return {"steps": retry_steps}

        if current_url:
            if not navigate_steps:
                plan["steps"].insert(0, {"action": "navigate", "url": current_url})
        return plan

    @staticmethod
    def _plan_has_download(plan):
        return isinstance(plan, dict) and any(
            isinstance(step, dict) and step.get("action") == "download"
            for step in plan.get("steps", [])
        )

    def _require_download_action(self, plan, validation_result):
        if not self._task_requires_download():
            return

        download_steps = [
            step
            for step in plan.get("steps", [])
            if isinstance(step, dict) and step.get("action") == "download"
        ]
        failed_urls = {
            failed_url
            for failure in self.failed_downloads
            if isinstance(
                failed_url := (
                    failure.get("resolved_url")
                    or failure.get("plan_step", {}).get("url")
                ),
                str,
            )
        }
        reused_url = next(
            (
                url
                for step in download_steps
                if isinstance((url := step.get("url")), str)
                and url in failed_urls
            ),
            None,
        )
        if download_steps and reused_url is None:
            return

        current_url = next(
            (
                step.get("url")
                for step in plan.get("steps", [])
                if isinstance(step, dict) and step.get("action") == "navigate"
            ),
            None,
        )
        validation_result["valid"] = False
        valid_info = validation_result.setdefault("valid_info", {})
        valid_info["valid"] = False
        valid_info.setdefault("errors", []).append(
            (
                f"Download URL already failed and must not be retried: {reused_url}"
                if reused_url
                else "The task requests a download, but the plan has no download action."
            )
        )
        validation_result.setdefault(
            "replan_data",
            {
                "executed_steps": {},
                "failed_step": {
                    f"download{index + 1}": failure
                    for index, failure in enumerate(self.failed_downloads)
                },
                "current_url": current_url or "about:blank",
                "dom_observations": {},
            },
        )

    @staticmethod
    def _execution_failure_replan_data(plan, attempt_state, error):
        execution_steps = attempt_state["execution"]["steps"]
        failed_index = next(
            (
                index
                for index, step_state in enumerate(execution_steps)
                if step_state["status"] == "failed"
                and step_state["action"] in {
                    "download", "api_request", "gmail_search", "extract", "click", "fill", "press", "select"
                }
            ),
            None,
        )
        if failed_index is None:
            return None

        executed_steps = {
            f"step{index + 1}": {
                "plan_step": plan["steps"][index],
                "result": step_state["result"],
            }
            for index, step_state in enumerate(execution_steps)
            if step_state["status"] == "succeeded"
        }
        failed_step = {
            f"step{failed_index + 1}": {
                "plan_step": plan["steps"][failed_index],
                "error": str(error),
                "result": execution_steps[failed_index].get("result"),
                "resolved_url": execution_steps[failed_index].get("resolved_url"),
                "failure_type": (
                    "blocked"
                    if (execution_steps[failed_index].get("result") or {}).get("blocked")
                    else "incomplete"
                    if (execution_steps[failed_index].get("result") or {}).get("missing_fields")
                    else "extraction_error"
                ),
            }
        }
        observation = execution_steps[failed_index].get("observation")
        return {
            "failed_action": execution_steps[failed_index]["action"],
            "executed_steps": executed_steps,
            "failed_step": failed_step,
            "current_url": (
                attempt_state["execution"].get("current_url")
                or execution_steps[failed_index].get("resolved_url")
                or plan["steps"][failed_index].get("url")
                or "about:blank"
            ),
            "dom_observations": (
                {"extraction_page": observation} if observation else {}
            ),
        }

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
                        "observation": None,
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
        if candidate is not None and not await validator.is_target_available(
            page,
            target["name"],
            target["role"],
            step["action"],
        ):
            return None
        return candidate


    async def do_task(self):
        try:
            await self._do_task()
        finally:
            await self._close_execution_session()

    async def _close_execution_session(self):
        try:
            if self.execution_context and self.execution_context_owned:
                await self.execution_context.close()
        finally:
            if self.execution_browser:
                await self.execution_browser.close()
            self.execution_context = None
            self.execution_browser = None
            self.execution_page = None
            self.execution_context_owned = False

    async def _do_task(self):
        self.agent_state = {"task": self.original_task}
        self.failed_downloads = []
        self._persist_state()
        try:
            plan = self.get_plan()
            plan = self._route_product_search(plan)
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
                self._require_download_action(plan, self.validation_result)
                attempt_state["validation"] = self.validation_result.get("valid_info")
                self._persist_state()

                if self.validation_result["valid"]:
                    attempt_state["status"] = "executing"
                    self._persist_state()
                    try:
                        await self.execute_valid_plan(
                            plan,
                            self.validation_result["storage_state"],
                            attempt_state,
                        )
                    except Exception as error:
                        replan_data = self._execution_failure_replan_data(
                            plan,
                            attempt_state,
                            error,
                        )
                        if (
                            replan_data is None
                            or self.replan_attempt >= MAX_REPLAN_ATTEMPTS
                        ):
                            raise

                        if replan_data["failed_action"] == "download":
                            for failure in replan_data["failed_step"].values():
                                if failure not in self.failed_downloads:
                                    self.failed_downloads.append(failure)

                        attempt_state["status"] = "replanning"
                        attempt_state["error"] = str(error)
                        attempt_state["failure_stage"] = replan_data["failed_action"]
                        self._persist_state()
                        failure_label = replan_data["failed_action"].capitalize()
                        replan_context = {
                            "valid_info": {
                                "valid": False,
                                "errors": [f"{failure_label} failed: {error}"],
                            },
                            "replan_data": replan_data,
                        }
                        new_plan = await self.create_replan(plan, replan_context)
                        new_plan = self._materialize_prior_references(
                            new_plan,
                            attempt_state,
                        )
                        if replan_data["failed_action"] == "extract":
                            failure = next(iter(replan_data["failed_step"].values()))
                            new_plan = self._prepare_extraction_replan(
                                new_plan,
                                failure,
                                replan_data.get("current_url") or "",
                                fallback_attempt=self.replan_attempt + 1,
                                page_observation=replan_data.get(
                                    "dom_observations", {}
                                ).get("extraction_page"),
                            )
                        elif replan_data["failed_action"] in {
                            "click", "fill", "press", "select"
                        }:
                            current_url = replan_data.get("current_url") or ""
                            if current_url and not any(
                                isinstance(step, dict)
                                and step.get("action") == "navigate"
                                for step in new_plan["steps"]
                            ):
                                new_plan["steps"].insert(
                                    0,
                                    {"action": "navigate", "url": current_url},
                                )
                        attempt_state["replan"] = {
                            "status": "succeeded",
                            "next_attempt": attempt_number + 1,
                            "plan": new_plan,
                        }
                        attempt_state["status"] = "replanned"
                        self._persist_state()
                        plan = new_plan
                        self.planning = new_plan
                        self.replan_attempt += 1
                        continue

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
        validation_browser = None
        validation_context = None
        validation_page = None
        requires_browser = self._plan_requires_browser(plan)
        try:
            if requires_browser:
                if config.PERSISTENT_CHROME:
                    validation_context = await self._get_persistent_chrome_context()
                else:
                    validation_browser = await self.playwright.chromium.launch(
                        headless=config.HEADLESS
                    )
                    validation_context = await validation_browser.new_context()
                validation_page = await validation_context.new_page()

            validator = Validater(plan, validation_page)

            start_url = next(
                (
                    step.get("url")
                    for step in plan.get("steps", [])
                    if isinstance(step, dict)
                    and step.get("action") == "navigate"
                    and isinstance(step.get("url"), str)
                    and validator.is_url_valid(step.get("url"))
                ),
                None,
            )
            if validation_page and start_url and validator.is_url_valid(start_url):
                await validation_page.goto(start_url)
            await validator.validate()
            valid_info = validator.valid_info

            print("#" * 20, "Validating the plan", "#" * 20,end="\n")
            print(f"Is valid: {valid_info['valid']}",end="\n")
            print(f"Validation information:\n{valid_info['table']}",end="\n")
            print("Validation results:",end="\n")
            print(json.dumps(valid_info, indent=2, ensure_ascii=False))

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

            storage_state = (
                await validation_context.storage_state()
                if validation_context is not None
                else None
            )
            return {
                "valid": True,
                "valid_info": valid_info,
                "storage_state": storage_state,
            }
        finally:
            if validation_page and config.PERSISTENT_CHROME:
                if validation_page and not validation_page.is_closed():
                    await validation_page.close()
            elif validation_context is not None:
                await validation_context.close()
                if validation_browser is not None:
                    await validation_browser.close()

    async def execute_valid_plan(self, plan, storage_state, attempt_state):
        """Execute a validated plan in the task's persistent browser context."""
        if not self._plan_requires_browser(plan):
            await self.execute_plan(plan, None, Validater(plan, None), attempt_state)
            return

        if self.execution_page is None or self.execution_page.is_closed():
            if config.PERSISTENT_CHROME:
                await self._get_persistent_chrome_context()
            else:
                self.execution_browser = await self.playwright.chromium.launch(
                    headless=config.HEADLESS
                )
                self.execution_context = await self.execution_browser.new_context(
                    storage_state=storage_state
                )
                self.execution_context_owned = True
            self.execution_page = await self.execution_context.new_page()

        execution_validator = Validater(plan, self.execution_page)
        await self.execute_plan(
            plan,
            self.execution_page,
            execution_validator,
            attempt_state,
        )

    async def execute_plan(self, plan, page, validator, attempt_state):
        """Execute the plan only after the complete plan is valid."""

        for index, step in enumerate(plan["steps"]):
            action = step["action"]
            step_state = attempt_state["execution"]["steps"][index]
            attempt_state["execution"]["current_step"] = index + 1
            step_state["status"] = "running"
            attempt_state["execution"]["current_url"] = page.url if page else None
            self._persist_state()

            try:
                result = None
                if action == "navigate":
                    url = self._resolve_step_reference(step["url"], attempt_state)
                    if not validator.is_url_valid(url):
                        raise ValueError(f"Resolved navigation URL is invalid: {url}")
                    print(f"Navigating to {url}")
                    await page.goto(url)
                    await page.wait_for_timeout(1000)
                    await self._capture_screenshot(page, "traces/opened.png")

                elif action == "api_request":
                    api_step = dict(step)
                    api_step["url"] = self._resolve_step_reference(
                        step["url"], attempt_state
                    )
                    if not validator.is_url_valid(api_step["url"]):
                        raise ValueError(
                            f"Resolved API URL is invalid: {api_step['url']}"
                        )
                    result = await execute_api_request(api_step)
                    print(json.dumps(result, indent=2, ensure_ascii=False))

                elif action == "gmail_search":
                    from integrations.gmail import search_messages

                    result = await asyncio.to_thread(
                        search_messages,
                        step["query"],
                        step.get("max_results", 10),
                    )
                    print(json.dumps(result, indent=2, ensure_ascii=False))

                elif action == "gmail_send":
                    from integrations.gmail import send_message

                    result = await asyncio.to_thread(
                        send_message,
                        step["to"],
                        step["subject"],
                        step["body"],
                        step.get("cc"),
                        step.get("bcc"),
                    )
                    print(json.dumps(result, indent=2, ensure_ascii=False))

                elif action == "download":
                    url = self._resolve_step_reference(step["url"], attempt_state)
                    if not validator.is_url_valid(url):
                        raise ValueError(f"Resolved download URL is invalid: {url}")
                    step_state["resolved_url"] = url
                    failed_urls = {
                        failure.get("resolved_url")
                        or failure.get("plan_step", {}).get("url")
                        for failure in self.failed_downloads
                        if isinstance(
                            failure.get("resolved_url")
                            or failure.get("plan_step", {}).get("url"),
                            str,
                        )
                    }
                    if url in failed_urls:
                        raise RuntimeError(
                            f"Download URL already failed and must not be retried: {url}"
                        )
                    result = str(await download_url(page, url))
                    print(f"Downloaded file: {result}")

                elif action == "extract":
                    result = await extract_categorized(
                        page,
                        step["category"],
                        step["fields"],
                        query=step.get("query") or self.original_task,
                    )
                    step_state["result"] = result
                    if result.get("blocked"):
                        raise RuntimeError(
                            f"Extraction blocked: {result['blocked']}"
                        )
                    if result.get("missing_fields"):
                        missing_fields = ", ".join(result["missing_fields"])
                        raise RuntimeError(
                            f"Extraction incomplete; missing requested fields: {missing_fields}"
                        )
                    print(json.dumps(result, indent=2, ensure_ascii=False))

                elif action in {"fill", "click", "press", "select"}:
                    locator = await self.get_target_locator(validator, page, step)
                    if locator is None:
                        if action == "click" and self._target_is_current_page(
                            page,
                            step.get("target"),
                        ):
                            print(
                                f"Already on requested page for target: "
                                f"{step['target']['name']}"
                            )
                            step_state["status"] = "succeeded"
                            attempt_state["execution"]["current_step"] = None
                            self._persist_state()
                            continue
                        raise ValueError("The validated target could not be resolved")

                    if action == "fill":
                        await locator.fill(step["value"])
                    elif action == "click":
                        await locator.click()
                    elif action == "press":
                        await self._press_key(page, locator, step)
                    elif action == "select":
                        await locator.select_option(step["value"])
                    await self._capture_screenshot(page, "traces/action.png")

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
                if action in {"extract", "click", "fill", "press", "select"}:
                    try:
                        step_state["observation"] = await self._observe_extraction_page(page)
                    except Exception as observation_error:
                        step_state["observation"] = {
                            "error": str(observation_error),
                        }
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

        current_url = page.url if page else "about:blank"

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
            mode=config.MODE,
            replanning=True,
        )

        if config.MODE == "Api":
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

        failed_extract = next(
            (
                failure.get("plan_step")
                for failure in replan_data["failed_step"].values()
                if failure.get("plan_step", {}).get("action") == "extract"
            ),
            None,
        )
        has_extract = any(
            isinstance(step, dict) and step.get("action") == "extract"
            for step in new_plan["steps"]
        )
        if failed_extract and not has_extract:
            has_navigation = any(
                isinstance(step, dict) and step.get("action") == "navigate"
                for step in new_plan["steps"]
            )
            if not has_navigation:
                raise RuntimeError(
                    "Extraction recovery unavailable: the planner did not provide "
                    "an alternate source or an extraction step."
                )
            new_plan["steps"].append(failed_extract)
        return new_plan

async def main(planning):
    async with async_playwright() as playwright:
        await Executor(playwright, planning).do_task()
