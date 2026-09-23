-- ShipTrack orchestrator demo — schema
-- Postgres. Applied automatically by docker-compose on first container start
-- (mounted into /docker-entrypoint-initdb.d/).

CREATE EXTENSION IF NOT EXISTS "pgcrypto"; -- gen_random_uuid()

CREATE TABLE customers (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,
    phone       TEXT
);

CREATE TABLE shipments (
    id                  SERIAL PRIMARY KEY,
    tracking_number     TEXT NOT NULL UNIQUE,
    customer_id         INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    carrier             TEXT NOT NULL,
    origin              TEXT NOT NULL,
    destination         TEXT NOT NULL,
    status              TEXT NOT NULL,   -- in_transit | delayed | customs_hold | out_for_delivery | delivered
    estimated_delivery  DATE,
    actual_delivery     DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE shipment_events (
    id              SERIAL PRIMARY KEY,
    shipment_id     INTEGER NOT NULL REFERENCES shipments(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,       -- picked_up | in_transit | customs_hold | delayed | out_for_delivery | delivered
    location        TEXT,
    occurred_at     TIMESTAMPTZ NOT NULL,
    note            TEXT
);

CREATE INDEX idx_shipment_events_shipment_id ON shipment_events(shipment_id, occurred_at);

-- One row per chat session. Tracks the last tracking number mentioned so
-- later turns / ticket creation can resolve a shipment without the caller
-- repeating it.
CREATE TABLE conversations (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_email              TEXT,
    last_tracking_number        TEXT,
    -- Ticket-intake state (app/tools/ticket_intake_tool.py): how many of the
    -- fixed clarifying questions have been asked in this conversation, and
    -- which ones, so a vague complaint can't skip straight to a ticket.
    clarifying_questions_asked  INTEGER NOT NULL DEFAULT 0,
    clarifying_questions        JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Draft-PR intake state (app/tools/pr_intake_tool.py): the mandatory PR
    -- fields collected so far this conversation, keyed by field name (plus a
    -- "_status" sentinel so a fresh conversation is distinguishable from one
    -- where intake has started but no field has resolved yet). Reset to '{}'
    -- once a PR is submitted (or fails), so the next request starts clean.
    pr_draft_fields             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE conversation_turns (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id     UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role                TEXT NOT NULL,      -- user | assistant
    content             TEXT NOT NULL,
    intent              TEXT,               -- set on assistant turns: track_shipment | file_complaint | file_purchase_requisition | find_vendor | general_faq
    agent_used          TEXT,               -- Orchestrator | TrackingAgent | EscalationAgent | PRIntake | DraftPRAgent | VendorAgent
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_conversation_turns_conv_id ON conversation_turns(conversation_id, created_at);

-- Per-message thumbs up/down. A "down" is what unlocks the "raise a ticket"
-- offer in the chat flow.
CREATE TABLE message_feedback (
    id                  SERIAL PRIMARY KEY,
    message_id          UUID NOT NULL REFERENCES conversation_turns(id) ON DELETE CASCADE,
    conversation_id     UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    helpful             BOOLEAN NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE support_tickets (
    id                  SERIAL PRIMARY KEY,
    conversation_id     UUID REFERENCES conversations(id) ON DELETE SET NULL,
    shipment_id         INTEGER REFERENCES shipments(id) ON DELETE SET NULL,
    issue_type          TEXT NOT NULL,      -- damaged | lost | delayed | wrong_address | unsatisfactory_response | other
    priority            TEXT NOT NULL,      -- low | medium | high | urgent
    subject             TEXT NOT NULL,
    description         TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'open',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every orchestrator/agent/tool step, so a run can be inspected after the
-- fact via GET /trace/{conversation_id} — a durable record of the same
-- steps that are printed live to the console while the request runs.
--
-- turn_id groups the steps belonging to one /chat or /ticket call. Step
-- numbers restart at 1 for every call, so without turn_id two calls in the
-- same conversation would both write "step=1,2,3..." with no way to tell
-- them apart — turn_id is what lets the frontend poll for "just this
-- message's steps" while it's in flight, live.
CREATE TABLE trace_events (
    id                  BIGSERIAL PRIMARY KEY,
    conversation_id     UUID,
    turn_id             UUID NOT NULL,
    step                INTEGER NOT NULL,
    agent               TEXT,
    action              TEXT NOT NULL,
    payload             JSONB,
    duration_ms         INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_trace_events_conv_id ON trace_events(conversation_id, step);
CREATE INDEX idx_trace_events_turn_id ON trace_events(turn_id, step);

-- === Draft PR Agent ==========================================================
-- Master-data tables backing app/tools/pr_master_data_tools.py's field
-- resolution. Same "DB query -> grounded context" rule as the shipment
-- tables above: free text from the customer is looked up here and turned
-- into an exact code, never guessed by an LLM. For this POC these are plain
-- Postgres tables seeded in db/02_seed.sql; swap the SQL in
-- pr_master_data_tools.py for a live ERP master-data call when one exists.

CREATE TABLE pr_jobs (
    job_code    TEXT PRIMARY KEY,
    job_serial  INTEGER NOT NULL,   -- S_JobSl on the submit payload — looked up once the job is picked, never asked
    job_name    TEXT NOT NULL
);

CREATE TABLE pr_warehouses (
    warehouse_code  TEXT PRIMARY KEY,
    warehouse_name  TEXT NOT NULL
);

CREATE TABLE pr_materials (
    material_code     TEXT PRIMARY KEY,
    description        TEXT NOT NULL,
    default_uom_code   TEXT NOT NULL,          -- dpR_UOM_Code — derived from the material, not asked
    suggested_rate     NUMERIC(14,2) NOT NULL,  -- dpR_Suggested_Rate — derived from the material, not asked
    -- Stock on hand. Decremented by app/store.py:decrement_material_stock()
    -- once a PR referencing this material actually submits — simulates the
    -- material getting "secured" for that PR. A CHECK, not just a default,
    -- so a decrement can never push this negative even under a race.
    available_qty     NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (available_qty >= 0)
);

-- === Vendor Agent ============================================================
-- Backs "find me the cheapest vendor for X" style questions
-- (app/tools/vendor_tools.py) — same grounding rule as everything else here:
-- the material is resolved against pr_materials exactly like PR intake does,
-- then every vendor's price for it is a real row, never an LLM guess. A
-- material can have several vendors, each with their own price, so "cheapest"
-- is a plain ORDER BY, not something the LLM has to reason about.

CREATE TABLE pr_vendors (
    vendor_code   TEXT PRIMARY KEY,
    vendor_name   TEXT NOT NULL
);

CREATE TABLE pr_vendor_material_prices (
    vendor_code     TEXT NOT NULL REFERENCES pr_vendors(vendor_code),
    material_code   TEXT NOT NULL REFERENCES pr_materials(material_code),
    price           NUMERIC(14,2) NOT NULL,
    uom_code        TEXT NOT NULL,
    lead_time_days  INTEGER,
    PRIMARY KEY (vendor_code, material_code)
);

-- material_code first (not vendor_code) since every query here is "who sells
-- material X", never "what does vendor Y sell" — price included so the
-- cheapest-first ORDER BY in vendor_tools.py is a pure index scan.
CREATE INDEX idx_pr_vendor_material_prices_material ON pr_vendor_material_prices(material_code, price);

-- === Reference/code tables — everything below is data the payload embeds
-- that ISN'T a master-data lookup and ISN'T typed by the user: the small
-- fixed-choice enumerations (was Python constants in app/tools/
-- pr_intake_tool.py) and the deployment-wide payload constants (was
-- app/config.py / .env). Moved here so literally every payload field is
-- DB-sourced except what the user actually typed/picked. Values are still
-- PLACEHOLDERS carried over from the example payload's comments — the point
-- of moving them here is that fixing them later is an UPDATE, not a
-- redeploy, not that the values themselves are now correct.

CREATE TABLE pr_category_options (
    code   INTEGER PRIMARY KEY,   -- hpR_PR_Category_Detail_Code
    label  TEXT NOT NULL
);

CREATE TABLE pr_planning_category_options (
    code   INTEGER PRIMARY KEY,   -- hpR_Planning_Category_Type_Detail_Code
    label  TEXT NOT NULL
);

CREATE TABLE pr_supply_at_options (
    code           INTEGER PRIMARY KEY,  -- hpR_Material_Supply_Detail_Code
    label          TEXT NOT NULL,
    own_premises   TEXT NOT NULL          -- hpR_Own_Premises — "Y" | "N"
);

-- Deployment-wide payload constants — one row per key. A key/value table
-- (not one column per constant) so adding one later is an INSERT, not a
-- migration. See app/tools/pr_master_data_tools.py:get_pr_constants().
CREATE TABLE pr_constants (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE pr_wbs (
    wbs_code                   TEXT PRIMARY KEY,
    job_code                   TEXT NOT NULL REFERENCES pr_jobs(job_code),
    description                TEXT NOT NULL,
    default_cost_package_code  TEXT NOT NULL   -- dpR_Cost_Package_Code — derived from the WBS, not asked (assumes one cost package per WBS for this POC)
);

CREATE INDEX idx_pr_wbs_job_code ON pr_wbs(job_code);

-- One row per submitted (or failed) PR draft, for audit/trace purposes —
-- mirrors support_tickets' role in the complaint flow.
CREATE TABLE purchase_requisitions (
    id               SERIAL PRIMARY KEY,
    conversation_id  UUID REFERENCES conversations(id) ON DELETE SET NULL,
    pr_number        TEXT,                              -- e.g. "PR/2026/00123" — null if the submit call failed
    status           TEXT NOT NULL DEFAULT 'submitted',  -- submitted | failed
    payload          JSONB NOT NULL,
    response         JSONB,
    -- The resolved PR-intake slot-fill state at submission time (job name,
    -- warehouse name, material description, etc.) — `payload` only has ERP
    -- codes, not human-readable labels, so this is what GET /purchase-
    -- requisitions/{id}/pdf reads to render a readable summary rather than a
    -- code dump. See app/tools/pr_pdf_tool.py.
    fields_snapshot  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Usage history for the free-text/grounded-lookup PR-intake fields (job,
-- warehouse, material, wbs — never the fixed-choice fields, which already
-- have a clickable options menu; see app/tools/pr_intake_tool.py). One row
-- per (field, value) ever successfully resolved, system-wide — not scoped
-- to a conversation or user, same as recently-used lists in most tools.
-- Bumped on every re-use rather than duplicated, so the list surfaces
-- actual frequent/recent values, not just "everything ever typed once".
CREATE TABLE pr_field_history (
    field         TEXT NOT NULL,        -- 'job' | 'warehouse' | 'material' | 'wbs'
    value         TEXT NOT NULL,        -- the canonical code — clicking a history entry sends this back verbatim
    label         TEXT NOT NULL,        -- human-readable "code — name" shown in the dropdown
    use_count     INTEGER NOT NULL DEFAULT 1,
    last_used_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (field, value)
);

CREATE INDEX idx_pr_field_history_lookup ON pr_field_history(field, last_used_at DESC);
