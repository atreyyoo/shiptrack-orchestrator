"""Deterministic intake for the Draft PR Agent — same "fixed questions, no
LLM" spirit as ticket_intake_tool.py, extended one step further: each answer
is resolved and stored under its own field name (not just counted), because
a PR draft needs several independent, machine-typed field values rather than
a single readiness threshold.

Field order is fixed for this POC (ask Job first, then Warehouse, ... then
Quantity last) — a real slot-filler would allow fields to arrive in any order
(e.g. from a document upload), but that's out of scope here; see the README's
"Extending this" section.

Every field here is resolved against Postgres — app/tools/pr_master_data_
tools.py — never guessed by an LLM: job/warehouse/material/wbs against real
master data, and pr_category/planning_category/supply_at against small
reference tables (pr_category_options etc.) rather than a Python constant,
so fixing a placeholder code later is a DB UPDATE, not a redeploy. The three
reference-table fields have a genuinely fixed, small set of choices, so —
unlike the master-data fields — the frontend renders them as a clickable
menu (see options_for()); free-text typing still works too, matched against
the same DB-backed option list. Only Quantity has no DB counterpart at all —
it's a fresh number the customer supplies each time, not a lookup.

Vendor sits between Material and WBS — it's a fourth kind of field, distinct
from both groups above: it gets a clickable options menu like the
reference-table fields (see options_for()), but the option set itself is
neither fixed nor free-text-matched against the whole catalog — it's the
real vendors who actually price the *just-resolved* Material, scoped by
material_code (app/tools/vendor_tools.py), the same way WBS is scoped to the
already-resolved Job. If nobody has priced that material yet, there's
nothing to choose between — see app/main.py's handling of this field for how
that auto-skips the question entirely rather than asking one with zero
options.
"""
import re

from app.tools import pr_master_data_tools as master_data
from app.tools import vendor_tools

FIELD_ORDER = [
    "job",
    "warehouse",
    "pr_category",
    "planning_category",
    "supply_at",
    "material",
    "vendor",
    "wbs",
    "quantity",
]

# Fields with a per-value usage history (app/store.py:record_field_history /
# get_field_history) — the free-text, grounded-master-data lookups only.
# Deliberately excludes the reference-table fields (they already have a
# fixed options menu — see options_for()) and quantity (a fresh number each
# time, not a reusable identifier).
HISTORY_FIELDS = {"job", "warehouse", "material", "wbs"}

# Fields backed by a pr_*_options reference table rather than a master-data
# lookup — see options_for()/resolve_field() below.
OPTION_FIELDS = {"pr_category", "planning_category", "supply_at"}

_STATIC_QUESTIONS = {
    "job": "Which job is this Purchase Requisition for? A job code or job name works.",
    "warehouse": "Which warehouse should this be raised against?",
    "pr_category": "Is this Revenue or Capital?",
    "planning_category": "What planning category does this fall under?",
    "supply_at": "Where's the material being supplied from — your own premises, or the vendor's?",
    "material": "What material do you need? A material code or description works.",
    "vendor": "Which vendor would you like to use for this material?",
    "wbs": "Which WBS element should this be booked to?",
    "quantity": "What quantity do you need?",
}

_QTY_RE = re.compile(r"[\d,]+(?:\.\d+)?")


def next_missing_field(fields: dict) -> str | None:
    """First field in FIELD_ORDER not yet present in `fields`. Any extra keys
    (e.g. the "_status" sentinel set when intake starts) are simply ignored."""
    for name in FIELD_ORDER:
        if name not in fields:
            return name
    return None


def question_for(field: str, fields: dict) -> str:
    """The question text for `field`, given what's already been resolved.
    "quantity" and "vendor" are dynamic — quantity names the material and
    states how much is actually in stock (app/store.py:decrement_material_
    stock() enforces this at submission), so the stock check that follows
    isn't a surprise; vendor names the material the price list below is for.
    Both fall back to the static text if the data they'd need isn't there —
    vendor's fallback never actually surfaces in practice since app/main.py
    only asks it once Material has resolved, but it's kept as a safety net
    consistent with how every other field looks itself up."""
    if field == "quantity":
        material = fields.get("material") or {}
        available = material.get("available_qty")
        description = material.get("description")
        if available is not None and description:
            return f"What quantity of {description} do you need? ({fmt_number(available)} {material.get('default_uom_code', '')} available)".strip()
    if field == "vendor":
        description = (fields.get("material") or {}).get("description")
        if description:
            return f"Which vendor would you like to use for {description}? Prices are shown below, cheapest first."
    return _STATIC_QUESTIONS[field]


