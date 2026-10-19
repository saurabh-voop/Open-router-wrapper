# Relay build plan for Claude Code

Work through the phases in order. Each phase ends with something that runs and is tested.

**How to use this file:** open the repo in Claude Code, press Shift+Tab to enter plan mode, and paste the phase's prompt. Review the plan, approve it, let it build, then check the acceptance list yourself before moving on.

Phases 0 to 5 give you a usable app (chat + routing + files + web search). Phases 6 to 9 add code execution, budgets, connectors and deployment.

---

## Phase 0: Scaffold

**Prompt**
> Read CLAUDE.md and docs/SPEC.md sections 3, 4 and 9. Scaffold the monorepo: `backend/` (FastAPI app with health endpoint, settings via pydantic-settings, async SQLAlchemy session, Alembic with the full data model from SPEC section 9), `frontend/` (Next.js App Router + TypeScript + Tailwind, single placeholder page), `docker-compose.yml` with `api`, `web`, `db`. Add ruff, mypy, pytest, a GitHub Actions workflow running them, `.env.example`, and `.gitignore`. Do not implement features yet.

**Done when**
- [ ] `docker compose up --build` starts all three services; `GET /health` returns 200 *(compose file validates and `/health` is tested; full `up` not yet run: the build environment is rate-limited by Docker Hub)*
- [x] `alembic upgrade head` creates every table in SPEC section 9 (`tests/test_schema.py`)
- [x] `pytest`, `ruff`, `mypy` pass in CI (`.github/workflows/ci.yml`; passing locally)
- [x] OpenRouter docs MCP server added (`.mcp.json` committed)

---

## Phase 1: OpenRouter client, catalog, single-model chat

**Prompt**
> Read SPEC sections 6 and 7. Before writing code, use the openrouter-docs MCP server to confirm the current request/response shapes for chat completions with streaming, provider preferences (`zdr`, `data_collection`), the `models` fallback array, `GET /api/v1/models/user`, `GET /api/v1/endpoints/zdr`, and usage accounting (`usage.cost`). Report any difference from the spec before continuing. Then build `backend/app/llm/openrouter.py` (streaming chat, fallbacks, privacy params always set, usage parsing) and `backend/app/llm/catalog.py` (sync job into the `models` table). Add a temporary endpoint that streams a reply from one fixed model given in an env var. Tests with respx only.

**Done when**
- [ ] A unit test proves every request body sent by the client contains `provider.zdr = true` and `provider.data_collection = "deny"`
- [ ] Streaming works end to end with a real key (manual check), and `usage.cost` is captured from the final chunk
- [ ] Catalog sync fills `models` with context length, modalities, supported parameters, prices and `has_zdr_endpoint`
- [ ] The answered model is taken from the response, not the request

---

## Phase 2: Auth, conversations, chat UI

**Prompt**
> Read SPEC sections 10, 12 and 13. Implement email/password auth (argon2, JWT in httpOnly cookie, login rate limit, admin-only user creation with a CLI command to create the first admin), conversations and messages CRUD, and `POST /conversations/{id}/messages` as an SSE stream still using the fixed model from Phase 1. Build the frontend: login page, conversation list, thread with markdown + code highlighting, streaming input, model badge on each answer. Every endpoint gets a cross-user access test.

**Done when**
- [ ] Two users cannot see each other's conversations (tests)
- [ ] Chat streams in the browser and survives a page reload (history loads from DB)
- [ ] Each assistant message stores model, tokens, cached tokens and cost
- [ ] Badge shows the model that answered

---

## Phase 3: Router v1

**Prompt**
> Read SPEC section 5 and section 2 carefully. First, fill `config/routing.yaml`: query the live catalog (`/api/v1/models/user` + `/api/v1/endpoints/zdr`), propose a quality-first model list for each category and for `classifier_model` and `baseline_model`, with one line of reasoning each, and show me the proposal before writing it. Then implement `backend/app/router/` exactly as specified: hard capability filter, deterministic pre-rules, classifier with strict JSON and 3 s timeout, sticky switching with the threshold, policy lookup with fallbacks, `RoutingDecision` persisted to `routing_decisions`, and the SSE `route` event. Write the tests first, including: follow-up keeps the model; low-confidence category change keeps the model; image attachment forces a switch to a vision model; locked model is respected; filter failure gives a clear error; classifier timeout keeps the current model. Add the UI pieces: route notice in the thread, "Re-run with…", "Lock this chat", thumbs up/down, all logged.

**Done when**
- [ ] `routing.yaml` has real slugs, all present in the catalog with ZDR endpoints; the app refuses to start otherwise
- [ ] All router tests above pass
- [ ] Router overhead (filter + classifier) p50 under 800 ms in manual testing
- [ ] Overrides, regenerations and thumbs are written to `routing_decisions` / `feedback`

---

## Phase 4: Eval harness (decides whether routing stays on)

**Prompt**
> Read SPEC sections 2 and 15. Build `backend/evals/`: a YAML set of prompts (I will add real team prompts; seed it with 10 realistic ones per category for an industrial sales team: gensets, panels, AMC, tenders, client emails, CSV analysis, code), a runner that sends each prompt through (a) the router and (b) `baseline_model` only, and a pairwise LLM judge (judge model from config, ZDR, randomised answer order, asks which answer is better or tie, with a short reason). Output a markdown report: win/tie/loss per category, cost per category, and the 10 largest losses with both answers. Make it a CLI: `uv run python -m evals.run --set evals/prompts.yaml`.

