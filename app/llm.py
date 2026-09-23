"""Single point of contact with the LLM.

Supports three interchangeable backends behind one `.chat()` call, all via
the OpenAI SDK (the local server and Foundry are both OpenAI-compatible):
  - "local"   a local OpenAI-compatible server — Ollama by default
              (`ollama pull gpt-oss:20b`), pointed at LOCAL_BASE_URL.
  - "foundry" a Microsoft/Azure AI Foundry deployment.
  - "mock"    a small deterministic mock, zero external dependencies.

LLM_MODE=auto (the default) picks local if LOCAL_BASE_URL is set, else
foundry if configured, else mock — so the pipeline always runs, and swapping
backends is an .env change, not a code change.
"""
import json
import logging
import re

from app.config import settings

logger = logging.getLogger("shiptrack.llm")


class LLMClient:
    def __init__(self):
        self._client = None
        self.deployment: str | None = None
        mode = settings.llm_mode

        if mode == "mock":
            self.active_mode = "mock"
        elif mode == "local":
            self._init_local()
        elif mode == "foundry":
            self._init_foundry()
        elif mode == "auto":
            if settings.local_base_url:
                self._init_local()
            elif settings.foundry_endpoint and settings.foundry_api_key:
                self._init_foundry()
            else:
                self.active_mode = "mock"
                logger.warning(
                    "No LLM backend configured (LOCAL_BASE_URL / FOUNDRY_ENDPOINT+FOUNDRY_API_KEY all unset) "
                    "— running in MOCK LLM mode."
                )
        else:
            raise RuntimeError(f"Unknown LLM_MODE={mode!r} — expected auto, local, foundry, or mock.")

        logger.info("LLM client ready — mode=%s model=%s", self.active_mode, self.deployment)

    def _init_local(self) -> None:
        if not settings.local_base_url:
            raise RuntimeError("LLM_MODE=local but LOCAL_BASE_URL is not set (e.g. http://localhost:11434/v1).")
        from openai import OpenAI

        self._client = OpenAI(base_url=settings.local_base_url, api_key=settings.local_api_key)
        self.deployment = settings.local_model
        self.active_mode = "local"

    def _init_foundry(self) -> None:
        if not (settings.foundry_endpoint and settings.foundry_api_key):
            raise RuntimeError(
                "LLM_MODE=foundry but FOUNDRY_ENDPOINT/FOUNDRY_API_KEY are not set. "
                "Fill them in .env, or set LLM_MODE=local/mock."
            )
        from openai import AzureOpenAI

        self._client = AzureOpenAI(
            azure_endpoint=settings.foundry_endpoint,
            api_key=settings.foundry_api_key,
            api_version=settings.foundry_api_version,
        )
        self.deployment = settings.foundry_deployment
        self.active_mode = "foundry"

    def chat(self, system_prompt: str, user_prompt: str, *, json_mode: bool = False, temperature: float = 0.2) -> str:
        if self.active_mode == "mock":
            return _mock_chat(system_prompt, user_prompt, json_mode)

        kwargs = {}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = self._client.chat.completions.create(
            model=self.deployment,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            **kwargs,
        )
        return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Mock mode — heuristic, no network calls. Good enough to exercise every
# branch of the orchestrator (routing, DB grounding, ticket drafting) before
# Foundry credentials are wired up.
# ---------------------------------------------------------------------------

