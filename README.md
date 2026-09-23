# ShipTrack — Orchestrator Pipeline Demo

A small, self-contained demo of an **orchestrator agent** that decides which
specialist agent and tool to invoke, backed by Postgres, driven from Postman,
with every decision traced live to the console as it happens.

```
Postman  --API call-->  FastAPI  --DB query-->  Postgres
                            |
                            v
                    Orchestrator Agent (LLM, system prompt)
             decides: track / complain / draft a PR / find a vendor / general
                            |
                 -----------+-----------+------------------+------------------+
                 |                     |                    |                  |
          TrackingAgent          EscalationAgent        PRIntake -> DraftPRAgent   VendorAgent
        (grounded in DB rows)   (drafts a ticket)   (asks mandatory fields, then  (grounded in
                 |                     |              submits the PR — grounded   DB vendor/price
                 v                     v              in DB master data, never    rows, cheapest
              answer               ticket created      LLM-guessed codes)         first)
                                     (Postgres)               |                        |
                                                               v                        v
                                                     PR submitted (ERP / mock)      answer
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
| **Orchestrator** | Reads the message, decides `track_shipment` / `file_complaint` / `file_purchase_requisition` / `find_vendor` / `general_faq`. Never answers directly. | No | No |
| **TrackingAgent** | Explains shipment status/ETA, strictly grounded in the rows the orchestrator fetched. | No (reads context handed to it) | Yes |
| **EscalationAgent** | Handles complaints *and* "I didn't like that answer" feedback — drafts a ticket + a reply. | No (reads context handed to it) | Yes |
| **DraftPRAgent** | Assembles + submits a Purchase Requisition once the PRIntake gate has every mandatory field. No LLM step — a PR payload is exact, machine-typed data, so there's nothing for an LLM to draft; see [app/agents/pr_draft_agent.py](app/agents/pr_draft_agent.py). | No (payload assembly only) | Yes (confirms the PR number) |
| **VendorAgent** | Recommends the cheapest vendor for a material, strictly grounded in the vendor/price rows the orchestrator fetched (cheapest-first, already sorted by the DB query). | No (reads context handed to it) | Yes |

The DB queries and the ticket/PR writes are plain deterministic **tools**
(`app/tools/`) — not LLM calls — called by the orchestration logic in
`app/main.py` between agent steps.

## The "raise a ticket if you don't like the answer" flow

1. `POST /chat` returns an `answer` plus a `message_id`.
2. If the customer is unhappy: `POST /feedback {conversation_id, message_id, helpful: false}` → the API records it and replies with `offer_ticket: true` and a prompt to escalate.
3. `POST /ticket {conversation_id, message_id}` → the EscalationAgent drafts a ticket from that conversation's context (grounded in whatever shipment was last discussed) and files it — issue type `unsatisfactory_response`.

## Drafting a Purchase Requisition

1. `POST /chat {message: "I need to raise a PR"}` (or any phrasing the Orchestrator recognizes as `file_purchase_requisition`) → the PRIntake gate (`app/tools/pr_intake_tool.py`) asks one fixed-order question per turn for each mandatory field:
   Job → Warehouse → PR Category (Revenue/Capital) → Planning Category → Supply At → Material → **Vendor** → WBS → Quantity.
2. Each answer is resolved against **grounded master data** in Postgres (`pr_jobs`, `pr_warehouses`, `pr_materials`, `pr_wbs` — seeded in `db/02_seed.sql` with deliberately varied sample data across several projects/sites) — never guessed by an LLM. An unresolved answer re-asks the same question rather than accepting it.
   UOM and Cost Package are *derived* automatically (from the Material and WBS rows) rather than asked, along with the line/PR value (qty × rate). Every fixed payload constant — the PR Category/Planning Category/Supply At code tables and the deployment-wide envelope values (`S_Finyear`, `hpR_DT_Code`, etc.) — is DB-sourced too (`pr_category_options`, `pr_planning_category_options`, `pr_supply_at_options`, `pr_constants`), not hardcoded in Python or `.env`: the only inputs that don't come from a DB table are the customer's own free-text answers.
   PR Category, Planning Category, and Supply At have a genuinely fixed, small set of choices — `ChatResponse.options` carries `[{value, label}]` for those three questions (queried live from their reference tables) so a frontend can render a clickable menu instead of a text box (the built-in chat UI does — see `appendOptionMenu()` in `frontend/app.js`); clicking sends `value` back exactly like typing it, so it always resolves. `options` is `null` for the other, open-ended fields (Job/Warehouse/Material/WBS/Quantity).
   **Vendor**: once Material resolves, every vendor actually pricing it (`pr_vendors` + `pr_vendor_material_prices`, same tables the standalone vendor question below uses) is offered as a clickable menu too, cheapest first, price and lead time shown right in the option label — see `app/tools/vendor_tools.py:get_vendors_for_material_code()`. If nobody has priced that material yet there's nothing to choose between, so `app/main.py` auto-skips straight to WBS instead of asking an unanswerable question — this is common in the seed data, only steel and cement have vendor pricing on file. When a vendor *is* chosen, **DraftPRAgent** (`app/agents/pr_draft_agent.py`) computes the rate/value from that vendor's price instead of the material's generic catalog `suggested_rate`, and appends the vendor's name/code to `hpR_Remarks` — there's no dedicated field for vendor identity in the example ERP payload, so this avoids inventing one that might not match the real schema. Skipped (no vendor picked) falls back to the catalog rate exactly as before this field existed.
   **Material stock**: `pr_materials.available_qty` is real stock on hand. The Quantity question names the material and states what's available; requesting more than that is rejected with a clear message instead of being accepted (`app/main.py`'s stock-sufficiency check) — nothing here comes from the customer without being checked against the DB. A successful submission decrements it (`app/store.py:decrement_material_stock()`, a single conditional `UPDATE ... WHERE available_qty >= qty` so it can't go negative even under a race) — simulating the material getting secured for that PR.
3. Once every field resolves, **DraftPRAgent** assembles the submit-draft-PR payload and **`app/tools/pr_submit_tool.py`** posts it — to the real ERP endpoint if `PR_SUBMIT_URL` is set, otherwise to a deterministic mock so the flow is runnable with no ERP access. The result (`PR/...`) comes back in the chat answer and in `ChatResponse.pr_number`/`pr_id`, and is logged to the `purchase_requisitions` table either way (`status: submitted | failed`), alongside a `fields_snapshot` (the resolved intake state — job name, material description, chosen vendor or null, etc., not just codes) for readable downloads later.
4. `GET /purchase-requisitions/{pr_id}/pdf` and `/payload` download a neat one-page PDF summary (`app/tools/pr_pdf_tool.py`, via `reportlab`, including a Vendor row) or the exact submitted JSON, respectively — both built entirely from the stored row, independent of any later master-data change. The chat UI shows both as download buttons once a PR is submitted (`appendPrDownloads()` in `frontend/app.js`), plus a **Preview** button — same PDF endpoint with `?inline=1`, which just swaps `Content-Disposition` from `attachment` to `inline` so it opens in the browser's own PDF viewer in a new tab instead of saving to disk.

Job, Warehouse, Material, and WBS also get a **usage-history dropdown** — clicking into the composer input while one of those is being asked shows past values as clickable shortcuts (`pr_field_history` table, bumped on every resolve — see `app/tools/pr_intake_tool.py:HISTORY_FIELDS` and `frontend/app.js`'s focus/blur handlers on `#chat-input`). Scoped to those four lookup fields only — Revenue/Capital, Planning Category, Supply At, and Vendor keep their existing clickable-options menu instead (see above), and Quantity stays plain free text since a past quantity isn't a reusable identifier.

