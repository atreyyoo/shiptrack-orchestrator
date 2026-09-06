"""ShipTrack orchestrator — FastAPI entrypoint.

Flow per requirement: API call (Postman) -> DB query (Postgres) -> context to
the LLM (agents, each with its own system prompt) -> output. Every step is
traced live to the console and persisted to `trace_events`.
"""
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from app import db, store
from app.agents.escalation_agent import EscalationAgent
from app.agents.guardrail_agent import OUT_OF_SCOPE_MESSAGE, REFUSAL_MESSAGE, GuardrailAgent
from app.agents.orchestrator import OrchestratorAgent
from app.agents.tracking_agent import TrackingAgent
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
from app.tools.shipment_tools import GET_SHIPMENT_SQL, get_shipment_status, lookup_customer_shipments
from app.tools.ticket_intake_tool import check_readiness
from app.tools.ticket_tools import CREATE_SUPPORT_TICKET_SQL, create_support_ticket, derive_priority
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    logger.info("ShipTrack orchestrator ready — LLM mode=%s deployment=%s", llm_client.active_mode, llm_client.deployment)
    yield
    await db.disconnect()


app = FastAPI(title="ShipTrack Orchestrator", lifespan=lifespan)


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

    async with tracer.step("Orchestrator", "classify_intent", {"message": req.message}) as out:
        # orchestrator.classify() calls the LLM SDK synchronously; run it in a
        # thread so it doesn't block the event loop (which would also block
        # this same request's own concurrent GET /trace polling from a UI).
        decision = await asyncio.to_thread(orchestrator.classify, req.message, history)
        out["decision"] = decision

    intent = decision["intent"]
    tracking_number = decision.get("tracking_number") or conversation.get("last_tracking_number")

    if intent == "file_complaint":
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


# Chat UI (frontend/index.html + static assets). Mounted last so it only
# catches requests none of the API routes above matched.
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
