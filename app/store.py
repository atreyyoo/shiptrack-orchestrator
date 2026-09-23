"""Conversation/turn/feedback/ticket persistence — everything that isn't the
shipment domain tables lives here."""
import uuid

import asyncpg

from app.utils import to_uuid


_CONVERSATION_COLUMNS = (
    "id, customer_email, last_tracking_number, clarifying_questions_asked, "
    "clarifying_questions, pr_draft_fields"
)


async def get_or_create_conversation(pool: asyncpg.Pool, conversation_id: str | None, customer_email: str | None) -> dict:
    if conversation_id:
        cid = to_uuid(conversation_id)
        row = await pool.fetchrow(f"SELECT {_CONVERSATION_COLUMNS} FROM conversations WHERE id = $1", cid)
        if row:
            if customer_email and not row["customer_email"]:
                await pool.execute("UPDATE conversations SET customer_email = $1 WHERE id = $2", customer_email, cid)
                data = dict(row)
                data["customer_email"] = customer_email
                return data
            return dict(row)
        row = await pool.fetchrow(
            f"""
            INSERT INTO conversations (id, customer_email) VALUES ($1, $2)
            RETURNING {_CONVERSATION_COLUMNS}
            """,
            cid,
            customer_email,
        )
        return dict(row)

    row = await pool.fetchrow(
        f"""
        INSERT INTO conversations (customer_email) VALUES ($1)
        RETURNING {_CONVERSATION_COLUMNS}
        """,
        customer_email,
    )
    return dict(row)


async def get_conversation(pool: asyncpg.Pool, conversation_id: str) -> dict | None:
    row = await pool.fetchrow(
        f"SELECT {_CONVERSATION_COLUMNS}, created_at FROM conversations WHERE id = $1",
        to_uuid(conversation_id),
    )
    return dict(row) if row else None


async def set_last_tracking_number(pool: asyncpg.Pool, conversation_id: str, tracking_number: str) -> None:
    await pool.execute(
        "UPDATE conversations SET last_tracking_number = $1 WHERE id = $2",
        tracking_number,
        to_uuid(conversation_id),
    )


async def record_clarifying_question(pool: asyncpg.Pool, conversation_id: str, question: str) -> None:
    """Bumps the counter and appends the question text — see
    app/tools/ticket_intake_tool.py for how these get read back."""
    await pool.execute(
        """
        UPDATE conversations
        SET clarifying_questions_asked = clarifying_questions_asked + 1,
            clarifying_questions = clarifying_questions || $2::jsonb
        WHERE id = $1
        """,
        to_uuid(conversation_id),
        [question],
    )


async def start_pr_draft(pool: asyncpg.Pool, conversation_id: str) -> None:
    """Marks PR intake as started for this conversation — see
    app/tools/pr_intake_tool.py. The "_status" sentinel is what lets
    app/main.py tell "never started" (pr_draft_fields == {}) apart from
    "started but no field resolved yet" on the very next turn."""
    await pool.execute(
        "UPDATE conversations SET pr_draft_fields = '{\"_status\": \"in_progress\"}'::jsonb WHERE id = $1",
        to_uuid(conversation_id),
    )


async def update_pr_draft_field(pool: asyncpg.Pool, conversation_id: str, field: str, value) -> None:
    """Merges one resolved field into pr_draft_fields (jsonb `||` overwrites
    on key conflict, so this is safe to call once per field, in any order)."""
    await pool.execute(
        "UPDATE conversations SET pr_draft_fields = pr_draft_fields || $2::jsonb WHERE id = $1",
        to_uuid(conversation_id),
        {field: value},
    )


async def reset_pr_draft(pool: asyncpg.Pool, conversation_id: str) -> None:
    """Clears PR intake state once a draft has been submitted (or failed),
    so the next PR request in this conversation starts clean."""
    await pool.execute(
        "UPDATE conversations SET pr_draft_fields = '{}'::jsonb WHERE id = $1",
        to_uuid(conversation_id),
    )


async def record_purchase_requisition(
    pool: asyncpg.Pool,
    *,
    conversation_id: str | None,
    pr_number: str | None,
    status: str,
    payload: dict,
    response: dict,
    fields_snapshot: dict,
) -> int:
    row = await pool.fetchrow(
        """
        INSERT INTO purchase_requisitions (conversation_id, pr_number, status, payload, response, fields_snapshot)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        to_uuid(conversation_id) if conversation_id else None,
        pr_number,
        status,
        payload,
        response,
        fields_snapshot,
    )
    return row["id"]


async def get_purchase_requisition(pool: asyncpg.Pool, pr_id: int) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT id, conversation_id, pr_number, status, payload, response, fields_snapshot, created_at
        FROM purchase_requisitions WHERE id = $1
        """,
        pr_id,
    )
    return dict(row) if row else None


