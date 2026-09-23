"""The Orchestrator Agent — the 'brain' of the pipeline. It never touches the
database and never talks to the customer; it only decides which specialist
agent + tool should run next."""
import json
import logging

from app.agents.prompts import ORCHESTRATOR_SYSTEM_PROMPT
from app.llm import LLMClient

logger = logging.getLogger("shiptrack.agents.orchestrator")

VALID_INTENTS = {"track_shipment", "file_complaint", "file_purchase_requisition", "find_vendor", "general_faq"}


class OrchestratorAgent:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def classify(self, message: str, history: list[dict] | None = None) -> dict:
        transcript = ""
        if history:
            transcript = "\n".join(f"{turn['role']}: {turn['content']}" for turn in history[-6:])
            transcript = f"Recent conversation:\n{transcript}\n\n"

        user_prompt = f"{transcript}New customer message:\n{message}"

        raw = self.llm.chat(ORCHESTRATOR_SYSTEM_PROMPT, user_prompt, json_mode=True, temperature=0)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Orchestrator returned non-JSON output, defaulting to general_faq: %r", raw)
            return {"intent": "general_faq", "tracking_number": None, "reason": "fallback: unparseable model output"}

        intent = data.get("intent")
        if intent not in VALID_INTENTS:
            logger.warning("Orchestrator returned unknown intent %r, defaulting to general_faq", intent)
            intent = "general_faq"

        tracking_number = data.get("tracking_number")
        if isinstance(tracking_number, str):
            tracking_number = tracking_number.strip().upper() or None

        # Only meaningful for find_vendor — left as the customer's own
        # words (never uppercased/normalized) since VendorAgent resolves it
        # against pr_materials the same free-text way PR intake does.
        material_query = data.get("material_query")
        if isinstance(material_query, str):
            material_query = material_query.strip() or None
        else:
            material_query = None

        return {
            "intent": intent,
            "tracking_number": tracking_number,
            "material_query": material_query,
            "reason": data.get("reason", ""),
        }
