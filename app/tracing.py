"""Live, structured tracing of every orchestrator/agent/tool step.

Two outputs from one call site:
  1. Printed live to the console as each step starts and finishes — this is
     what makes the orchestrator's decisions visible while a Postman request
     is in flight.
  2. Persisted to the `trace_events` table so the same steps can be replayed
     afterwards via GET /trace/{conversation_id}.

Usage:
    async with tracer.step("Orchestrator", "classify_intent", {"message": message}) as out:
        decision = orchestrator.classify(message, history)
        out["decision"] = decision
"""
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from app.utils import to_uuid

logger = logging.getLogger("shiptrack.trace")


def _short(value: Any, limit: int = 300) -> Any:
    """Truncate long strings in trace payloads so console lines stay readable."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...(+{len(value) - limit} chars)"
    if isinstance(value, dict):
        return {k: _short(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_short(v, limit) for v in value]
    return value


class Tracer:
    """One Tracer per API call (i.e. per /chat or /ticket request). `turn_id`
    scopes this call's steps so a frontend polling GET /trace/{conversation_id}
    ?turn_id=... while the call is in flight sees only *this* call's steps,
    not older ones from earlier messages in the same conversation."""

    def __init__(self, pool: asyncpg.Pool, conversation_id: str, turn_id: str):
        self.pool = pool
        self.conversation_id = conversation_id
        self.turn_id = turn_id
        self.step_no = 0
        self.events: list[dict] = []

    def _print(self, step_no: int, agent: str | None, action: str, status: str, payload: dict | None, duration_ms: int | None) -> None:
        parts = [f"conv={self.conversation_id}", f"turn={self.turn_id}", f"step={step_no}"]
        if agent:
            parts.append(f"agent={agent}")
        parts.append(f"action={action}")
        parts.append(f"status={status}")
        if duration_ms is not None:
            parts.append(f"({duration_ms}ms)")
        line = " ".join(parts)
        if payload:
            line += " " + json.dumps(_short(payload), default=str)
        logger.info(line)

    @asynccontextmanager
    async def step(self, agent: str | None, action: str, payload: dict | None = None):
        """Trace one step. Yields a dict the caller can enrich with results
        before the block exits — that dict is merged into the final payload."""
        self.step_no += 1
        step_no = self.step_no
        start_payload = dict(payload or {})
        self._print(step_no, agent, action, "start", start_payload, None)

        out: dict = {}
        started = time.perf_counter()
        try:
            yield out
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            error_payload = {**start_payload, "error": str(exc)}
            self._print(step_no, agent, action, "error", error_payload, duration_ms)
            await self._persist(step_no, agent, action, error_payload, duration_ms)
            raise
        else:
            duration_ms = int((time.perf_counter() - started) * 1000)
            final_payload = {**start_payload, **out}
            self._print(step_no, agent, action, "done", final_payload, duration_ms)
            await self._persist(step_no, agent, action, final_payload, duration_ms)

    async def _persist(self, step_no: int, agent: str | None, action: str, payload: dict, duration_ms: int) -> None:
        event = {"step": step_no, "agent": agent, "action": action, "payload": payload, "duration_ms": duration_ms}
        self.events.append(event)
        try:
            await self.pool.execute(
                """
                INSERT INTO trace_events (conversation_id, turn_id, step, agent, action, payload, duration_ms)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                to_uuid(self.conversation_id),
                to_uuid(self.turn_id),
                step_no,
                agent,
                action,
                payload,
                duration_ms,
            )
        except Exception:
            # Tracing must never break the actual answer path.
            logger.exception("failed to persist trace event (non-fatal)")
