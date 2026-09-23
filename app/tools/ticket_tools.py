"""Write-side tool: files a support ticket, plus the deterministic priority
calculation that goes with it.

Priority is NEVER decided by the LLM (same reasoning as GRIP's
TicketPriorityCalculator) — a fixed base priority per issue_type, escalated
by one concrete, DB-derived signal (how overdue the shipment actually is).
This is a plain function precisely so the same complaint always yields the
same priority, and so it's auditable independent of any model's mood.
"""
import asyncpg
from datetime import date

from app.utils import to_uuid

CREATE_SUPPORT_TICKET_SQL = (
    "INSERT INTO support_tickets (conversation_id, shipment_id, issue_type, priority, subject, description) "
    "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id"
)

_BASE_PRIORITY = {
    "lost": "urgent",
    "damaged": "high",
    "wrong_address": "medium",
    "delayed": "medium",
    "unsatisfactory_response": "low",
    "other": "medium",
}


def derive_priority(issue_type: str, shipment_context: dict | None) -> str:
    """shipment_context is the {tracking_number, found, shipment, events}
    envelope built by app.main.build_tracking_context — same shape the
    agents get, so this function is grounded in the same real data."""
    priority = _BASE_PRIORITY.get(issue_type, "medium")

    if issue_type == "delayed" and shipment_context and shipment_context.get("found"):
        shipment = shipment_context.get("shipment") or {}
        estimated = shipment.get("estimated_delivery")
        actual = shipment.get("actual_delivery")
        if estimated and not actual:
            overdue_days = (date.today() - estimated).days
            if overdue_days >= 3:
                priority = "urgent"
            elif overdue_days >= 1:
                priority = "high"

    return priority


async def create_support_ticket(
    pool: asyncpg.Pool,
    *,
    conversation_id: str | None,
    shipment_id: int | None,
    issue_type: str,
    priority: str,
    subject: str,
    description: str,
) -> int:
    row = await pool.fetchrow(
        CREATE_SUPPORT_TICKET_SQL,
        to_uuid(conversation_id) if conversation_id else None,
        shipment_id,
        issue_type,
        priority,
        subject,
        description,
    )
    return row["id"]
