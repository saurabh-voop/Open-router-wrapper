# Relay: Team Chat with Automatic Model Routing

Design spec, v1. Owner: Saurabh Salunkhe. Date: 2026-10-19.

Relay is a self-hosted chat app for a small team (2 to 5 people). Every message goes through a router that picks the most suitable model on OpenRouter for the task, shows which model answered and why, and lets the user override it. It supports web search, document/PDF chat, Python code execution, and connectors (Gmail, Drive, CRM).

This document is the source of truth for the build. `docs/TASKS.md` breaks it into phases for Claude Code; `CLAUDE.md` holds the working rules.

---

## 1. Decisions already made

| Topic | Decision | Why |
|---|---|---|
| Users | Saurabh + team, 2 to 5 people | Internal tool, not a product (yet) |
| Optimise for | Answer quality first; cost is secondary | Stated requirement |
| Switching within a chat | Re-route only when the task changes (sticky routing) | Avoids cache loss and style drift from per-message switching |
| User control | Automatic, with visible model + override | Overrides double as training data for the router |
| Build | Own frontend + own backend, no fork of an existing chat UI | Stated requirement; keeps branding/licensing free |
| v1 tools | Web search, file/PDF chat, code execution, connectors | All four requested |
| Budget | Per-user monthly cap in USD, admin can raise it | Quality-first + agentic tools can get expensive |
| Auth | Email + password, admin-created accounts, no self sign-up | Small team |
| Client data | Will go through the system. Baseline: ZDR + no-training on every request. Stricter rules still open (see section 14) | User is not sure yet |
| Hosting | AWS, Docker Compose on one instance, region ap-south-1 (Mumbai) | Matches existing stack |

---

## 2. What the research says, and how it shapes this design

These findings come from the investigation that preceded this spec. Each one drives a concrete rule below.

1. **Routers plateau, and simple ones are as good as complex ones.** A study of 21 routing methods (Rice/Amazon, arXiv 2606.07587, May 2026) found the top methods within ~0.2 percentage points of each other, with kNN in the top two on every benchmark, and all of them 10 to 30 points below a perfect oracle. Most of the gain comes from coarse task structure (code vs. math vs. writing).
   → **Rule:** v1 routes by task category with a hand-written policy. No trained router until there is real usage data, and then start with kNN.

2. **"Always use the best single model" is a hard baseline.** LLMRouterBench (arXiv 2601.07206, Jan 2026) found several routers, including OpenRouter's commercial auto-router, did not beat the best single model. A separate study (arXiv 2608.14641) found a fixed "always mid-tier" policy matched the top router on three of four benchmarks.
   → **Rule:** the eval harness (Phase 4) must compare the router against "always use the single best model". If the router does not win on our own prompts, set every category to the same model and keep the rest of the app.

3. **With quality as the goal, routing is about capability, not cost.** Gains for performance-oriented routing are small; savings for cost-oriented routing are large (Avengers-Pro: same quality as the top model at ~27% lower cost).
   → **Rule:** route on hard capability constraints first (vision, file input, context length, tool support, ZDR availability), then on task category.

4. **Switching models mid-conversation has real costs.** It loses the provider prompt cache and puts a system prompt in front of a model it was not tuned for. OpenRouter's own auto-router pins the model for the rest of a conversation.
   → **Rule:** sticky routing with a confidence threshold (section 5.4).

5. **Preference-based routing is practical.** Arch-Router (Katanemo, used by HuggingChat Omni) maps a query to a domain/action policy written in plain language, and each policy maps to a model the team trusts. Its documented weakness: overlapping policy descriptions make routing arbitrary.
   → **Rule:** categories are few, written to be mutually exclusive, and each has an example list in `config/routing.yaml`.

6. **Routers can learn from implicit feedback.** Bandit-style routers learn from the outcome of the chosen model only, including signals like the user retrying.
   → **Rule:** log every routing decision with overrides, regenerations and thumbs (section 9). This is the dataset for a v2 learned router.

---

## 3. Architecture

```
 Browser (Next.js app)
     │  HTTPS, SSE for streaming
     ▼
 Caddy (TLS)
     │
     ▼
 FastAPI backend ──────────────► OpenRouter /api/v1/chat/completions
  ├─ auth                         (provider.zdr=true, data_collection=deny)
  ├─ conversations/messages
  ├─ router  (filter → classify → policy → stickiness)
  ├─ agent loop (tool calls)
  │    ├─ web search  → OpenRouter server tool (runs at OpenRouter)
  │    ├─ run_python  → sandbox service (local, no network)
  │    └─ connector tools → MCP client → MCP servers (Gmail/Drive/CRM)
  ├─ files  → S3 (SSE-KMS)
  ├─ budgets/usage
  └─ model catalog sync (hourly)
     │
     ▼
 PostgreSQL 16
```