An in-progress PR draft is tracked per-conversation (`conversations.pr_draft_fields`) and takes priority over the Orchestrator's own classification on the next turn — so a terse follow-up like "10 MT" continues the draft instead of risking misclassification as `general_faq`. **Known limitation:** there's currently no way to cancel a PR draft mid-flow — every message is treated as an answer to the current question until all fields resolve or the conversation is abandoned. A failed submission clears the collected fields, so a retry re-asks every question rather than resuming.

**Not implemented in this pass** (deliberately deferred — see the source spec you're working from): surplus-stock lookup / DC-based PR branching, the "Concorde item" exclusion check, PR creation from an uploaded document, PR-level/material-level file attachments, and RFQ creation.

## Finding the cheapest vendor for a material

`POST /chat {message: "find me the cheapest vendor selling steel"}` — the
Orchestrator recognizes this as `find_vendor` and extracts the material the
customer means (`material_query`, their own words, e.g. "steel") straight
from the message; no multi-turn intake, this is a single-shot question.

That text is resolved to a real material the same way PR intake resolves it
(`app/tools/pr_master_data_tools.py:resolve_material`), then every vendor
with a price on file for it is fetched from Postgres, cheapest first
(`pr_vendors` + `pr_vendor_material_prices`, seeded in `db/02_seed.sql`).
**VendorAgent** only composes a sentence around those real rows — it never
invents a vendor, price, or material, and never re-ranks the list itself
(see [app/agents/vendor_agent.py](app/agents/vendor_agent.py)). If the
material doesn't resolve, it says so and asks for a material code instead
of guessing.

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
| `local` (default via `auto`) | A local OpenAI-compatible server — **Ollama running `qwen2.5:3b`** by default (small, fast, runs comfortably on a laptop) | No cloud dependency; what this demo ships configured for |
| `foundry` | Microsoft/Azure AI Foundry deployment | When you have a working Foundry endpoint |
| `mock` | Deterministic heuristic responder, zero network calls | Quick smoke-testing the pipeline shape with no model at all |

`LLM_MODE=auto` (the default) picks `local` if `LOCAL_BASE_URL` is set (it is,
out of the box), else `foundry` if configured, else falls back to `mock`.
Switching backends is an `.env` change only — no code changes.

### Using Ollama (default)

```bash
ollama pull qwen2.5:3b     # small, fast, one-time pull
# Ollama serves an OpenAI-compatible API at http://localhost:11434/v1 automatically
```

Want better quality and have the hardware for it? `gpt-oss:20b` also works
(`ollama pull gpt-oss:20b`, ~13GB) — set `LOCAL_MODEL=gpt-oss:20b` in `.env`.
It's opt-in, not the default, since on modest hardware it can peg CPU/GPU for
minutes per request.

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
matching `.postman_environment.json`). Run requests **0 through 20 in order**
in the "ShipTrack Orchestrator" folder — each one prints what it's doing to
your terminal running `uvicorn`, and later requests pick up
`conversationId`/`messageId` automatically from earlier responses via
collection variables. Requests 10-19 walk a full Draft PR Agent conversation
end to end, one mandatory field per request (including picking a vendor in
request 17), ending with a submitted PR. Request 20 exercises the standalone
"find me the cheapest vendor" question on its own.