def fmt_number(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f == int(f) else f"{f:.2f}"


async def options_for(pool, field: str, fields: dict) -> list[dict] | None:
    """[{"value": ..., "label": ...}] for a field the caller can render as a
    clickable menu — clicking sends `value` straight back as the chat
    message, so it's guaranteed to resolve. None for a free-text/grounded-
    lookup field (job, warehouse, material, wbs) or quantity.

    For the three OPTION_FIELDS this is the whole fixed reference table.
    For "vendor" it's the real vendors pricing the already-resolved Material
    (`fields["material"]`) — None if that's missing (asked out of order, or
    material.resolve() somehow didn't run) or if nobody has priced it, same
    as any other "nothing to offer" case; app/main.py treats a None options
    result for "vendor" as "auto-skip this field", it does not re-ask it."""
    if field == "pr_category":
        rows = await master_data.get_pr_category_options(pool)
        return [{"value": row["label"], "label": row["label"]} for row in rows]
    if field == "planning_category":
        rows = await master_data.get_planning_category_options(pool)
        return [{"value": row["label"], "label": row["label"]} for row in rows]
    if field == "supply_at":
        rows = await master_data.get_supply_at_options(pool)
        return [{"value": row["label"], "label": row["label"]} for row in rows]
    if field == "vendor":
        material = fields.get("material") or {}
        material_code = material.get("material_code")
        if not material_code:
            return None
        rows = await vendor_tools.get_vendors_for_material_code(pool, material_code)
        if not rows:
            return None
        return [{"value": row["vendor_name"], "label": _vendor_option_label(row)} for row in rows]
    return None


def _vendor_option_label(row: dict) -> str:
    label = f"{row['vendor_name']} — {fmt_number(row['price'])} {row['uom_code']}"
    lead_time = row.get("lead_time_days")
    if lead_time is not None:
        label += f" ({lead_time}d lead time)"
    return label


def _parse_quantity(text: str) -> float | None:
    match = _QTY_RE.search(text)
    if not match:
        return None
    try:
        value = float(match.group(0).replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def history_entry(field: str, resolved) -> tuple[str, str] | None:
    """(value, label) to record in that field's usage history (see
    app/store.py:record_field_history), or None for a field not in
    HISTORY_FIELDS. `value` is the canonical code — recording anything else
    would mean a clicked history entry might not resolve the same way twice."""
    if field == "job":
        return resolved["job_code"], f"{resolved['job_code']} — {resolved['job_name']}"
    if field == "warehouse":
        return resolved["warehouse_code"], f"{resolved['warehouse_code']} — {resolved['warehouse_name']}"
    if field == "material":
        available = resolved.get("available_qty")
        uom = resolved.get("default_uom_code", "")
        stock_note = f" ({fmt_number(available)} {uom} available)" if available is not None else ""
        return resolved["material_code"], f"{resolved['material_code']} — {resolved['description']}{stock_note}"
    if field == "wbs":
        return resolved["wbs_code"], f"{resolved['wbs_code']} — {resolved['description']}"
    return None


async def resolve_field(pool, field: str, text: str, fields: dict):
    """Resolves `text` (the customer's raw answer) into the value for `field`,
    or None if it couldn't be matched — the caller re-asks the same question
    on None rather than guessing. `fields` is the slot-fill state gathered so
    far, needed for fields resolved relative to an earlier answer (WBS is
    scoped to the already-resolved job)."""
    if field == "job":
        return await master_data.resolve_job(pool, text)
    if field == "warehouse":
        return await master_data.resolve_warehouse(pool, text)
    if field == "pr_category":
        return await master_data.resolve_pr_category(pool, text)
    if field == "planning_category":
        return await master_data.resolve_planning_category(pool, text)
    if field == "supply_at":
        return await master_data.resolve_supply_at(pool, text)
    if field == "material":
        return await master_data.resolve_material(pool, text)
    if field == "vendor":
        material = fields.get("material") or {}
        return await vendor_tools.resolve_vendor(pool, text, material.get("material_code"))
    if field == "wbs":
        job = fields.get("job") or {}
        return await master_data.resolve_wbs(pool, text, job.get("job_code"))
    if field == "quantity":
        return _parse_quantity(text)
    raise ValueError(f"Unknown PR intake field: {field!r}")
