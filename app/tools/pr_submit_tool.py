"""Write-side tool: submits a completed PR draft to the ERP's submit-draft-PR
endpoint. Unlike every other write in this app (ticket_tools.create_support_
ticket, a plain Postgres INSERT), this is the first tool that reaches an
external system over HTTP — so a failure here is a real network/API error,
not a DB error, and app/main.py handles that as a distinct "submission
failed" case rather than letting it bubble up as a 500.

Falls back to a deterministic mock response when PR_SUBMIT_URL isn't
configured, so the whole Draft PR Agent flow is runnable and demoable
without real ERP credentials — same reasoning as LLM_MODE=mock in
app/llm.py. The response shape below ({"message": {"messageType": 1,
"message": "PR/..."}}) matches the example you were given; the failure
shape from the real API hasn't been provided yet, so a non-2xx response is
just surfaced as an httpx.HTTPStatusError for now — tighten this once you
have that.
"""
import itertools
import logging

import httpx

from app.config import settings

logger = logging.getLogger("shiptrack.tools.pr_submit")

_mock_counter = itertools.count(1)


async def submit_draft_pr(payload: dict) -> dict:
    if not settings.pr_submit_url:
        n = next(_mock_counter)
        logger.info(
            "PR_SUBMIT_URL not configured — returning a mock PR number "
            "(set PR_SUBMIT_URL in .env to call the real ERP endpoint)"
        )
        return {"message": {"messageType": 1, "message": f"PR/MOCK/{n:05d}"}}

    headers = {"Content-Type": "application/json"}
    if settings.pr_api_key:
        headers["Authorization"] = f"Bearer {settings.pr_api_key}"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(settings.pr_submit_url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()
