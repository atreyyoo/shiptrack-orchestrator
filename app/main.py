"""ShipTrack orchestrator — FastAPI entrypoint.

Flow per requirement: API call (Postman) -> DB query (Postgres) -> context to
the LLM (agents, each with its own system prompt) -> output. Every step is
traced live to the console and persisted to `trace_events`.
"""
import asyncio
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles

from app import db, store
from app.agents.escalation_agent import EscalationAgent
from app.agents.guardrail_agent import OUT_OF_SCOPE_MESSAGE, REFUSAL_MESSAGE, GuardrailAgent
from app.agents.orchestrator import OrchestratorAgent
from app.agents.pr_draft_agent import DraftPRAgent
from app.agents.tracking_agent import TrackingAgent
from app.agents.vendor_agent import VendorAgent
from app.config import settings
from app.llm import LLMClient
from app.models import (
    ChatRequest,
    ChatResponse,
    FeedbackRequest,
    FeedbackResponse,
    TicketRequest,
    TicketResponse,
)
from app.tools import pr_intake_tool
from app.tools import pr_master_data_tools
from app.tools.pr_pdf_tool import build_pr_pdf
from app.tools.pr_submit_tool import submit_draft_pr
from app.tools.shipment_tools import GET_SHIPMENT_SQL, get_shipment_status, lookup_customer_shipments
from app.tools.ticket_intake_tool import check_readiness
from app.tools.ticket_tools import CREATE_SUPPORT_TICKET_SQL, create_support_ticket, derive_priority
from app.tools import vendor_tools
from app.tracing import Tracer

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s.%(msecs)03d %(levelname)-5s %(name)-24s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("shiptrack.main")

llm_client = LLMClient()
guardrail_agent = GuardrailAgent(llm_client)
orchestrator = OrchestratorAgent(llm_client)
tracking_agent = TrackingAgent(llm_client)
escalation_agent = EscalationAgent(llm_client)
vendor_agent = VendorAgent(llm_client)
draft_pr_agent = DraftPRAgent()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    logger.info("ShipTrack orchestrator ready — LLM mode=%s deployment=%s", llm_client.active_mode, llm_client.deployment)
    yield
    await db.disconnect()


app = FastAPI(title="ShipTrack Orchestrator", lifespan=lifespan)


