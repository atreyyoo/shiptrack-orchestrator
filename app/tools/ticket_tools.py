"""Write-side tool: files a support ticket."""
import asyncpg

from app.utils import to_uuid

CREATE_SUPPORT_TICKET_SQL = (
    "INSERT INTO support_tickets (conversation_id, shipment_id, issue_type, priority, subject, description) "
    "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id"
)


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