Containers in `docker-compose.yml`: `web` (Next.js), `api` (FastAPI, uvicorn), `db` (Postgres, or RDS later), `sandbox` (code runner), `caddy`.

**Why a separate backend route rather than calling OpenRouter from the browser:** the API key, privacy parameters, budgets, tool execution and routing logs must all be enforced server-side.

---

## 4. Tech stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 (async), Alembic, httpx |
| LLM client | OpenAI Python SDK pointed at `https://openrouter.ai/api/v1`, OpenRouter-specific fields via `extra_body` |
| DB | PostgreSQL 16 |
| Frontend | Next.js (App Router), TypeScript, Tailwind CSS |
| Streaming | Server-Sent Events from backend to browser |
| Files | AWS S3, server-side encryption, presigned URLs never exposed for other users' files |
| Sandbox | Separate container that runs each job in a fresh, network-less, resource-limited container |
| Connectors | Python MCP client SDK (`mcp`) |
| Tests | pytest, respx (mock OpenRouter), Playwright smoke tests |
| Deploy | Docker Compose on EC2, Caddy for TLS, secrets in AWS SSM Parameter Store |

---

## 5. The router

The router runs once per user message, before the model call. The tool loop that follows stays on the chosen model.

### 5.1 Inputs

- The new user message, including attachments (type, size, page count, estimated tokens)
- The conversation state: `current_model`, `current_category`, `locked_model`, `confidential`
- Estimated total prompt tokens (history + attachments + system prompt)
- The model catalog (section 7)
- `config/routing.yaml`

### 5.2 Step 1: hard capability filter

Build the candidate set from the catalog. Drop any model that fails one of these:

| Need | Check against catalog field |
|---|---|
| Image attached | `architecture.input_modalities` contains `image` |
| PDF/file attached | `input_modalities` contains `file`, or OpenRouter's file-parser will be used (allowed, but prefer native) |
| Prompt size | `context_length` ≥ estimated tokens × 1.2 |
| Tools needed (code, connectors) | `supported_parameters` contains `tools` |
| Client-data policy | model has at least one endpoint in the ZDR list (`/api/v1/endpoints/zdr`) |
| Admin allowlist | model slug appears in `routing.yaml` |

If the filter leaves nothing, return an error to the user that says which constraint failed (for example, "no ZDR model with vision and 400k context is available"). Never silently relax the privacy filter.

### 5.3 Step 2: classify the task

A small, fast classifier model (set in `routing.yaml` as `classifier_model`, must itself pass the ZDR filter) receives the last few turns and the new message and returns strict JSON:

```json
{
  "category": "coding",
  "confidence": 0.86,
  "is_followup": false,
  "needs": ["code_execution"],
  "reason": "asks to write and run a Python script on the attached CSV"
}
```

- Categories are fixed and listed in `routing.yaml` (v1 set below).
- `needs` can include `web_search`, `code_execution`, `connectors`, `vision`, `long_context`.
- Before calling the classifier, apply cheap deterministic rules that short-circuit it: an image attachment adds `vision`, a slash command like `/code` forces a category, an empty or very short follow-up ("shorter", "again", "translate it") is treated as `is_followup: true`.
- Timeout 3 seconds. On timeout or invalid JSON, keep the current model; on a new conversation, use the `default` category.

**v1 categories** (mutually exclusive by design):

| Category | Covers | Typical needs |
|---|---|---|
| `coding` | Writing, reviewing, debugging code; SQL; regex | code_execution |
| `data_analysis` | Analysing tables/CSV/Excel, charts, calculations | code_execution |
| `research` | Questions about current facts, prices, news, companies | web_search |
| `document_qa` | Questions about an attached or stored document | long_context |
| `writing` | Emails, proposals, rewriting, tone, translation | none |
| `reasoning` | Planning, strategy, maths, multi-step problems | none |
| `actions` | Read or act on Gmail, Drive, CRM | connectors |
| `general` | Everything else, small talk, quick facts | none |

### 5.4 Step 3: stickiness (when to switch)

