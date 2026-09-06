"""Deterministic ticket-intake gate — decides whether there's enough context
to file a ticket yet, and if not, which fixed clarifying question to ask
next.

Mirrors GRIP's ticket_intake tool: in GRIP too, this is a plain tool, not an
LLM agent — the question order is fixed and the readiness rule is a simple
counter, precisely so a complaint can't be filed from a single vague
message ("I want to complain") with nothing for a human to act on.
"""

CLARIFYING_QUESTIONS = [
    "Which shipment is this about? A tracking number would help, if you have one.",
    "Can you describe what happened in a bit more detail?",
    "Is there anything else that would help us resolve this?",
]

MAX_CLARIFYING_QUESTIONS = len(CLARIFYING_QUESTIONS)


def check_readiness(*, questions_asked: int, tracking_number_resolved: bool) -> dict:
    """Returns {"ready": bool, "question": str|None}.

    Deliberately scoped to the complaint itself, not the whole conversation:
    no "2nd turn overall" shortcut, because that counted unrelated earlier
    turns (a greeting, an unrelated tracking question) as progress on a
    complaint that hadn't even started yet. Ready only once a real shipment
    is resolved, or once every fixed question has actually been asked.
    """
    ready = tracking_number_resolved or questions_asked >= MAX_CLARIFYING_QUESTIONS
    if ready:
        return {"ready": True, "question": None}
    return {"ready": False, "question": CLARIFYING_QUESTIONS[questions_asked]}
