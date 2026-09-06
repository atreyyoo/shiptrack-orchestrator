# ShipTrack — Orchestrator Pipeline Demo

A small, self-contained demo of an **orchestrator agent** that decides which
specialist agent and tool to invoke, backed by Postgres, driven from Postman,
with every decision traced live to the console as it happens.

```
Postman  --API call-->  FastAPI  --DB query-->  Postgres
                            |
                            v
                    Orchestrator Agent (LLM, system prompt)
                       decides: track / complain / general
                            |
                 -----------+-----------
                 |                     |
          TrackingAgent          EscalationAgent
        (grounded in DB rows)   (drafts a ticket)
                 |                     |
                 v                     v
              answer               ticket created (Postgres)
                            |
                            v
              every step printed live to the console
              + persisted to trace_events (GET /trace/{id})
```

## Agents

Each agent is a separate system prompt (`app/agents/prompts.py`) — none of
them share instructions:

| Agent | Job | Touches the DB? | Touches the customer? |
|---|---|---|---|
| **Orchestrator** | Reads the message, decides `track_shipment` / `file_complaint` / `general_faq`. Never answers directly. | No | No |
| **TrackingAgent** | Explains shipment status/ETA, strictly grounded in the rows the orchestrator fetched. | No (reads context handed to it) | Yes |
| **EscalationAgent** | Handles complaints *and* "I didn't like that answer" feedback — drafts a ticket + a reply. | No (reads context handed to it) | Yes |

The DB queries and the ticket write are plain deterministic **tools**
(`app/tools/`) — not LLM calls — called by the orchestration logic in
`app/main.py` between agent steps.

## The "raise a ticket if you don't like the answer" flow

1. `POST /chat` returns an `answer` plus a `message_id`.
2. If the customer is unhappy: `POST /feedback {conversation_id, message_id, helpful: false}` → the API records it and replies with `offer_ticket: true` and a prompt to escalate.
3. `POST /ticket {conversation_id, message_id}` → the EscalationAgent drafts a ticket from that conversation's context (grounded in whatever shipment was last discussed) and files it — issue type `unsatisfactory_response`.

## Live tracing

Every step — receiving the API call, the orchestrator's classification, each
DB query, each agent's answer/draft, each DB write — is:
1. **Printed live to the console** the moment it starts and finishes, while `uvicorn` is running. This is what makes the orchestrator's decisions visible in real time while you fire requests from Postman.
2. **Persisted** to the `trace_events` table, replayable afterwards via `GET /trace/{conversation_id}`.
3. **Returned inline** in the `/chat` and `/ticket` response bodies (`trace` field) so Postman shows it immediately too.

Example console output for one `/chat` call:
```
11:48:43.912 INFO  shiptrack.trace | conv=7f15... step=1 action=receive_api_call status=start {...}
11:48:43.912 INFO  shiptrack.trace | conv=7f15... step=1 action=receive_api_call status=done (0ms) {...}
11:48:43.912 INFO  shiptrack.trace | conv=7f15... step=2 agent=Orchestrator action=classify_intent status=start {...}
11:48:51.297 INFO  shiptrack.trace | conv=7f15... step=2 agent=Orchestrator action=classify_intent status=done (7385ms) {"decision": {"intent": "track_shipment", "tracking_number": "TRK100987", ...}}
11:48:51.297 INFO  shiptrack.trace | conv=7f15... step=3 action=db_query:get_shipment_status status=start {...}
11:48:51.365 INFO  shiptrack.trace | conv=7f15... step=3 action=db_query:get_shipment_status status=done (68ms) {"found": true}
11:48:51.365 INFO  shiptrack.trace | conv=7f15... step=4 agent=TrackingAgent action=compose_answer status=start {...}
11:48:56.601 INFO  shiptrack.trace | conv=7f15... step=4 agent=TrackingAgent action=compose_answer status=done (5236ms) {"answer_preview": "..."}
```

## LLM backend

`app/llm.py` is the single point of contact with the model, behind one
`.chat()` call. Three interchangeable backends, chosen by `LLM_MODE` in `.env`:

