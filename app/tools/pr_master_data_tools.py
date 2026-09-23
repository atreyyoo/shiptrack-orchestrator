"""Grounded PR field lookups — free text from the customer resolved against
real master-data rows in Postgres, mirroring shipment_tools.py's rule: the DB
is ground truth, never invented. Used by app/tools/pr_intake_tool.py to turn
"steel" or "the main warehouse" into the exact codes the submit-draft-PR
payload needs — an LLM is never asked to produce one of these codes itself.

Each query matches four ways, ranked best-first, so a natural sentence like
"here is the job code LE24M128" or "I need some steel packing wire" resolves
without requiring the bare code/name:
  0. the whole answer IS the code (exact, case-insensitive)
  1. the answer CONTAINS the code somewhere in it (a longer/more specific
     code wins over a short one that happens to be a substring of something
     else in the sentence)
  2. the code's free-text name/description CONTAINS the answer
  3. the answer CONTAINS the code's free-text name/description

This is substring containment in both directions, not real fuzzy/semantic
matching — very different phrasing (synonyms, reordered words, a partial
description) still won't resolve; it just re-asks. Good enough for this POC,
not a replacement for real fuzzy search if that turns out to matter.

For this POC these are plain Postgres tables seeded in db/02_seed.sql; swap
the SQL here for a live ERP master-data call when one is available.
"""
import asyncpg

RESOLVE_JOB_SQL = """
    SELECT job_code, job_serial, job_name FROM pr_jobs
    WHERE job_code ILIKE $1
       OR job_name ILIKE '%' || $1 || '%'
       OR $1 ILIKE '%' || job_code || '%'
       OR $1 ILIKE '%' || job_name || '%'
    ORDER BY
        CASE
            WHEN job_code ILIKE $1 THEN 0
            WHEN $1 ILIKE '%' || job_code || '%' THEN 1
            WHEN job_name ILIKE '%' || $1 || '%' THEN 2
            ELSE 3
        END,
        length(job_code) DESC
    LIMIT 1
"""

RESOLVE_WAREHOUSE_SQL = """
    SELECT warehouse_code, warehouse_name FROM pr_warehouses
    WHERE warehouse_code ILIKE $1
       OR warehouse_name ILIKE '%' || $1 || '%'
       OR $1 ILIKE '%' || warehouse_code || '%'
       OR $1 ILIKE '%' || warehouse_name || '%'
    ORDER BY
        CASE
            WHEN warehouse_code ILIKE $1 THEN 0
            WHEN $1 ILIKE '%' || warehouse_code || '%' THEN 1
            WHEN warehouse_name ILIKE '%' || $1 || '%' THEN 2
            ELSE 3
        END,
        length(warehouse_code) DESC
    LIMIT 1
"""

RESOLVE_MATERIAL_SQL = """
    SELECT material_code, description, default_uom_code, suggested_rate, available_qty FROM pr_materials
    WHERE material_code ILIKE $1
       OR description ILIKE '%' || $1 || '%'
       OR $1 ILIKE '%' || material_code || '%'
       OR $1 ILIKE '%' || description || '%'
    ORDER BY
        CASE
            WHEN material_code ILIKE $1 THEN 0
            WHEN $1 ILIKE '%' || material_code || '%' THEN 1
            WHEN description ILIKE '%' || $1 || '%' THEN 2
            ELSE 3
        END,
        length(material_code) DESC
    LIMIT 1
"""

RESOLVE_WBS_SQL_SCOPED = """
    SELECT wbs_code, description, default_cost_package_code FROM pr_wbs
    WHERE job_code = $2 AND (
        wbs_code ILIKE $1
        OR description ILIKE '%' || $1 || '%'
        OR $1 ILIKE '%' || wbs_code || '%'
        OR $1 ILIKE '%' || description || '%'
    )
    ORDER BY
        CASE
            WHEN wbs_code ILIKE $1 THEN 0
            WHEN $1 ILIKE '%' || wbs_code || '%' THEN 1
            WHEN description ILIKE '%' || $1 || '%' THEN 2
            ELSE 3
        END,
        length(wbs_code) DESC
    LIMIT 1
"""

