"""Grounded vendor-price lookups — same "DB is ground truth" rule as
pr_master_data_tools.py. Backs two things:
  1. Standalone "find me the cheapest vendor for X" questions (VendorAgent) —
     free text resolved to a material, then priced by every vendor selling
     it, cheapest first.
  2. The PR-intake "vendor" step (app/tools/pr_intake_tool.py) — once
     Material has already resolved to a real material_code, the same
     vendor/price rows back a clickable options menu and the answer's
     resolution, scoped to that exact material (no free-text material
     matching needed a second time).
VendorAgent and DraftPRAgent only ever compose around / plug in these rows —
neither ever picks or invents a vendor or a price itself.
"""
import asyncpg

from app.tools.pr_master_data_tools import resolve_material

GET_VENDOR_PRICES_SQL = """
    SELECT v.vendor_code, v.vendor_name, p.price, p.uom_code, p.lead_time_days
    FROM pr_vendor_material_prices p
    JOIN pr_vendors v ON v.vendor_code = p.vendor_code
    WHERE p.material_code = $1
    ORDER BY p.price ASC
"""

RESOLVE_VENDOR_SQL = """
    SELECT v.vendor_code, v.vendor_name, p.price, p.uom_code, p.lead_time_days
    FROM pr_vendor_material_prices p
    JOIN pr_vendors v ON v.vendor_code = p.vendor_code
    WHERE p.material_code = $2
      AND (v.vendor_code ILIKE $1 OR v.vendor_name ILIKE '%' || $1 || '%')
    ORDER BY
        CASE
            WHEN v.vendor_code ILIKE $1 THEN 0
            WHEN v.vendor_name ILIKE $1 THEN 1
            ELSE 2
        END,
        p.price ASC
    LIMIT 1
"""


async def get_vendors_for_material_code(pool: asyncpg.Pool, material_code: str) -> list[dict]:
    """Every vendor with a price on file for an already-resolved material_code,
    cheapest first — no free-text matching, the material is already known.
    [] if nobody has priced it (a legitimate, common case — most materials in
    this POC's seed data have no vendor pricing yet; callers should treat
    that as "no vendor step to offer", not an error)."""
    rows = await pool.fetch(GET_VENDOR_PRICES_SQL, material_code)
    return [dict(r) for r in rows]


async def resolve_vendor(pool: asyncpg.Pool, text: str, material_code: str | None) -> dict | None:
    """Resolves the customer's answer to the vendor question against the
    vendors actually selling `material_code` — the same containment-match
    rule as pr_master_data_tools (exact code first, then name contains
    answer). None if material_code is unset or nothing matched, exactly like
    every other pr_intake_tool resolver — the caller re-asks rather than
    guessing a vendor."""
    if not material_code:
        return None
    row = await pool.fetchrow(RESOLVE_VENDOR_SQL, text.strip(), material_code)
    return dict(row) if row else None


async def get_vendor_prices_for_material(pool: asyncpg.Pool, material_text: str) -> dict:
    """Resolves `material_text` (free text, e.g. "steel") to a real material
    row, then every vendor selling it, cheapest first. Used by the standalone
    "find me the cheapest vendor" question (VendorAgent) — the PR-intake
    vendor step uses get_vendors_for_material_code() directly instead, since
    it already has a resolved material_code and would otherwise re-run (and
    risk re-ambiguating) the free-text match.

    Returns {"material": dict|None, "vendors": list[dict]}:
      - material is None when nothing in pr_materials matched — the caller
        should say so rather than guess.
      - vendors is [] when the material resolved but no vendor has a price
        on file for it yet — a materially different case from "not found",
        so VendorAgent's prompt is told to distinguish the two.
    """
    material = await resolve_material(pool, material_text)
    if material is None:
        return {"material": None, "vendors": []}
    vendors = await get_vendors_for_material_code(pool, material["material_code"])
    return {"material": material, "vendors": vendors}