**Done when**
- [ ] Report generated for the seed set
- [ ] Team has added at least 50 real prompts and the report is re-run
- [ ] Decision recorded in this file: keep routing, adjust `routing.yaml`, or set all categories to the baseline model
- [ ] At least 20 report items spot-checked by a person (LLM judges have biases)

---

## Phase 5: Files/PDF and web search

**Prompt**
> Read SPEC sections 8.1, 8.2 and 10. Confirm current details with the openrouter-docs MCP server (file content parts and file-parser engines; the `openrouter:web_search` server tool parameters and `url_citation` annotations) before coding. Implement uploads to S3 (use LocalStack or MinIO in docker-compose for dev), attachment token estimation, sending PDFs as `file` parts and images as `image_url` parts, inlining text files, the web search server tool for the `research` category or when the user toggles it, citation rendering, and the confidential toggle (default on when a conversation has an attachment; disables web search). Feed attachment needs into the router's hard filter.

**Done when**
- [ ] A 50-page PDF can be asked about; router picks a model with enough context and file support
- [ ] Web search answers show numbered citations
- [ ] Confidential conversations never include the web search tool (test)
- [ ] Deleting a conversation deletes its S3 objects (test)

---

## Phase 6: Code execution sandbox

**Prompt**
> Read SPEC section 8.3. Build `sandbox/`: a small service that accepts a job (code + list of attachment keys), starts a fresh container from a pinned image (python, pandas, numpy, matplotlib, openpyxl) with no network, 60 s timeout, 1 CPU, 1 GB RAM, 100 MB disk, read-only root, non-root user, attachments mounted read-only at /data, writable /output. Use gVisor if available. Add the `run_python` function tool to the agent loop (max 10 steps, `tool_start`/`tool_end` events), store /output files in S3 and show them in the thread. Write security tests: network access fails, writing outside /output fails, infinite loop is killed, fork bomb is contained.

**Done when**
- [ ] "Plot monthly totals from this CSV" returns a chart in the chat
- [ ] All four security tests pass
- [ ] The model never switches during a tool loop (test)

---

## Phase 7: Budgets and admin

**Prompt**
> Read SPEC sections 11 and 13 (admin pages). Implement per-user monthly caps: pre-call check, re-check after each tool step, ledger updates from `usage.cost`, 80% warning in the UI, spend in the header. Admin pages: users and caps, spend per user per month, spend per model, recent routing decisions with overrides highlighted, and the catalog warnings from SPEC section 7.

**Done when**
- [ ] A user at their cap is blocked with a clear message (test)
- [ ] A tool loop stops when the cap is crossed mid-loop (test)
- [ ] Admin can raise a cap and the user can continue immediately

---

## Phase 8: Connectors (MCP)

**Prompt**
> Read SPEC section 8.4. Implement the MCP client in `backend/app/agent/tools/connectors.py` driven by `config/connectors.yaml`. Start with Google Drive (read-only), then Gmail (read, then draft, then send). Implement per-user Google OAuth with encrypted refresh tokens. Mark each tool read or write; write tools pause the loop with an `approval_required` event and an Approve/Reject card in the UI. Find current, maintained MCP servers for Gmail and Drive and show me the options with their permissions before choosing. CRM comes later once we decide which CRM.

**Done when**
- [ ] "Find the last email from <client>" works for a connected user
- [ ] Sending an email always shows an approval card; rejecting it sends nothing (test)
- [ ] Revoking Google access in the UI deletes the stored token

---

## Phase 9: Deploy to AWS

**Prompt**
> Read SPEC sections 3 and 10. Prepare production deployment: production compose file with Caddy (TLS), secrets from AWS SSM Parameter Store, S3 bucket with encryption and private access, daily encrypted Postgres backups to S3, log rotation, and a `docs/RUNBOOK.md` covering deploy, rollback, backup restore, rotating the OpenRouter key, and adding a user. Use ap-south-1.

**Done when**
- [ ] App reachable over HTTPS on the team domain
- [ ] Backup restore tested once on a scratch instance
- [ ] RUNBOOK.md written and followed once by someone other than the author

---

## Decisions log

Record decisions made during the build here (date, decision, why).

- 2026-10-19: Build own UI instead of forking Open WebUI/LibreChat. Sticky routing, auto + override, quality first, per-user caps, email/password auth.
- 2026-10-19: Project lives at the repo root. Phases are planned then built without a blocking approval step (owner's call).
- 2026-10-19: Data-model additions beyond SPEC section 9 (marked `ext:` in `backend/app/models/entities.py`): `users.name` (for the budget-refusal message), `messages.parent_id` + `messages.superseded` (group an agent-loop turn; regenerations keep the old turn for routing data but hide it), `attachments.user_id` (uploads exist before a message), `tool_runs.tool_call_id` (resume after approval). Money columns are `Numeric(18,10)`, catalog prices `Numeric(24,16)`, because OpenRouter per-token prices are ~1e-8 USD.
- 2026-10-19: `routing_decisions.message_id` points at the assistant message the decision produced, so each regeneration gets its own row.
- 2026-10-19: Browser reaches the API only via same-origin `/api/*` (Next.js rewrite in dev, Caddy in prod) so the auth cookie is first-party.
- 2026-10-19: No Google Fonts (`next/font/google` fetches at build time; system font stack instead).
- 2026-10-19: Sandbox uses a docker-socket-proxy that only allows container create/start/wait/logs/remove, never the raw socket (owner's call).
- 2026-10-19: Budgets allow parallel requests per user; overshoot is bounded by the number of concurrent requests, accepted for a 2-5 person team (owner's call).
- 2026-10-19: Gmail/Drive MCP servers: Claude picks and documents the choice in `docs/CONNECTORS.md`; swappable via `config/connectors.yaml` (owner's call).