_TRACKING_RE = re.compile(r"\bTRK[- ]?\d{4,}\b", re.IGNORECASE)
_COMPLAINT_WORDS = ("damaged", "broken", "lost", "missing", "complain", "complaint", "refund", "wrong address", "never arrived", "terrible", "awful")
_TRACKING_WORDS = ("track", "where", "status", "when will", "eta", "arrive", "delivery")
# Full phrases only (never a bare "pr") to avoid false-positiving on unrelated
# words like "pretty" or "print".
_PR_WORDS = ("purchase requisition", "raise a pr", "raise pr", "draft a pr", "draft pr", "create a pr", "file a pr")
_VENDOR_WORDS = (
    "cheapest vendor", "cheapest supplier", "best vendor", "best supplier",
    "compare vendor", "compare price", "compare prices", "vendor for",
    "supplier for", "who sells", "which vendor", "lowest price",
)
# Stripped out of a find_vendor message to approximate the material term
# alone (e.g. "find me the cheapest vendor selling steel" -> "steel") — a
# real LLM backend does this extraction properly via ORCHESTRATOR_SYSTEM_
# PROMPT; this is only the zero-network mock's stand-in for it.
_VENDOR_FILLER_WORDS = {
    "find", "me", "the", "a", "an", "cheapest", "cheaper", "best", "lowest",
    "vendor", "vendors", "supplier", "suppliers", "who", "is", "are",
    "selling", "sells", "sell", "for", "of", "compare", "price", "prices",
    "pricing", "which", "on", "us", "please", "can", "you", "give", "show",
    "i", "need", "want", "to", "buy",
}


def _mock_chat(system_prompt: str, user_prompt: str, json_mode: bool) -> str:
    if "Guardrail" in system_prompt:
        return _mock_guardrail(user_prompt)
    if "Orchestrator" in system_prompt:
        return _mock_orchestrator(user_prompt)
    if "Escalation" in system_prompt:
        return _mock_escalation(user_prompt)
    if "Vendor" in system_prompt:
        return _mock_vendor(user_prompt)
    return _mock_tracking(user_prompt)


_RESTRICTED_WORDS = {
    "prompt_injection": ("ignore previous instructions", "ignore all previous", "disregard your instructions", "system prompt", "your instructions"),
    "internal_data": ("database schema", "internal id", "admin password", "api key", "your prompt"),
    "other_customer_data": ("every customer", "all customers", "list every", "everyone's tracking", "other customers'"),
}
_OUT_OF_SCOPE_WORDS = ("joke", "poem", "recipe", "weather", "capital of", "write code", "python function", "who won the")
_GREETING_WORDS = ("hi", "hello", "hey", "thanks", "thank you", "good morning", "good afternoon")


def _mock_guardrail(user_prompt: str) -> str:
    lower = user_prompt.lower()

    for category, words in _RESTRICTED_WORDS.items():
        if any(w in lower for w in words):
            return json.dumps({"restricted": True, "category": category, "in_scope": True, "reason": f"[mock] matched '{category}' heuristic"})

    if any(w in lower for w in _GREETING_WORDS):
        return json.dumps({"restricted": False, "category": None, "in_scope": True, "reason": "[mock] greeting"})

    if any(w in lower for w in _OUT_OF_SCOPE_WORDS):
        return json.dumps({"restricted": False, "category": None, "in_scope": False, "reason": "[mock] unrelated to shipments"})

    return json.dumps({"restricted": False, "category": None, "in_scope": True, "reason": "[mock] no red flags"})


def _extract_tracking_number(text: str) -> str | None:
    match = _TRACKING_RE.search(text)
    if not match:
        return None
    return match.group(0).upper().replace(" ", "").replace("-", "")


def _extract_material_query(text: str) -> str | None:
    words = re.findall(r"[a-zA-Z0-9]+", text.lower())
    kept = [w for w in words if w not in _VENDOR_FILLER_WORDS]
    return " ".join(kept) or None


def _mock_orchestrator(user_prompt: str) -> str:
    lower = user_prompt.lower()
    tracking_number = _extract_tracking_number(user_prompt)

    if any(phrase in lower for phrase in _VENDOR_WORDS):
        intent = "find_vendor"
    elif any(phrase in lower for phrase in _PR_WORDS):
        intent = "file_purchase_requisition"
    elif any(word in lower for word in _COMPLAINT_WORDS):
        intent = "file_complaint"
    elif tracking_number or any(word in lower for word in _TRACKING_WORDS):
        intent = "track_shipment"
    else:
        intent = "general_faq"

    material_query = _extract_material_query(user_prompt) if intent == "find_vendor" else None

    return json.dumps({
        "intent": intent,
        "tracking_number": tracking_number,
        "material_query": material_query,
        "reason": f"[mock] matched heuristic for '{intent}'",
    })