RESOLVE_WBS_SQL_UNSCOPED = """
    SELECT wbs_code, description, default_cost_package_code FROM pr_wbs
    WHERE wbs_code ILIKE $1
       OR description ILIKE '%' || $1 || '%'
       OR $1 ILIKE '%' || wbs_code || '%'
       OR $1 ILIKE '%' || description || '%'
    ORDER BY
        CASE
            WHEN wbs_code ILIKE $1 THEN 0
            WHEN $1 ILIKE '%' || wbs_code || '%' THEN 1
            WHEN description ILIKE '%' || $1 || '%' THEN 2
            ELSE 3
        END,
        length(wbs_code) DESC
    LIMIT 1
"""


async def resolve_job(pool: asyncpg.Pool, text: str) -> dict | None:
    row = await pool.fetchrow(RESOLVE_JOB_SQL, text.strip())
    return dict(row) if row else None


async def resolve_warehouse(pool: asyncpg.Pool, text: str) -> dict | None:
    row = await pool.fetchrow(RESOLVE_WAREHOUSE_SQL, text.strip())
    return dict(row) if row else None


async def resolve_material(pool: asyncpg.Pool, text: str) -> dict | None:
    row = await pool.fetchrow(RESOLVE_MATERIAL_SQL, text.strip())
    return dict(row) if row else None


async def resolve_wbs(pool: asyncpg.Pool, text: str, job_code: str | None) -> dict | None:
    """Scoped to job_code when known (a WBS element belongs to one job) so a
    WBS code that happens to match text in a *different* job never gets
    picked. Falls back to an unscoped lookup if job_code isn't available."""
    if job_code:
        row = await pool.fetchrow(RESOLVE_WBS_SQL_SCOPED, text.strip(), job_code)
    else:
        row = await pool.fetchrow(RESOLVE_WBS_SQL_UNSCOPED, text.strip())
    return dict(row) if row else None


# --- small-enumeration fields (pr_category / planning_category / supply_at) -
# Same grounded rule as everything above: the option set and its codes live
# in pr_category_options / pr_planning_category_options / pr_supply_at_options
# (db/01_schema.sql + db/02_seed.sql), not in Python — fixing a code or
# adding a new option is a DB UPDATE/INSERT, not a redeploy.

async def get_pr_category_options(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch("SELECT code, label FROM pr_category_options ORDER BY code")
    return [dict(r) for r in rows]


async def get_planning_category_options(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch("SELECT code, label FROM pr_planning_category_options ORDER BY code")
    return [dict(r) for r in rows]


async def get_supply_at_options(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch("SELECT code, label, own_premises FROM pr_supply_at_options ORDER BY code")
    return [dict(r) for r in rows]


async def resolve_pr_category(pool: asyncpg.Pool, text: str) -> dict | None:
    row = await pool.fetchrow("SELECT code, label FROM pr_category_options WHERE label ILIKE '%' || $1 || '%'", text.strip())
    return dict(row) if row else None


async def resolve_planning_category(pool: asyncpg.Pool, text: str) -> dict | None:
    row = await pool.fetchrow("SELECT code, label FROM pr_planning_category_options WHERE label ILIKE '%' || $1 || '%'", text.strip())
    return dict(row) if row else None


async def resolve_supply_at(pool: asyncpg.Pool, text: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT code, label, own_premises FROM pr_supply_at_options WHERE label ILIKE '%' || $1 || '%'", text.strip()
    )
    return dict(row) if row else None


async def get_pr_constants(pool: asyncpg.Pool) -> dict[str, str]:
    """The deployment-wide payload constants (pr_constants table) as a plain
    {key: value} dict — see app/agents/pr_draft_agent.py for what reads
    which key. All values come back as strings (the column is TEXT); cast at
    the call site for the numeric ones."""
    rows = await pool.fetch("SELECT key, value FROM pr_constants")
    return {r["key"]: r["value"] for r in rows}