```
if conversation.locked_model:
    use locked_model (still apply the hard filter; if it fails, tell the user why)
elif new conversation:
    use policy[category]
elif hard filter rejects current_model:
    switch to policy[category]            # forced switch, e.g. an image was attached
elif result.is_followup or result.category == current_category:
    keep current_model
elif result.confidence >= switch_threshold (default 0.75)
     and policy[category].primary != current_model:
    switch to policy[category]
else:
    keep current_model
```

A switch carries the full message history. The UI shows a one-line notice in the thread: "Switched to Model X for coding."

### 5.5 Step 4: policy lookup

`routing.yaml` maps each category to an ordered list. The first model that survives the hard filter is the primary; the next two are sent as OpenRouter fallbacks (`models` array) for outages and rate limits. Requests are billed at the model that actually answered, which OpenRouter returns in the response `model` field. Always store that field, not the requested one.

### 5.6 Outputs

The router returns a `RoutingDecision` (persisted, section 9) and emits an SSE `route` event before any tokens:

```json
{"type":"route","model":"<slug>","category":"coding","reason":"asks to write and run a Python script","switched":true,"forced":false}
```

### 5.7 What the router must never do

- Route to a model outside `routing.yaml`.
- Drop the privacy parameters.
- Switch models in the middle of a tool loop.
- Call the classifier more than once per user message.

---

## 6. Calling OpenRouter

All calls go through one module, `backend/app/llm/openrouter.py`. No other module may call OpenRouter directly.

Every chat request body includes:

```json
{
  "model": "<primary slug>",
  "models": ["<fallback 1>", "<fallback 2>"],
  "provider": { "zdr": true, "data_collection": "deny" },
  "stream": true
}
```

Also turn on ZDR enforcement in the OpenRouter account privacy settings. The per-request flag can only add enforcement, not remove account-level enforcement, so having both is safe.

**Usage and cost.** OpenRouter returns usage on every response without extra parameters. For streaming, it arrives in the last SSE chunk. Read `usage.cost` (amount charged), `usage.prompt_tokens`, `usage.completion_tokens`, and `usage.prompt_tokens_details.cached_tokens`.

**Errors.** Retry once on 429/5xx with backoff when fallbacks are exhausted. Surface provider errors to the user in plain language.

**Verification rule.** OpenRouter's API changes often (several features used here are in beta). Before implementing any OpenRouter feature, check the current docs (the OpenRouter docs MCP server is configured for Claude Code, see `CLAUDE.md`).

---

## 7. Model catalog

A background job runs on startup and every hour:

1. `GET /api/v1/models/user`: models filtered by the account's provider preferences and privacy settings.
2. `GET /api/v1/endpoints/zdr`: endpoints with a ZDR policy.
3. Upsert into `models` table: slug, name, context_length, input_modalities, supported_parameters, prompt/completion price, `has_zdr_endpoint`, `last_seen_at`.
4. Mark models not seen for 24 hours as unavailable.
5. Log a warning (and show it on the admin page) if any slug in `routing.yaml` is missing or has no ZDR endpoint.

---

## 8. Tools

The backend runs an agent loop: call model → if it returns tool calls, run them → append results → call again. Maximum 10 steps per user message, configurable. The SSE stream emits `tool_start` and `tool_end` events so the UI can show progress.

### 8.1 Web search

- Implemented as OpenRouter's server tool: `{"type": "openrouter:web_search", "parameters": {"max_results": 5, "max_total_results": 15}}`. OpenRouter runs the search; the model decides when to search.
- The tool is in beta. Pin behaviour with tests and check the docs before changing it.
- Citations come back as `url_citation` annotations. Render them as numbered source links under the answer.
- Engine `auto` uses the provider's native search where available and otherwise Exa (~$0.007 per search on Exa's default mode).
- **Privacy caveat:** ZDR enforcement covers model inference only, not tools like web search. The search query leaves through the search engine. When a conversation is marked **confidential** (section 10), web search is disabled.

### 8.2 Files and PDFs

- Upload → virus/type check → store in S3 under `files/{user_id}/{uuid}` → row in `attachments`.
- Limits: 25 MB per file, 10 files per message (configurable).
- Sending to the model: as an OpenAI-style `file` content part. OpenRouter passes the PDF natively if the model supports file input; otherwise it parses it (default engine falls back to `mistral-ocr`, which is paid). Do not use the `pdf-text` engine; it is deprecated.
- Images: `image_url` content parts with base64 data.
- Text-like files (CSV, TXT, MD, code): read on the server and inline as text, truncated with a notice if huge; for CSV/Excel also mount them into the sandbox for `data_analysis`.
- v1 sends whole documents and relies on long-context models. RAG over a document store is v2.