@app.middleware("http")
async def _no_cache(request, call_next):
    """This is a fast-iterating local dev/demo app — frontend/ static files
    (app.js, style.css) change often and StaticFiles otherwise only sets
    ETag/Last-Modified, which leaves room for a browser to serve a stale
    cached copy after a plain reload. Blanket no-store removes that whole
    class of "did my browser actually pick up the new code" doubt; never do
    this on a real production static-asset path."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def _value_error_to_400(exc: ValueError):
    raise HTTPException(status_code=400, detail=str(exc))


async def build_tracking_context(pool: asyncpg.Pool, tracer: Tracer, tracking_number: str | None) -> dict:
    """The one place `search the DB for a shipment` happens. Always returns
    the envelope TrackingAgent/EscalationAgent expect."""
    if not tracking_number:
        return {"tracking_number": None, "found": False, "shipment": None, "events": []}

    async with tracer.step(None, "db_query:get_shipment_status", {
        "sql": GET_SHIPMENT_SQL,
        "params": [tracking_number],
    }) as out:
        result = await get_shipment_status(pool, tracking_number)
        out["found"] = result is not None

    if result is None:
        return {"tracking_number": tracking_number, "found": False, "shipment": None, "events": []}
    return {"tracking_number": tracking_number, "found": True, "shipment": result["shipment"], "events": result["events"]}


async def _question(pool: asyncpg.Pool, field: str, fields: dict, *, retry: bool = False) -> tuple[str, list[dict] | None, list[dict] | None]:
    """(answer text, options, history) for asking/re-asking `field` — options
    is the clickable-menu payload for a reference-table field or "vendor"
    (app/tools/pr_intake_tool.py:options_for()), history is the usage-history
    dropdown payload for a free-text lookup field (HISTORY_FIELDS) — each is
    None if it doesn't apply to this field. A field never has both. `fields`
    is the slot-fill state so far — used to make the "quantity"/"vendor"
    questions name the material (question_for()), and to scope "vendor"'s
    options to the already-resolved material (options_for())."""
    prefix = "Sorry, I couldn't match that. " if retry else ""
    history = await store.get_field_history(pool, field) if field in pr_intake_tool.HISTORY_FIELDS else None
    options = await pr_intake_tool.options_for(pool, field, fields)
    return prefix + pr_intake_tool.question_for(field, fields), options, history


async def handle_pr_draft(
    pool: asyncpg.Pool, tracer: Tracer, conversation_id: str, fields: dict, message: str
) -> tuple[str, str, str | None, list[dict] | None, int | None, list[dict] | None]:
    """Advances the Draft PR Agent's intake by one field per call (app/tools/
    pr_intake_tool.py) — a fixed question order, but each answer is resolved
    and stored under its field name, not just counted, since a PR draft
    needs several independent field values. Once every field is filled,
    assembles (app/agents/pr_draft_agent.py) and submits (app/tools/
    pr_submit_tool.py) the payload. Returns (agent_used, answer, pr_number,
    options, pr_id, history) — pr_id is the purchase_requisitions row id, for
    the download-as-PDF/payload links (GET /purchase-requisitions/{pr_id}/...)."""
    if not fields:
        await store.start_pr_draft(pool, conversation_id)
        field = pr_intake_tool.FIELD_ORDER[0]
        answer, options, history = await _question(pool, field, {})
        return "PRIntake", answer, None, options, None, history

    field = pr_intake_tool.next_missing_field(fields)

    async with tracer.step("PRIntake", f"resolve_field:{field}", {"field": field, "raw_answer": message}) as out:
        resolved = await pr_intake_tool.resolve_field(pool, field, message, fields)
        out["resolved"] = resolved

    if resolved is None:
        answer, options, history = await _question(pool, field, fields, retry=True)
        return "PRIntake", answer, None, options, None, history

    # Stock-sufficiency check — a quantity resolves (it's just a number) but
    # isn't accepted if it exceeds what's actually in stock, so available_qty
    # can't be decremented below zero. Checked here, before the field is
    # stored, rather than inside resolve_field(), since it's a business rule
    # against an earlier field (material), not a parse-format check.
    if field == "quantity":
        material = fields.get("material") or {}
        available = material.get("available_qty")
        if available is not None and resolved > float(available):
            async with tracer.step(None, "insufficient_stock", {"requested": resolved, "available": available}):
                pass
            answer = (
                f"Only {pr_intake_tool.fmt_number(available)} {material.get('default_uom_code', '')} of "
                f"{material.get('description', 'this material')} is available. Please enter a smaller quantity."
            )
            return "PRIntake", answer, None, None, None, None

    await store.update_pr_draft_field(pool, conversation_id, field, resolved)
    entry = pr_intake_tool.history_entry(field, resolved)
    if entry is not None:
        async with tracer.step(None, f"record_field_history:{field}", {"value": entry[0]}):
            await store.record_field_history(pool, field, entry[0], entry[1])
    fields = {**fields, field: resolved}

    # Auto-skip "vendor" when nobody has priced the just-resolved material —
    # there'd be nothing to choose between, so asking would be a dead end.
    # Stored as an explicit null (not just left absent) so next_missing_field
    # treats it as answered and moves straight to WBS; DraftPRAgent then
    # falls back to the material's own catalog suggested_rate, exactly like
    # before this field existed.
    if field == "material":
        vendor_rows = await vendor_tools.get_vendors_for_material_code(pool, resolved["material_code"])
        if not vendor_rows:
            async with tracer.step(None, "auto_skip_vendor", {"material_code": resolved["material_code"], "reason": "no vendor pricing on file"}):
                await store.update_pr_draft_field(pool, conversation_id, "vendor", None)
            fields["vendor"] = None

    next_field = pr_intake_tool.next_missing_field(fields)

    if next_field:
        answer, options, history = await _question(pool, next_field, fields)
        return "PRIntake", answer, None, options, None, history

    async with tracer.step(None, "load_pr_constants", {}) as out:
        constants = await pr_master_data_tools.get_pr_constants(pool)
        out["keys"] = list(constants.keys())

    async with tracer.step("DraftPRAgent", "build_payload", {}) as out:
        payload = draft_pr_agent.build_payload(fields, constants)
        out["payload"] = payload

    async with tracer.step(None, "submit_draft_pr", {"url": settings.pr_submit_url or "(mock — PR_SUBMIT_URL not set)"}) as out:
        try:
            response = await submit_draft_pr(payload)
            pr_number = response.get("message", {}).get("message")
            status = "submitted"
        except Exception as exc:
            response = {"error": str(exc)}
            pr_number = None
            status = "failed"
        out["status"] = status
        out["pr_number"] = pr_number

    if status == "submitted":
        material = fields["material"]
        async with tracer.step(None, "decrement_material_stock", {"material_code": material["material_code"], "qty": fields["quantity"]}) as out:
            out["decremented"] = await store.decrement_material_stock(pool, material["material_code"], float(fields["quantity"]))

    pr_id = await store.record_purchase_requisition(
        pool,
        conversation_id=conversation_id,
        pr_number=pr_number,
        status=status,
        payload=payload,
        response=response,
        fields_snapshot=fields,
    )
    await store.reset_pr_draft(pool, conversation_id)

    if status == "submitted":
        return "DraftPRAgent", f"PR raised — {pr_number}.", pr_number, None, pr_id, None
    return (
        "DraftPRAgent",
        f"Sorry, I couldn't submit that PR ({response.get('error')}). Please try again shortly, or raise it manually.",
        None,
        None,
        pr_id,
        None,
    )


@app.get("/health")
async def health():
    return {"status": "ok", "llm_mode": llm_client.active_mode}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    pool = db.get_pool()

    try:
        conversation = await store.get_or_create_conversation(pool, req.conversation_id, req.customer_email)
    except ValueError as exc:
        _value_error_to_400(exc)
    conversation_id = str(conversation["id"])
    turn_id = req.turn_id or str(uuid.uuid4())

    # PR-draft continuation check — deterministic, same "runs before the LLM
    # gets a vote" reasoning as the Guardrail below. Without this, a terse
    # mid-intake answer like "10 MT" would need the Orchestrator to correctly
    # re-classify it as file_purchase_requisition from conversation history
    # alone; instead, an in-progress draft short-circuits straight back into
    # its own intake (app/tools/pr_intake_tool.py) regardless of how the LLM
    # would have classified this message on its own.
    pr_fields = conversation.get("pr_draft_fields") or {}
    pr_in_progress = bool(pr_fields) and pr_intake_tool.next_missing_field(pr_fields) is not None

    tracer = Tracer(pool, conversation_id, turn_id)

    async with tracer.step(None, "receive_api_call", {"message": req.message, "customer_email": req.customer_email}):
        pass

    history = await store.get_recent_turns(pool, conversation_id, limit=6)
    await store.add_turn(pool, conversation_id, "user", req.message)

    # Pre-flight: the Guardrail runs before the Orchestrator ever sees the
    # message. This is a real orchestration decision point — restricted or
    # out-of-scope messages short-circuit here and never reach the DB, the
    # Orchestrator, or any specialist agent.
    async with tracer.step("Guardrail", "classify_safety", {"message": req.message}) as out:
        verdict = await asyncio.to_thread(guardrail_agent.classify, req.message)
        if pr_in_progress:
            # guardrail_agent.classify() never sees conversation history, so
            # a bare fragment answering our own PR-intake question reads as
            # suspicious out of context — confirmed in testing hitting BOTH
            # "internal_data" (a job code) and "prompt_injection" (a
            # misspelled "own premises") on the exact same small local
            # model. That rules out patching categories one at a time; the
            # whole restricted check is unreliable here without context, so
            # it's fully suppressed while a PR draft is in progress.
            #
            # This is safe specifically because nothing downstream of this
            # point is an LLM: pr_intake_tool.py resolves every field via a
            # parameterized DB lookup, a tiny fixed keyword table, or a
            # numeric regex — never by feeding the raw text to a model. An
            # adversarial answer either matches real master data / a fixed
            # keyword and becomes a real, valid field value, or it matches
            # nothing and gets re-asked, exactly like any garbage input.
            # There is no injection target for it to reach. This override
            # does NOT apply outside PR intake, where TrackingAgent/
            # EscalationAgent free-form answers make the Guardrail's job
            # real again.
            verdict = {"restricted": False, "category": None, "in_scope": True, "reason": verdict.get("reason", "")}
        out["verdict"] = verdict

    if verdict["restricted"]:
        answer = REFUSAL_MESSAGE
        message_id = await store.add_turn(pool, conversation_id, "assistant", answer, intent="blocked", agent_used="Guardrail")
        return ChatResponse(
            conversation_id=conversation_id, turn_id=turn_id, message_id=str(message_id),
            intent="blocked", agent_used="Guardrail", answer=answer,
            blocked=True, blocked_category=verdict["category"], trace=tracer.events,
        )

    if not verdict["in_scope"]:
        answer = OUT_OF_SCOPE_MESSAGE
        message_id = await store.add_turn(pool, conversation_id, "assistant", answer, intent="out_of_scope", agent_used="Guardrail")
        return ChatResponse(
            conversation_id=conversation_id, turn_id=turn_id, message_id=str(message_id),
            intent="out_of_scope", agent_used="Guardrail", answer=answer,
            out_of_scope=True, trace=tracer.events,
        )

    if pr_in_progress:
        decision = {"intent": "file_purchase_requisition", "tracking_number": None, "reason": "continuing an in-progress PR draft"}
    else:
        async with tracer.step("Orchestrator", "classify_intent", {"message": req.message}) as out:
            # orchestrator.classify() calls the LLM SDK synchronously; run it in a
            # thread so it doesn't block the event loop (which would also block
            # this same request's own concurrent GET /trace polling from a UI).
            decision = await asyncio.to_thread(orchestrator.classify, req.message, history)
            out["decision"] = decision

    intent = decision["intent"]
    tracking_number = decision.get("tracking_number") or conversation.get("last_tracking_number")
    pr_number = None
    options = None
    pr_id = None
    history = None

    if intent == "file_purchase_requisition":
        agent_used, answer, pr_number, options, pr_id, history = await handle_pr_draft(pool, tracer, conversation_id, pr_fields, req.message)

    elif intent == "file_complaint":
        ctx = await build_tracking_context(pool, tracer, tracking_number)
        shipment_context = ctx if ctx["found"] else None

        # Ticket intake gate — deterministic, no LLM (same as GRIP's
        # ticket_intake tool): a vague complaint gets a clarifying question
        # instead of an immediately-filed, thin ticket. Scoped to the
        # complaint itself — no "2nd turn overall" shortcut, so an unrelated
        # earlier turn (a greeting, a tracking question) can't skip this.
        async with tracer.step(None, "check_intake_readiness", {
            "questions_asked": conversation.get("clarifying_questions_asked", 0),
            "tracking_number_resolved": ctx["found"],
        }) as out:
            readiness = check_readiness(
                questions_asked=conversation.get("clarifying_questions_asked", 0),
                tracking_number_resolved=ctx["found"],
            )
            out["readiness"] = readiness

        if not readiness["ready"]:
            agent_used = "TicketIntake"
            answer = readiness["question"]
            await store.record_clarifying_question(pool, conversation_id, answer)
            message_id = await store.add_turn(pool, conversation_id, "assistant", answer, intent=intent, agent_used=agent_used)
            return ChatResponse(
                conversation_id=conversation_id, turn_id=turn_id, message_id=str(message_id),
                intent=intent, agent_used=agent_used, answer=answer, trace=tracer.events,
            )

        agent_used = "EscalationAgent"
        async with tracer.step("EscalationAgent", "draft_ticket", {}) as out:
            draft = await asyncio.to_thread(escalation_agent.draft, req.message, shipment_context)
            out["issue_type"] = draft["issue_type"]

        async with tracer.step(None, "compute_priority", {"issue_type": draft["issue_type"]}) as out:
            priority = derive_priority(draft["issue_type"], shipment_context)
            out["priority"] = priority

        shipment_id = ctx["shipment"]["id"] if ctx["found"] else None
        async with tracer.step(None, "db_write:create_support_ticket", {
            "sql": CREATE_SUPPORT_TICKET_SQL,
            "params": [conversation_id, shipment_id, draft["issue_type"], priority, draft["subject"], draft["description"]],
        }) as out:
            ticket_id = await create_support_ticket(
                pool,
                conversation_id=conversation_id,
                shipment_id=shipment_id,
                issue_type=draft["issue_type"],
                priority=priority,
                subject=draft["subject"],
                description=draft["description"],
            )
            out["ticket_id"] = ticket_id

        answer = f"{draft['customer_reply']} (Ticket #{ticket_id})"

    elif intent == "find_vendor":
        agent_used = "VendorAgent"
        # decision.get(...) is absent (not None) on the pr_in_progress
        # short-circuit above, where intent is hardcoded and can never be
        # find_vendor anyway — .get() covers that safely regardless.
        material_query = decision.get("material_query") or req.message

        async with tracer.step(None, "db_query:get_vendor_prices_for_material", {"material_query": material_query}) as out:
            vendor_ctx = await vendor_tools.get_vendor_prices_for_material(pool, material_query)
            out["material_resolved"] = vendor_ctx["material"] is not None
            out["vendor_count"] = len(vendor_ctx["vendors"])

        async with tracer.step("VendorAgent", "compose_answer", {"material_query": material_query}) as out:
            answer = await asyncio.to_thread(vendor_agent.answer, req.message, vendor_ctx)
            out["answer_preview"] = answer[:200]

    else:
        # track_shipment and general_faq share the same grounded-answer path —
        # TrackingAgent's prompt handles "no tracking number" and "not found"
        # branches itself (see app/agents/prompts.py).
        agent_used = "TrackingAgent"
        ctx = await build_tracking_context(pool, tracer, tracking_number)

        async with tracer.step("TrackingAgent", "compose_answer", {"tracking_number": ctx["tracking_number"], "found": ctx["found"]}) as out:
            answer = await asyncio.to_thread(tracking_agent.answer, req.message, ctx)
            out["answer_preview"] = answer[:200]

        if ctx["found"]:
            await store.set_last_tracking_number(pool, conversation_id, ctx["tracking_number"])

    message_id = await store.add_turn(pool, conversation_id, "assistant", answer, intent=intent, agent_used=agent_used)

    return ChatResponse(
        conversation_id=conversation_id,
        turn_id=turn_id,
        message_id=str(message_id),
        intent=intent,
        agent_used=agent_used,
        answer=answer,
        can_raise_ticket=True,
        pr_number=pr_number,
        pr_id=pr_id,
        options=options,
        history=history,
        trace=tracer.events,
    )


@app.post("/feedback", response_model=FeedbackResponse)
async def feedback(req: FeedbackRequest):
    pool = db.get_pool()

    try:
        await store.record_feedback(pool, req.conversation_id, req.message_id, req.helpful)
    except ValueError as exc:
        _value_error_to_400(exc)
    except asyncpg.ForeignKeyViolationError:
        raise HTTPException(status_code=404, detail="Unknown conversation_id or message_id")

    tracer = Tracer(pool, req.conversation_id, str(uuid.uuid4()))
    async with tracer.step(None, "record_feedback", {"message_id": req.message_id, "helpful": req.helpful}):
        pass

    if req.helpful:
        return FeedbackResponse(recorded=True, offer_ticket=False)

    return FeedbackResponse(
        recorded=True,
        offer_ticket=True,
        message="Sorry that wasn't helpful. Want me to raise a support ticket so a person can follow up?",
    )


@app.post("/ticket", response_model=TicketResponse)
async def raise_ticket(req: TicketRequest):
    pool = db.get_pool()

    try:
        conversation = await store.get_conversation(pool, req.conversation_id)
    except ValueError as exc:
        _value_error_to_400(exc)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown conversation_id")

    turn_id = req.turn_id or str(uuid.uuid4())
    tracer = Tracer(pool, req.conversation_id, turn_id)
    async with tracer.step(None, "receive_api_call", {"message_id": req.message_id, "note": req.note}):
        pass

    # The Orchestrator still makes the routing call here, but it's a free,
    # instant decision rather than an LLM call: unlike /chat (freeform text
    # that needs classifying), this endpoint is only ever reached via an
    # explicit UI action ("raise a ticket") — the intent is already known,
    # so there's nothing to classify and no reason to spend an LLM call on it.
    async with tracer.step("Orchestrator", "route_decision", {
        "trigger": "explicit_ticket_request",
        "reason": "No freeform message to classify — intent is already known from the UI action, so routing is deterministic rather than LLM-classified.",
    }) as out:
        out["decision"] = {"route": "EscalationAgent"}

    original_answer = None
    if req.message_id:
        turn = await store.get_turn(pool, req.message_id)
        if turn and turn["role"] == "assistant":
            original_answer = turn["content"]

    complaint_text = "Customer was not satisfied with the assistant's previous response and asked to raise a ticket."
    if original_answer:
        complaint_text += f"\n\nOriginal assistant answer:\n{original_answer}"
    if req.note:
        complaint_text += f"\n\nAdditional detail from customer:\n{req.note}"

    ctx = await build_tracking_context(pool, tracer, conversation.get("last_tracking_number"))
    shipment_context = ctx if ctx["found"] else None

    async with tracer.step("EscalationAgent", "draft_ticket", {"source": "feedback"}) as out:
        draft = await asyncio.to_thread(escalation_agent.draft, complaint_text, shipment_context, req.note)
        out["issue_type"] = draft["issue_type"]

    async with tracer.step(None, "compute_priority", {"issue_type": draft["issue_type"]}) as out:
        priority = derive_priority(draft["issue_type"], shipment_context)
        out["priority"] = priority

    shipment_id = ctx["shipment"]["id"] if ctx["found"] else None
    async with tracer.step(None, "db_write:create_support_ticket", {
        "sql": CREATE_SUPPORT_TICKET_SQL,
        "params": [req.conversation_id, shipment_id, draft["issue_type"], priority, draft["subject"], draft["description"]],
    }) as out:
        ticket_id = await create_support_ticket(
            pool,
            conversation_id=req.conversation_id,
            shipment_id=shipment_id,
            issue_type=draft["issue_type"],
            priority=priority,
            subject=draft["subject"],
            description=draft["description"],
        )
        out["ticket_id"] = ticket_id

    return TicketResponse(
        ticket_id=ticket_id,
        turn_id=turn_id,
        issue_type=draft["issue_type"],
        priority=priority,
        subject=draft["subject"],
        description=draft["description"],
        customer_reply=draft["customer_reply"],
        trace=tracer.events,
    )


@app.get("/conversations/{conversation_id}")
async def get_conversation_detail(conversation_id: str):
    pool = db.get_pool()
    try:
        conversation = await store.get_conversation(pool, conversation_id)
    except ValueError as exc:
        _value_error_to_400(exc)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown conversation_id")
    turns = await store.get_all_turns(pool, conversation_id)
    return {"conversation": conversation, "turns": turns}


@app.get("/trace/{conversation_id}")
async def get_trace(conversation_id: str, turn_id: str | None = None):
    """Poll this (optionally with ?turn_id=... from a ChatResponse/TicketResponse)
    while a /chat or /ticket call is in flight to see steps appear live — each
    step is persisted the moment it completes, before the overall call returns."""
    pool = db.get_pool()
    try:
        events = await store.get_trace_events(pool, conversation_id, turn_id)
    except ValueError as exc:
        _value_error_to_400(exc)
    return {"conversation_id": conversation_id, "turn_id": turn_id, "trace": events}


@app.get("/shipments/{tracking_number}")
async def get_shipment(tracking_number: str):
    """Raw DB lookup, no LLM involved — handy in Postman to see the 'DB query'
    leg of the pipeline in isolation from the 'context to the LLM' leg."""
    pool = db.get_pool()
    result = await get_shipment_status(pool, tracking_number.upper())
    if result is None:
        raise HTTPException(status_code=404, detail="No shipment found for that tracking number")
    return result


@app.get("/customers/{email}/shipments")
async def get_customer_shipments(email: str):
    pool = db.get_pool()
    return {"email": email, "shipments": await lookup_customer_shipments(pool, email)}


def _pr_download_filename(row: dict, suffix: str) -> str:
    """A filesystem/header-safe filename stem from the PR number (which
    contains "/", e.g. "PR/2026/00123") — falls back to the row id if the
    submission itself failed and there's no PR number."""
    stem = row.get("pr_number") or f"PR-{row['id']}"
    return re.sub(r"[^A-Za-z0-9_-]+", "_", stem) + suffix


