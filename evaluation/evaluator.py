"""Evaluate one recorded agent run without executing the task again."""

import json
from pathlib import Path
from typing import Any


CASES: dict[str, dict[str, Any]] = {
    "1": {
        "name": "Basic navigation and page extraction",
        "required_actions": ["navigate", "extract"],
        "required_fields": ["page_title"],
    },
    "2": {
        "name": "Search and extract an article",
        "required_actions": ["navigate", "fill", "press", "click", "extract"],
        "required_fields": ["article_title", "first_paragraph"],
    },
    "3": {
        "name": "Click a named link and extract the next page",
        "required_actions": ["navigate", "click", "extract"],
        "required_fields": ["page_title"],
    },
    "4": {
        "name": "Select a form option",
        "required_actions": ["navigate", "select", "extract"],
        "required_fields": ["selected_value"],
    },
    "5": {
        "name": "Extract an official website and follow it",
        "required_actions": ["navigate", "extract", "navigate", "extract"],
        "required_fields": ["official_website_url", "page_title"],
    },
    "6": {
        "name": "Extract a direct downloadable resource",
        "required_actions": ["navigate", "extract"],
        "required_fields": ["resource_name", "resource_url"],
    },
    "7": {
        "name": "Download a direct file",
        "required_actions": ["download"],
        "download_must_succeed": True,
    },
    "8": {
        "name": "Extract a URL and download it",
        "required_actions": ["navigate", "extract", "download"],
        "required_fields": ["resource_url"],
        "download_must_succeed": True,
    },
    "9": {
        "name": "Reject an HTML page as a download",
        "required_actions": ["download"],
        "expected_status": "failed",
    },
    "10": {
        "name": "API-first read-only request",
        "required_actions": ["api_request"],
        "api_must_succeed": True,
    },
    "11": {
        "name": "API request with query parameters",
        "required_actions": ["api_request"],
        "api_must_succeed": True,
    },
    "12": {
        "name": "Reject an unsupported API method",
        "required_actions": ["api_request"],
        "expected_status": "failed",
    },
    "13": {
        "name": "Gmail search",
        "required_actions": ["gmail_search"],
        "gmail_must_succeed": True,
    },
    "14": {
        "name": "Gmail send",
        "required_actions": ["gmail_send"],
        "gmail_must_succeed": True,
    },
    "15": {
        "name": "Missing Gmail send scope",
        "required_actions": ["gmail_send"],
        "expected_status": "failed",
    },
    "16": {
        "name": "Invalid target and bounded recovery",
        "required_actions": ["click"],
        "expected_status": "failed",
    },
    "17": {
        "name": "Human-verification handling",
        "required_actions": ["extract"],
        "expected_status": "failed",
        "blocked_must_be_reported": True,
    },
}


def load_state(path: str | Path) -> dict[str, Any]:
    """Load one state snapshot created by the executor."""
    state_path = Path(path)
    with state_path.open("r", encoding="utf-8") as state_file:
        state = json.load(state_file)
    if not isinstance(state, dict):
        raise ValueError("The state file must contain a JSON object")
    return state


def _attempt_number(key: str) -> int:
    if key.startswith("Attempt") and key[7:].isdigit():
        return int(key[7:])
    return -1


