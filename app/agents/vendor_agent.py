"""The Vendor Agent — composes a natural-language vendor recommendation
strictly grounded in the vendor/price rows resolved from Postgres
(app/tools/vendor_tools.py). Same grounding discipline as TrackingAgent: the
LLM only phrases a sentence around real rows it's handed — it never invents
a vendor, a price, or a material that isn't in the context, and never
re-ranks the list itself (the DB query already sorts cheapest-first)."""
import json

from app.agents.prompts import VENDOR_SYSTEM_PROMPT
from app.llm import LLMClient


class VendorAgent:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def answer(self, question: str, context: dict) -> str:
        user_prompt = (
            f"Customer question:\n{question}\n\n"
            f"Context (JSON, from a real database lookup):\n"
            f"{json.dumps(context, default=str)}"
        )
        return self.llm.chat(VENDOR_SYSTEM_PROMPT, user_prompt, temperature=0.3)