## API reference

| Method | Route | Purpose |
|---|---|---|
| GET | `/health` | Liveness + active LLM mode |
| POST | `/chat` | `{conversation_id?, customer_email?, message}` → orchestrated answer + trace |
| POST | `/feedback` | `{conversation_id, message_id, helpful}` → records feedback; `helpful:false` offers a ticket |
| POST | `/ticket` | `{conversation_id, message_id?, note?}` → drafts + files a support ticket from conversation context |
| — | *(no new route)* | Drafting a PR is driven entirely through `/chat` — see "Drafting a Purchase Requisition" above. `ChatResponse.pr_number`/`pr_id` are set once a PR is submitted. |
| GET | `/purchase-requisitions/{pr_id}/pdf` | Downloads a neat one-page PDF summary of that PR |
| GET | `/purchase-requisitions/{pr_id}/payload` | Downloads the exact JSON payload that was submitted |
| GET | `/conversations/{id}` | Full turn-by-turn replay |
| GET | `/trace/{id}` | Persisted step-by-step trace for that conversation |
| GET | `/shipments/{tracking_number}` | Raw DB lookup, no LLM — isolates the "DB query" leg |
| GET | `/customers/{email}/shipments` | All shipments for a customer |

## Schema

See [`db/01_schema.sql`](db/01_schema.sql) for the full DDL:
`customers`, `shipments`, `shipment_events`, `conversations`,
`conversation_turns`, `message_feedback`, `support_tickets`, `trace_events`,
`pr_jobs`, `pr_warehouses`, `pr_materials`, `pr_wbs`, `pr_field_history`,
`pr_category_options`, `pr_planning_category_options`, `pr_supply_at_options`,
`pr_constants`, `pr_vendors`, `pr_vendor_material_prices`,
`purchase_requisitions`.