### 8.3 Code execution

- Function tool `run_python(code: str)`, available only when the category needs it or the user enables it.
- Runs in the `sandbox` service, which starts a fresh container per job from a pinned image with pandas, numpy, matplotlib, openpyxl.
- Limits: no network, 60 s wall time, 1 CPU, 1 GB RAM, 100 MB disk, read-only root filesystem, non-root user. Use gVisor (`runsc`) if available on the host.
- The conversation's attachments are mounted read-only at `/data`. Files written to `/output` (charts, CSVs) are returned, stored in S3, and shown in the chat.
- Return to the model: stdout/stderr (truncated to 20 KB), list of output files.
- Code runs locally, so client data never leaves the server for this step.

### 8.4 Connectors (MCP)

- The backend is an MCP client. Each connector is an MCP server configured in `config/connectors.yaml` (command or URL, required scopes).
- Gmail and Drive need per-user Google OAuth. Store refresh tokens encrypted (Fernet key from SSM). The CRM depends on which CRM the team uses (open question).
- Tools from connected servers are offered to the model only for the `actions` category or when the user enables a connector in the chat.
- **Write actions need confirmation.** Any tool marked as writing (send email, create file, update CRM record) pauses the loop and shows the exact action in the UI with Approve / Reject. Read-only tools run directly.
- Connector results count as client data: when a conversation is confidential, connectors still work (data stays between Relay, the MCP server and a ZDR model), but web search stays off.
- Connectors are the largest piece of work and are scheduled last.

---

## 9. Data model

```
users              id, email (unique), password_hash (argon2), role (admin|member),
                   monthly_cap_usd, is_active, created_at

conversations      id, user_id, title, current_model, current_category,
                   locked_model (nullable), confidential (bool), created_at, updated_at

messages           id, conversation_id, role (user|assistant|tool|system), content (jsonb),
                   model (as returned by OpenRouter), prompt_tokens, completion_tokens,
                   cached_tokens, cost_usd, latency_ms, created_at

attachments        id, conversation_id, message_id, s3_key, filename, mime, size_bytes,
                   sha256, page_count, est_tokens, created_at

routing_decisions  id, message_id, category, confidence, is_followup, needs (jsonb),
                   candidates (jsonb), chosen_model, answered_model, switched (bool),
                   forced (bool), reason, classifier_latency_ms,
                   overridden_to (nullable), regenerated (bool), created_at

feedback           id, message_id, user_id, rating (-1|1), comment, created_at

tool_runs          id, message_id, tool, input (jsonb), output_summary, status,
                   duration_ms, approved_by (nullable), created_at

usage_ledger       user_id, month (YYYY-MM), cost_usd           -- primary key (user_id, month)

models             slug (pk), name, context_length, input_modalities (jsonb),
                   supported_parameters (jsonb), price_prompt, price_completion,
                   has_zdr_endpoint, available, last_seen_at

oauth_tokens       id, user_id, provider, encrypted_refresh_token, scopes, created_at
```

---

## 10. Privacy and security

- **Inference:** `provider.zdr=true` and `provider.data_collection="deny"` on every call, plus account-level ZDR in OpenRouter settings. OpenRouter states that it does not retain prompts itself unless prompt logging is opted in; keep that off.
- **Caching:** OpenRouter treats in-memory prompt caching as not retaining data, so cached endpoints remain eligible under ZDR.
- **Confidential mode:** a per-conversation toggle, default on for any conversation with an attachment. It disables web search and hides any non-ZDR option.
- **Storage:** chat history and files live in our Postgres and S3 (Mumbai). Backups encrypted. Deleting a conversation deletes its S3 objects.
- **Secrets:** OpenRouter key, JWT secret, Fernet key in SSM, loaded as environment variables at start. Never in the repo.
- **Auth:** argon2 hashes, JWT in an httpOnly, Secure, SameSite=Lax cookie, 7-day expiry, login rate limit.
- **Isolation:** every query filters by `user_id`. Tests must cover cross-user access to conversations and files.
- **Logs:** never log message content or file content at info level.

---

## 11. Budgets