async def record_field_history(pool: asyncpg.Pool, field: str, value: str, label: str) -> None:
    """Upserts one (field, value) usage — see pr_field_history in
    db/01_schema.sql. Called every time a PR-intake lookup field resolves
    (app/tools/pr_intake_tool.py:HISTORY_FIELDS), success or not doesn't
    matter here — only successful resolutions ever reach this call."""
    await pool.execute(
        """
        INSERT INTO pr_field_history (field, value, label, use_count, last_used_at)
        VALUES ($1, $2, $3, 1, now())
        ON CONFLICT (field, value) DO UPDATE
        SET label = EXCLUDED.label, use_count = pr_field_history.use_count + 1, last_used_at = now()
        """,
        field,
        value,
        label,
    )


async def get_field_history(pool: asyncpg.Pool, field: str, limit: int = 8) -> list[dict]:
    rows = await pool.fetch(
        "SELECT value, label FROM pr_field_history WHERE field = $1 ORDER BY last_used_at DESC LIMIT $2",
        field,
        limit,
    )
    return [dict(r) for r in rows]


async def decrement_material_stock(pool: asyncpg.Pool, material_code: str, qty: float) -> bool:
    """Reduces pr_materials.available_qty by `qty` — called once a PR
    referencing this material actually submits (app/main.py), simulating the
    material getting "secured" for that PR. Conditional on the row's own
    current value (`WHERE available_qty >= $2`) so this one UPDATE is atomic
    against a concurrent decrement, on top of the CHECK constraint that
    stops it going negative outright. Returns whether it actually happened —
    app/main.py already checks availability before submitting, so a False
    here means a rare race, not the normal rejection path (that's handled
    earlier, before the ERP call, with a proper "not enough stock" message)."""
    result = await pool.execute(
        "UPDATE pr_materials SET available_qty = available_qty - $2 WHERE material_code = $1 AND available_qty >= $2",
        material_code,
        qty,
    )
    return result == "UPDATE 1"


async def add_turn(
    pool: asyncpg.Pool, conversation_id: str, role: str, content: str, intent: str | None = None, agent_used: str | None = None
) -> uuid.UUID:
    row = await pool.fetchrow(
        """
        INSERT INTO conversation_turns (conversation_id, role, content, intent, agent_used)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        to_uuid(conversation_id),
        role,
        content,
        intent,
        agent_used,
    )
    return row["id"]


async def get_recent_turns(pool: asyncpg.Pool, conversation_id: str, limit: int = 6) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT role, content FROM conversation_turns
        WHERE conversation_id = $1
        ORDER BY created_at ASC
        LIMIT $2
        """,
        to_uuid(conversation_id),
        limit,
    )
    return [dict(r) for r in rows]


async def get_all_turns(pool: asyncpg.Pool, conversation_id: str) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT id, role, content, intent, agent_used, created_at
        FROM conversation_turns
        WHERE conversation_id = $1
        ORDER BY created_at ASC
        """,
        to_uuid(conversation_id),
    )
    return [dict(r) for r in rows]


async def get_turn(pool: asyncpg.Pool, message_id: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT id, conversation_id, role, content, intent, agent_used FROM conversation_turns WHERE id = $1",
        to_uuid(message_id),
    )
    return dict(row) if row else None


async def record_feedback(pool: asyncpg.Pool, conversation_id: str, message_id: str, helpful: bool) -> None:
    await pool.execute(
        "INSERT INTO message_feedback (message_id, conversation_id, helpful) VALUES ($1, $2, $3)",
        to_uuid(message_id),
        to_uuid(conversation_id),
        helpful,
    )


async def get_trace_events(pool: asyncpg.Pool, conversation_id: str, turn_id: str | None = None) -> list[dict]:
    if turn_id:
        rows = await pool.fetch(
            """
            SELECT turn_id, step, agent, action, payload, duration_ms, created_at
            FROM trace_events
            WHERE conversation_id = $1 AND turn_id = $2
            ORDER BY step ASC
            """,
            to_uuid(conversation_id),
            to_uuid(turn_id),
        )
    else:
        rows = await pool.fetch(
            """
            SELECT turn_id, step, agent, action, payload, duration_ms, created_at
            FROM trace_events
            WHERE conversation_id = $1
            ORDER BY created_at ASC, step ASC
            """,
            to_uuid(conversation_id),
        )
    return [dict(r) for r in rows]