async def _get_purchase_requisition_or_404(pr_id: int) -> dict:
    pool = db.get_pool()
    row = await store.get_purchase_requisition(pool, pr_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown purchase requisition id")
    return row


@app.get("/purchase-requisitions/{pr_id}/payload")
async def download_pr_payload(pr_id: int):
    """The exact JSON body that was (or would have been) POSTed to the ERP's
    submit-draft-PR endpoint — see app/tools/pr_submit_tool.py."""
    row = await _get_purchase_requisition_or_404(pr_id)
    filename = _pr_download_filename(row, "_payload.json")
    return Response(
        content=json.dumps(row["payload"], indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/purchase-requisitions/{pr_id}/pdf")
async def download_pr_pdf(pr_id: int, inline: bool = False):
    """A neat, human-readable one-pager for this PR — see app/tools/pr_pdf_tool.py.
    `?inline=true` opens it in the browser's own PDF viewer instead of forcing
    a save-to-disk — that's the chat UI's "Preview" button; same bytes
    either way, only Content-Disposition changes."""
    row = await _get_purchase_requisition_or_404(pr_id)
    pdf_bytes = build_pr_pdf(row)
    filename = _pr_download_filename(row, ".pdf")
    disposition = "inline" if inline else "attachment"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


# Chat UI (frontend/index.html + static assets). Mounted last so it only
# catches requests none of the API routes above matched.
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
