"""The Tracking Agent — composes a natural-language answer strictly grounded
in the shipment rows the orchestrator fetched from Postgres.

`context` always has the shape:
    {"tracking_number": str|None, "found": bool, "shipment": dict|None, "events": list}
"""
import json

from app.agents.prompts import TRACKING_SYSTEM_PROMPT
from app.llm import LLMClient


class TrackingAgent:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def answer(self, question: str, context: dict) -> str:
        user_prompt = (
            f"Customer question:\n{question}\n\n"
            f"Context (JSON, from a real database lookup):\n"
            f"{json.dumps(context, default=str)}"
        )
        return self.llm.chat(TRACKING_SYSTEM_PROMPT, user_prompt, temperature=0.3)