- Each user has `monthly_cap_usd` (default set in config, admin-editable).
- Before a model call: if the user's month-to-date spend ≥ cap, refuse with a clear message and the admin's name.
- During a tool loop: re-check after each step; stop the loop if the cap is crossed.
- After each call: add `usage.cost` to `usage_ledger` and the message row.
- Web search and file-parsing charges appear in OpenRouter's cost; include them.
- Warn in the UI at 80% of the cap.
- Admin page: spend per user per month, spend per model, routing category mix.

---

## 12. API (backend)

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/login`, `/auth/logout` | Session cookie |
| GET | `/me` | Current user, spend, cap |
| GET/POST | `/conversations` | List / create |
| GET/PATCH/DELETE | `/conversations/{id}` | Read, rename, lock model, toggle confidential, delete |
| POST | `/conversations/{id}/messages` | Send a message; response is an SSE stream |
| POST | `/messages/{id}/regenerate` | Optional `model` to re-run with a specific model |
| POST | `/messages/{id}/feedback` | Thumbs up/down |
| POST | `/tool-runs/{id}/approve`, `/reject` | Connector write confirmation |
| POST | `/files` | Upload |
| GET | `/models` | Allowed models with capabilities, for the override menu |
| Admin | `/admin/users`, `/admin/usage`, `/admin/routing` | Manage users and caps, view spend and decisions |

**SSE event types:** `route`, `token`, `tool_start`, `tool_end`, `approval_required`, `citation`, `file`, `usage`, `done`, `error`.

---

## 13. Frontend

- **Layout:** conversation list on the left, thread in the middle, input at the bottom with attach button and toggles (web search, confidential, connectors).
- **Per answer:** a small badge with the model name; hover shows the category and reason. Menu: "Re-run with…" (list from `/models`), "Lock this chat to this model", thumbs up/down.
- **Thread notices:** "Switched to X for coding", tool progress lines, approval cards for connector writes, citations under answers.
- **Header:** month-to-date spend vs. cap.
- **Admin pages:** users and caps, spend charts, a table of recent routing decisions with overrides highlighted.
- Markdown rendering with code highlighting, tables, and copy buttons. Charts from the sandbox shown as images.

---

## 14. Open questions (do not block v1)

1. **Client data rules.** Is ZDR + no-training enough, or do some contracts ban certain providers or require data to stay in India? If India-only is ever required, cloud inference through OpenRouter is likely ruled out for those conversations.
2. **CRM.** Which CRM, and does it have an MCP server or an API we wrap ourselves?
3. **Model mapping.** `config/routing.yaml` ships with placeholders. Fill it from the current catalog in Phase 3 and confirm with the team.
4. **Google OAuth.** Connectors need a Google Cloud OAuth client; does PaiKane's Google Workspace allow internal OAuth apps?

---

## 15. Success criteria for v1

- The router beats or matches "always use the single best model" on the team's own eval set (Phase 4). If it does not, ship with routing off and keep collecting data.
- Fewer than 15% of assistant messages get a manual re-run with another model in the first month.
- No request reaches OpenRouter without the privacy parameters (enforced by a unit test on the client module).
- No user exceeds their cap by more than one in-flight request.
- p50 time to first token under 3 seconds excluding tool calls (router + classifier overhead under 800 ms).

---

## 16. v2 and later

- Learned router: kNN over embeddings of past messages with labels from overrides, regenerations and thumbs.
- RAG over a shared team document store.
- OpenAI-compatible endpoint (`/v1/chat/completions` with model `auto`) so other internal agents can use the router.
- Self-hosted classifier (for example Arch-Router-1.5B) if classifier latency or cost becomes an issue.
- Image generation.

---

## References

- OpenRouter docs: provider routing and ZDR (https://openrouter.ai/docs/guides/features/zdr), web search server tool (https://openrouter.ai/docs/guides/features/server-tools/web-search), PDF inputs (https://openrouter.ai/docs/features/multimodal/pdfs), models API (https://openrouter.ai/docs/models), model fallbacks (https://openrouter.ai/docs/guides/routing/model-fallbacks), usage accounting (https://openrouter.ai/docs/guides/administration/usage-accounting)
- The Routing Plateau, arXiv 2606.07587
- LLMRouterBench, arXiv 2601.07206
- Task- and Session-Level Model Routing, arXiv 2608.14641
- Avengers-Pro, arXiv 2508.12631
- RouteLLM, ICLR 2025, arXiv 2406.18665
- Arch-Router, arXiv 2506.16655
- PILOT (bandit routing under budgets), EMNLP Findings 2025, arXiv 2508.21141
