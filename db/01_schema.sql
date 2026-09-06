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
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE conversation_turns (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id     UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role                TEXT NOT NULL,      -- user | assistant
    content             TEXT NOT NULL,
    intent              TEXT,               -- set on assistant turns: track_shipment | file_complaint | general_faq
    agent_used          TEXT,               -- Orchestrator | TrackingAgent | EscalationAgent
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
