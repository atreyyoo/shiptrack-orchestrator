from typing import Any, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    conversation_id: Optional[str] = Field(None, description="Omit to start a new conversation.")
    turn_id: Optional[str] = Field(None, description="Client-generated id for this one call, so a frontend can poll GET /trace/{conversation_id}?turn_id=... while this request is in flight. Auto-generated if omitted.")
    customer_email: Optional[str] = Field(None, description="Optional — helps resolve 'my shipments' style questions.")
    message: str


class ChatOption(BaseModel):
    """One clickable choice for a fixed-choice PR-intake question (see
    app/tools/pr_intake_tool.py:options_for()). Sending `value` back as the
    next /chat message is equivalent to a customer typing it — it resolves
    the same way either path is taken."""
    value: str
    label: str


class ChatResponse(BaseModel):
    conversation_id: str
    turn_id: str
    message_id: str
    intent: str
    agent_used: str
    answer: str
    can_raise_ticket: bool = True
    pr_number: Optional[str] = Field(None, description="Set once the Draft PR Agent has submitted a PR this turn.")
    pr_id: Optional[int] = Field(None, description="purchase_requisitions row id — use with GET /purchase-requisitions/{pr_id}/pdf or /payload to download it.")
    options: Optional[list[ChatOption]] = Field(
        None, description="Present when `answer` is a PR-intake question with a fixed set of choices — render as a clickable menu. Absent/null for free-text questions."
    )
    history: Optional[list[ChatOption]] = Field(
        None, description="Present when `answer` is a PR-intake question that has a usage-history dropdown (job/warehouse/material/wbs) — [] if the field has one but no entries yet, null if this field doesn't track history at all."
    )
    blocked: bool = False
    blocked_category: Optional[str] = None
    out_of_scope: bool = False
    trace: list[dict[str, Any]]


class FeedbackRequest(BaseModel):
    conversation_id: str
    message_id: str
    helpful: bool


class FeedbackResponse(BaseModel):
    recorded: bool
    offer_ticket: bool
    message: Optional[str] = None


class TicketRequest(BaseModel):
    conversation_id: str
    turn_id: Optional[str] = Field(None, description="Client-generated id for this one call, for live trace polling. Auto-generated if omitted.")
    message_id: Optional[str] = Field(None, description="The assistant message the customer is unhappy with, if any.")
    note: Optional[str] = Field(None, description="Extra detail from the customer about the issue.")


class TicketResponse(BaseModel):
    ticket_id: int
    turn_id: str
    issue_type: str
    priority: str
    subject: str
    description: str
    customer_reply: str
    trace: list[dict[str, Any]]
