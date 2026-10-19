# Relay

Self-hosted team chat that routes each message to the best OpenRouter model for the task.

- Full design: `docs/SPEC.md` (source of truth; read the relevant section before changing anything)
- Build plan: `docs/TASKS.md` (work one phase at a time, in order)
- Routing policy: `config/routing.yaml`

## Stack

- Backend: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 async, Alembic, httpx, OpenAI SDK pointed at OpenRouter
- DB: PostgreSQL 16
- Frontend: Next.js (App Router), TypeScript, Tailwind
- Tests: pytest + respx (backend), Playwright (frontend smoke)
- Run: Docker Compose (`web`, `api`, `db`, `sandbox`, `caddy`)

## Layout

```
backend/app/
  api/          # FastAPI routers (auth, conversations, messages, files, connectors, admin)
  core/         # settings, security, db session
  llm/          # openrouter.py (ONLY place that calls OpenRouter), catalog.py
  router/       # config.py, filter.py, classifier.py, policy.py, sticky.py, decision.py
  agent/        # turn.py (one chat turn), history.py, stream.py (SSE), tools/
  agent/tools/  # web_search.py, run_python.py, connectors.py (MCP), registry.py
  services/     # budget, files (S3), attachments, oauth, virus_scan
  models/       # SQLAlchemy models
  schemas/      # Pydantic schemas
backend/evals/  # router vs. baseline eval harness
backend/tests/
frontend/       # Next.js app; e2e/ has Playwright smoke tests (API mocked)
sandbox/        # code-execution service; runtime/ is the per-job image
config/         # routing.yaml, connectors.yaml
deploy/         # production compose, Caddyfile, SSM loader, backups, AWS bootstrap
docs/
```

## Commands

```bash
docker compose up -d db s3                   # Postgres + local S3 (moto)
cd backend && uv run alembic upgrade head    # migrate
cd backend && uv run pytest -q               # backend tests (needs a relay_test DB)
cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy app tests evals
cd backend && uv run python -m app.cli create-admin --email you@x.com --name "You"
cd backend && uv run python -m evals.run --set evals/prompts.yaml   # router vs baseline eval
cd sandbox && uv run pytest -q               # security tests need: docker build -t relay-sandbox-runtime:1 sandbox/runtime
cd frontend && npm run dev                   # UI on :3000
cd frontend && npm run lint && npm run typecheck && npm run build && npm run test:e2e
docker compose up --build                    # everything
```

Use `uv` for Python dependencies. Update this section when a command changes.

## Hard rules

1. **Only `backend/app/llm/openrouter.py` talks to OpenRouter.** Every request it sends includes `provider: {"zdr": true, "data_collection": "deny"}`. A unit test asserts this for every code path; never weaken that test.
2. **Never invent model slugs.** Slugs live only in `config/routing.yaml` and must exist in the live catalog (`GET /api/v1/models/user`) with a ZDR endpoint (`GET /api/v1/endpoints/zdr`). Look them up; do not rely on memory.
3. **Check OpenRouter docs before using any OpenRouter feature.** Several features we use (server tools / web search) are beta and change often. Use the `openrouter-docs` MCP server (see setup below). If the docs disagree with `docs/SPEC.md`, stop and tell me instead of guessing.
4. **Store the model OpenRouter returns** in the response `model` field, not the one requested (fallbacks may answer).
5. **Cost comes from `usage.cost`** in the response (last SSE chunk when streaming). Never estimate cost when the real value is available.
6. **No message or file content in logs** above debug level.
7. **Every DB query that touches user data filters by `user_id`.** Add a cross-user access test for every new endpoint.
8. **Connector write actions require user approval** (`approval_required` SSE event). Never auto-approve.
9. **The sandbox has no network.** Do not add network access to make a test pass.
10. **Secrets come from environment variables.** Never commit `.env`; keep `.env.example` updated.

## Working style

- Start each phase in plan mode, show the plan, wait for approval, then implement.
- Write tests first for the router (`backend/app/router/`) and the OpenRouter client. Mock OpenRouter with respx; no live API calls in the test suite.
- Small commits, one concern each. Run tests, ruff and mypy before every commit.
- When a phase is done, tick its acceptance checklist in `docs/TASKS.md` and note anything deferred.
- If something in the spec looks wrong or underspecified, ask. Do not silently change the design.

## Setup for Claude Code (once)

```bash
claude mcp add --transport http openrouter-docs --scope project https://openrouter.ai/docs/_mcp/server
```

This writes `.mcp.json` so the whole team gets the OpenRouter docs server.

## Environment variables

See `.env.example` (kept up to date). Production values come from SSM Parameter Store (`docs/RUNBOOK.md`).
