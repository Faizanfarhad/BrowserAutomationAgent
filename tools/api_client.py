"""Small direct HTTP client used by API-first plan steps."""

from typing import Any

import httpx

from config import API_TIMEOUT_SECONDS


async def execute_api_request(step: dict[str, Any]) -> dict[str, Any]:
    """Execute one read-only JSON API request from a validated plan step."""
    method = str(step.get("method", "GET")).upper()
    if method != "GET":
        raise ValueError("api_request currently supports GET requests only")

    url = step["url"]
    params = step.get("params") or {}
    headers = step.get("headers") or {}

    async with httpx.AsyncClient(
        timeout=API_TIMEOUT_SECONDS,
        follow_redirects=True,
    ) as client:
        response = await client.get(url, params=params, headers=headers)

    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type:
        response_data = response.json()
    else:
        response_data = response.text

    return {
        "status_code": response.status_code,
        "content_type": content_type,
        "data": response_data,
        "url": str(response.url),
    }