## Extending this

- Add another agent (e.g. a **Notifier** that emails on status change) by adding a prompt in `app/agents/prompts.py`, a class in `app/agents/`, and a branch in `app/main.py`'s `/chat` handler.
- Add a new tool the same way — a function in `app/tools/`, called from `main.py`, wrapped in `tracer.step(...)`.
- Swap `LOCAL_MODEL` for any other model your local server hosts, or flip `LLM_MODE` to `foundry` once that's available again — no other code changes needed.

### Draft PR Agent — known gaps to close before this is more than a POC

- **Placeholder codes — now the right shape, still the wrong values.** PR Category / Planning Category / Supply At (`pr_category_options`, `pr_planning_category_options`, `pr_supply_at_options`) and the envelope constants (`pr_constants`) are DB tables now, so fixing one is an `UPDATE`, not a redeploy — but every value in them is still a best guess carried over from the example payload's own comments. Confirm each one against your ERP's real reference-code tables before this goes near production. Same caveat for all the master-data rows (`pr_jobs`/`pr_warehouses`/`pr_materials`/`pr_wbs`, including every `available_qty` stock figure) — expanded to be varied for testing, none of it is real inventory data.
- **`jsonScheduleBreakups`** is sent as an empty array — its `dprS_*` field shape was never provided.
- **Master-data matching is substring containment, not real fuzzy search** (`app/tools/pr_master_data_tools.py`) — it tolerates a natural sentence around the code/name ("here is the job code LE24M128", "I need some steel packing wire") but not different phrasing (synonyms, reordered words, a partial description); those just get re-asked rather than mismatched.
- **The Guardrail runs with no conversation context** (`guardrail_agent.classify()` takes only the current message). Confirmed in testing that this makes it unreliable on short PR-intake answers taken out of context — the same small local model misclassified "here is the job code LE24M128" as `internal_data`-restricted, then in a later run misclassified a misspelled "own premsis" as `prompt_injection`. Two different categories on two harmless answers ruled out patching categories one at a time, so `app/main.py` fully suppresses the Guardrail's `restricted` verdict while a PR draft is in progress (plus a prompt clarification, for general benefit outside that window too). This is safe specifically because nothing downstream of PR intake is an LLM — `pr_intake_tool.py` resolves every field via a parameterized DB lookup, a fixed keyword table, or a regex, never by handing the raw text to a model — so an adversarial answer just fails to resolve and gets re-asked, same as any garbage input. Confirmed a genuine injection attempt is still blocked outside PR intake. Worth re-examining if the intake ever grows a step that does feed free text to an LLM.
- **Deferred scope** (see the "Drafting a Purchase Requisition" section above): surplus-stock / DC-based PR branching, the Concorde-item exclusion check, document-upload PR creation, attachments, RFQ. Each needs its own spec/API before it can be built the same grounded way as the rest of this flow.
- **Slot-filling is fixed-order and single-shot.** If document upload is ever added, `pr_intake_tool.py`'s field resolution (already keyed by field name, not a counter) is reusable as-is, but `next_missing_field`/the question order would need to allow fields to arrive out of order.

### Vendor Agent — known gaps to close before this is more than a POC

- **Vendor pricing is sparsely seeded.** Only 3 of the 10 seed materials (steel packing wire, structural steel angle, cement) have any `pr_vendor_material_prices` rows at all — every other material has no vendor to pick, by design (see the PR-intake auto-skip behavior above), but that also means most "find me the cheapest vendor for X" questions on other materials will correctly report "nothing on file" rather than demonstrate the happy path. Seed more rows in `db/02_seed.sql` to exercise it further.
- **Same substring-containment matching as everywhere else** — `resolve_vendor()`/`resolve_material()` tolerate a natural sentence but not real fuzzy matching (synonyms, reordered words). A material with two plausible substring matches (e.g. "steel" matching both "Steel packing wire" and "Structural steel angle...") resolves to whichever the DB's tie-break happens to pick, not necessarily the one the customer meant.
- **No vendor identity field in the ERP payload.** The example submit-draft-PR payload has no `hpR_Vendor_Code`-equivalent, so a chosen vendor is appended to `hpR_Remarks` as free text instead of a structured field — confirm the real field name (if one exists) with your ERP schema and switch to it once known.
