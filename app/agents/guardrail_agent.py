"""The Guardrail — the pre-flight decision point. Runs before the Orchestrator
ever sees the message and never touches the database or answers the customer;
it only decides whether the message is safe and in-scope to process at all.

Fail-open by design: a malformed classification defaults to "allow". A
guardrail is a real orchestration decision point, but the goal here is to
demonstrate that pattern — it should not become a source of false blocks
that make the demo look broken.
"""
import json
import logging

from app.agents.prompts import GUARDRAIL_SYSTEM_PROMPT
from app.llm import LLMClient

logger = logging.getLogger("shiptrack.agents.guardrail")

VALID_CATEGORIES = {"prompt_injection", "internal_data", "other_customer_data", "abuse"}


class GuardrailAgent:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def classify(self, message: str) -> dict:
        raw = self.llm.chat(GUARDRAIL_SYSTEM_PROMPT, message, json_mode=True, temperature=0)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Guardrail returned non-JSON output, fail-open (allow): %r", raw)
            return {"restricted": False, "category": None, "in_scope": True, "reason": "fallback: unparseable model output"}

        restricted = bool(data.get("restricted", False))
        category = data.get("category")
        if restricted and category not in VALID_CATEGORIES:
            category = "internal_data"  # safe default rather than trusting an unknown category blindly
        if not restricted:
            category = None

        # in_scope defaults to True unless explicitly False — a malformed
        # verdict must never wrongly redirect a legitimate support message.
        in_scope = data.get("in_scope", True) is not False

        return {
            "restricted": restricted,
            "category": category,
            "in_scope": in_scope,
            "reason": data.get("reason", ""),
        }


REFUSAL_MESSAGE = "I can't help with that request. I'm only able to assist with shipment tracking and support questions."
OUT_OF_SCOPE_MESSAGE = "I'm the ShipTrack assistant, so I can only help with shipment tracking and support questions. Is there a shipment I can help you check on?"
