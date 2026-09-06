"""Deterministic, read-only Postgres tools the agents ground their answers in.

SQL is kept as named, single-line constants (rather than inline strings) so
the exact query text can be exported and shown in the live trace alongside
the parameters it ran with — see how these are used in app/main.py's
tracer.step(...) payloads.
"""
import asyncpg

GET_SHIPMENT_SQL = (
    "SELECT s.id, s.tracking_number, s.carrier, s.origin, s.destination, s.status, "
    "s.estimated_delivery, s.actual_delivery, c.name AS customer_name, c.email AS customer_email "
    "FROM shipments s JOIN customers c ON c.id = s.customer_id WHERE s.tracking_number = $1"
)

GET_SHIPMENT_EVENTS_SQL = (
    "SELECT event_type, location, occurred_at, note FROM shipment_events "
    "WHERE shipment_id = $1 ORDER BY occurred_at ASC"
)

LOOKUP_CUSTOMER_SHIPMENTS_SQL = (
    "SELECT s.tracking_number, s.carrier, s.status, s.estimated_delivery, s.actual_delivery "
    "FROM shipments s JOIN customers c ON c.id = s.customer_id "
    "WHERE c.email = $1 ORDER BY s.created_at DESC"
)


async def get_shipment_status(pool: asyncpg.Pool, tracking_number: str) -> dict | None:
    """Shipment header row + its full event history, or None if not found."""
    shipment = await pool.fetchrow(GET_SHIPMENT_SQL, tracking_number)
    if shipment is None:
        return None

    events = await pool.fetch(GET_SHIPMENT_EVENTS_SQL, shipment["id"])

    return {
        "shipment": dict(shipment),
        "events": [dict(e) for e in events],
    }


async def get_shipment_history(pool: asyncpg.Pool, tracking_number: str) -> list[dict]:
    """Just the event timeline, oldest first."""
    rows = await pool.fetch(
        """
        SELECT se.event_type, se.location, se.occurred_at, se.note
        FROM shipment_events se
        JOIN shipments s ON s.id = se.shipment_id
        WHERE s.tracking_number = $1
        ORDER BY se.occurred_at ASC
        """,
        tracking_number,
    )
    return [dict(r) for r in rows]


async def lookup_customer_shipments(pool: asyncpg.Pool, email: str) -> list[dict]:
    rows = await pool.fetch(LOOKUP_CUSTOMER_SHIPMENTS_SQL, email)
    return [dict(r) for r in rows]