| Mode | Uses | When |
|---|---|---|
| `local` (default via `auto`) | A local OpenAI-compatible server — **Ollama running `gpt-oss:20b`** by default | No cloud dependency; what this demo ships configured for |
| `foundry` | Microsoft/Azure AI Foundry deployment | When you have a working Foundry endpoint |
| `mock` | Deterministic heuristic responder, zero network calls | Quick smoke-testing the pipeline shape with no model at all |

`LLM_MODE=auto` (the default) picks `local` if `LOCAL_BASE_URL` is set (it is,
out of the box), else `foundry` if configured, else falls back to `mock`.
Switching backends is an `.env` change only — no code changes.

### Using Ollama (default)

```bash
ollama pull gpt-oss:20b     # ~13GB, one-time
# Ollama serves an OpenAI-compatible API at http://localhost:11434/v1 automatically
```

### Using LM Studio instead

Set in `.env`:
```
LOCAL_BASE_URL=http://localhost:1234/v1
LOCAL_MODEL=<whatever LM Studio's server shows for the loaded model>
```

### Using Microsoft/Azure AI Foundry instead

Set in `.env`:
```
LLM_MODE=foundry
FOUNDRY_ENDPOINT=https://<your-foundry-resource>.openai.azure.com/
FOUNDRY_API_KEY=<key>
FOUNDRY_DEPLOYMENT=<your deployment name>
```

## Setup

### 1. Postgres

Option A — Docker (needs Docker Desktop / the `docker compose` plugin):
```bash
docker compose up -d
```

Option B — a local Postgres install: create a `shiptrack` role/db and apply
the two files in `db/` in order:
```bash
createuser -s shiptrack
psql -d postgres -c "ALTER USER shiptrack WITH PASSWORD 'shiptrack';"
createdb -O shiptrack shiptrack
psql -U shiptrack -d shiptrack -h localhost -f db/01_schema.sql
psql -U shiptrack -d shiptrack -h localhost -f db/02_seed.sql
```

Seed data: 2 customers, 4 shipments covering every status —
`TRK100234` (in transit), `TRK100987` (delayed / customs hold),
`TRK101122` (out for delivery), `TRK101555` (delivered).

### 2. Python app

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # edit if your Postgres/LLM setup differs from the defaults
uvicorn app.main:app --reload --port 8000
```

Check `GET /health` — it reports which LLM mode is active.

### 3. Postman

Import `postman/ShipTrack.postman_collection.json` (and, optionally, the
matching `.postman_environment.json`). Run requests **0 through 9 in order**
in the "ShipTrack Orchestrator" folder — each one prints what it's doing to
your terminal running `uvicorn`, and later requests pick up
`conversationId`/`messageId` automatically from earlier responses via
collection variables.

## API reference

| Method | Route | Purpose |
|---|---|---|
| GET | `/health` | Liveness + active LLM mode |
| POST | `/chat` | `{conversation_id?, customer_email?, message}` → orchestrated answer + trace |
| POST | `/feedback` | `{conversation_id, message_id, helpful}` → records feedback; `helpful:false` offers a ticket |
| POST | `/ticket` | `{conversation_id, message_id?, note?}` → drafts + files a support ticket from conversation context |
| GET | `/conversations/{id}` | Full turn-by-turn replay |
| GET | `/trace/{id}` | Persisted step-by-step trace for that conversation |
| GET | `/shipments/{tracking_number}` | Raw DB lookup, no LLM — isolates the "DB query" leg |
| GET | `/customers/{email}/shipments` | All shipments for a customer |

## Schema

See [`db/01_schema.sql`](db/01_schema.sql) for the full DDL:
`customers`, `shipments`, `shipment_events`, `conversations`,
`conversation_turns`, `message_feedback`, `support_tickets`, `trace_events`.

## Extending this

- Add a fourth agent (e.g. a **Notifier** that emails on status change) by adding a prompt in `app/agents/prompts.py`, a class in `app/agents/`, and a branch in `app/main.py`'s `/chat` handler.
- Add a new tool the same way — a function in `app/tools/`, called from `main.py`, wrapped in `tracer.step(...)`.
- Swap `LOCAL_MODEL` for any other model your local server hosts, or flip `LLM_MODE` to `foundry` once that's available again — no other code changes needed.