def _mock_tracking(user_prompt: str) -> str:
    # user_prompt embeds the Context JSON built by TrackingAgent.answer().
    match = re.search(r"Context \(JSON.*?:\s*(\{.*\})", user_prompt, re.DOTALL)
    try:
        ctx = json.loads(match.group(1)) if match else {}
    except json.JSONDecodeError:
        ctx = {}

    tracking_number = ctx.get("tracking_number")
    found = ctx.get("found")
    shipment = ctx.get("shipment") or {}
    events = ctx.get("events") or []

    if not tracking_number:
        return ("[mock] Hi, I'm the ShipTrack assistant. Give me a tracking number (e.g. TRK100234) "
                "and I can look up its status for you.")

    if not found:
        return (f"[mock] I couldn't find a shipment matching '{tracking_number}' in our system. "
                "Could you double-check the tracking number and try again?")

    status = shipment.get("status", "unknown")
    last_event = events[-1] if events else None

    sentence = f"[mock] Your shipment {shipment.get('tracking_number')} is currently '{status}'."
    if last_event:
        sentence += f" Latest update: {last_event.get('note')} ({last_event.get('location')})."
    if shipment.get("estimated_delivery"):
        sentence += f" Estimated delivery: {shipment.get('estimated_delivery')}."
    return sentence


def _mock_vendor(user_prompt: str) -> str:
    # user_prompt embeds the Context JSON built by VendorAgent.answer() —
    # {"material": dict|None, "vendors": [...]} sorted cheapest-first.
    match = re.search(r"Context \(JSON.*?:\s*(\{.*\})", user_prompt, re.DOTALL)
    try:
        ctx = json.loads(match.group(1)) if match else {}
    except json.JSONDecodeError:
        ctx = {}

    material = ctx.get("material")
    vendors = ctx.get("vendors") or []

    if not material:
        return ("[mock] I couldn't match that to anything in our material catalog — "
                "could you give me a material code or a more specific description?")

    description = material.get("description", material.get("material_code", "that material"))
    if not vendors:
        return f"[mock] I found '{description}' in the catalog, but no vendor pricing is on file for it yet."

    cheapest = vendors[0]
    sentence = (
        f"[mock] The cheapest option for {description} is {cheapest.get('vendor_name')} "
        f"at {cheapest.get('price')} per {cheapest.get('uom_code')}."
    )
    if len(vendors) > 1:
        sentence += f" {len(vendors) - 1} other vendor(s) also carry it."
    return sentence


def _mock_escalation(user_prompt: str) -> str:
    # Priority is no longer part of this agent's job (see derive_priority in
    # app/tools/ticket_tools.py) — mock only needs to pick issue_type.
    lower = user_prompt.lower()
    if "damaged" in lower or "broken" in lower:
        issue_type = "damaged"
    elif "lost" in lower or "missing" in lower or "never arrived" in lower:
        issue_type = "lost"
    elif "wrong address" in lower:
        issue_type = "wrong_address"
    elif "unsatisfactory" in lower or "not helpful" in lower or "don't like" in lower or "didn't like" in lower:
        issue_type = "unsatisfactory_response"
    elif "delayed" in lower or "delay" in lower or "late" in lower:
        issue_type = "delayed"
    else:
        issue_type = "other"

    return json.dumps({
        "issue_type": issue_type,
        "subject": f"[mock] Customer issue: {issue_type.replace('_', ' ')}",
        "description": f"[mock-drafted ticket] Customer reported an issue classified as '{issue_type}'. "
                        f"Original context: {user_prompt[:400]}",
        "customer_reply": "[mock] I've raised a support ticket for this — a member of our team will follow up shortly.",
    })