def latest_attempt(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return the latest numbered attempt from a state snapshot."""
    attempts = [
        (key, value)
        for key, value in state.items()
        if _attempt_number(key) >= 1 and isinstance(value, dict)
    ]
    if not attempts:
        raise ValueError("The state file does not contain an AttemptN entry")
    return max(attempts, key=lambda item: _attempt_number(item[0]))


def _contains_in_order(actual: list[str], expected: list[str]) -> bool:
    expected_index = 0
    for action in actual:
        if expected_index < len(expected) and action == expected[expected_index]:
            expected_index += 1
    return expected_index == len(expected)


def _step_result(step: dict[str, Any]) -> Any:
    return step.get("result")


def _result_contains_field(result: Any, field: str) -> bool:
    if not isinstance(result, dict):
        return False
    data = result.get("data")
    if not isinstance(data, dict):
        return False
    value = data.get(field)
    return value is not None and value != "" and value != []


def _has_successful_download(step: dict[str, Any]) -> bool:
    if step.get("status") != "succeeded":
        return False
    result = step.get("result")
    if not isinstance(result, str):
        return False
    return result.replace("\\", "/").startswith("downloads/")


def _has_successful_api(step: dict[str, Any]) -> bool:
    if step.get("status") != "succeeded":
        return False
    result = _step_result(step)
    return isinstance(result, dict) and 200 <= result.get("status_code", 0) < 300


def _has_successful_gmail(step: dict[str, Any]) -> bool:
    if step.get("status") != "succeeded":
        return False
    result = _step_result(step)
    return isinstance(result, (dict, list))


def evaluate_state(
    state: dict[str, Any],
    case_id: str | None = None,
) -> dict[str, Any]:
    """Return a check-by-check evaluation report for one saved run."""
    attempt_key, attempt = latest_attempt(state)
    plan = attempt.get("plan") or {}
    plan_steps = plan.get("steps") if isinstance(plan, dict) else None
    plan_steps = plan_steps if isinstance(plan_steps, list) else []
    execution = attempt.get("execution") or {}
    execution_steps = execution.get("steps") if isinstance(execution, dict) else None
    execution_steps = execution_steps if isinstance(execution_steps, list) else []
    actions = [
        step.get("action")
        for step in plan_steps
        if isinstance(step, dict) and isinstance(step.get("action"), str)
    ]

    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    expected = CASES.get(str(case_id)) if case_id is not None else None
    if case_id is not None and expected is None:
        raise ValueError(f"Unknown case {case_id}. Use --list to see valid cases.")

    expected_status = (expected or {}).get("expected_status", "succeeded")
    actual_status = attempt.get("status")
    check(
        "terminal status",
        actual_status == expected_status,
        f"expected {expected_status}, observed {actual_status}",
    )
    check("plan exists", bool(plan_steps), f"{len(plan_steps)} plan step(s)")

    if expected:
        required_actions = expected.get("required_actions", [])
        check(
            "required action order",
            _contains_in_order(actions, required_actions),
            f"required {required_actions}; observed {actions}",
        )

        for field in expected.get("required_fields", []):
            field_found = any(
                step.get("action") == "extract"
                and _result_contains_field(_step_result(step), field)
                for step in execution_steps
                if isinstance(step, dict)
            )
            check(
                f"field: {field}",
                field_found,
                "field was returned by an extract step"
                if field_found
                else "field was not returned by a successful extract step",
            )

        if expected.get("download_must_succeed"):
            download_ok = any(
                _has_successful_download(step)
                for step in execution_steps
                if isinstance(step, dict) and step.get("action") == "download"
            )
            check("download result", download_ok, "saved path must be under downloads/")

        if expected.get("api_must_succeed"):
            api_ok = any(
                _has_successful_api(step)
                for step in execution_steps
                if isinstance(step, dict) and step.get("action") == "api_request"
            )
            check("API result", api_ok, "expected a successful 2xx API response")

        if expected.get("gmail_must_succeed"):
            gmail_ok = any(
                _has_successful_gmail(step)
                for step in execution_steps
                if isinstance(step, dict)
                and step.get("action") in {"gmail_search", "gmail_send"}
            )
            check("Gmail result", gmail_ok, "expected a Gmail result")

        if expected.get("blocked_must_be_reported"):
            blocked = any(
                isinstance(_step_result(step), dict)
                and bool(_step_result(step).get("blocked"))
                for step in execution_steps
                if isinstance(step, dict) and step.get("action") == "extract"
            )
            check("blocked status", blocked, "expected extraction to report blocked")

    passed_checks = sum(item["passed"] for item in checks)
    total_checks = len(checks)
    return {
        "task": state.get("task"),
        "attempt": attempt_key,
        "case_id": str(case_id) if case_id is not None else None,
        "case_name": (expected or {}).get("name"),
        "status": "passed" if passed_checks == total_checks else "failed",
        "score": f"{passed_checks}/{total_checks}",
        "checks": checks,
    }


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"Evaluation: {report['status'].upper()} ({report['score']})",
        f"Attempt: {report['attempt']}",
    ]
    if report.get("case_name"):
        lines.append(f"Case: {report['case_id']} - {report['case_name']}")
    if report.get("task"):
        lines.append(f"Task: {report['task']}")
    lines.append("")
    for item in report["checks"]:
        marker = "PASS" if item["passed"] else "FAIL"
        lines.append(f"[{marker}] {item['name']}: {item['detail']}")
    return "\n".join(lines)
