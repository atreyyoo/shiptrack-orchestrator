"""The Escalation Agent — drafts a support ticket (and a customer-facing
reply) from conversation context. Used both for direct complaints
(orchestrator intent = file_complaint) and for the 'I didn't like that
answer, raise a ticket' feedback path."""
import json
import logging
from typing import Any, Optional

from app.agents.prompts import ESCALATION_SYSTEM_PROMPT
from app.llm import LLMClient

logger = logging.getLogger("shiptrack.agents.escalation")

VALID_ISSUE_TYPES = {"damaged", "lost", "delayed", "wrong_address", "unsatisfactory_response", "other"}
VALID_PRIORITIES = {"low", "medium", "high", "urgent"}


class EscalationAgent:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def draft(self, question: str, shipment_context: Optional[dict[str, Any]], extra_note: Optional[str] = None) -> dict:
        parts = [f"Customer message / issue:\n{question}"]
        if extra_note:
            parts.append(f"Additional detail from customer:\n{extra_note}")
        parts.append(f"Shipment data (JSON, null if not applicable):\n{json.dumps(shipment_context, default=str)}")
        user_prompt = "\n\n".join(parts)

        raw = self.llm.chat(ESCALATION_SYSTEM_PROMPT, user_prompt, json_mode=True, temperature=0.2)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Escalation agent returned non-JSON output, using fallback ticket: %r", raw)
            data = {}

        issue_type = data.get("issue_type")
        if issue_type not in VALID_ISSUE_TYPES:
            issue_type = "other"

        priority = data.get("priority")
        if priority not in VALID_PRIORITIES:
            priority = "medium"

        return {
            "issue_type": issue_type,
            "priority": priority,
            "subject": data.get("subject") or "Customer support issue",
            "description": data.get("description") or question,
            "customer_reply": data.get("customer_reply")
            or "I've raised a support ticket for this — a member of our team will follow up shortly.",
        }
