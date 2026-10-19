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
  api/          # FastAPI routers (auth, conversations, messages, files, admin)
  core/         # settings, security, db session
  llm/          # openrouter.py (ONLY place that calls OpenRouter), catalog.py
  router/       # filter.py, classifier.py, policy.py, sticky.py, decision.py
  agent/        # loop.py, tools/ (web_search.py, run_python.py, connectors.py)
  models/       # SQLAlchemy models
  schemas/      # Pydantic schemas
backend/tests/
frontend/
sandbox/        # code-execution service
config/         # routing.yaml, connectors.yaml
docs/
```

## Commands

```bash
docker compose up -d db                      # start Postgres
cd backend && uv run alembic upgrade head    # migrate
cd backend && uv run pytest -q               # backend tests
cd backend && uv run ruff check . && uv run mypy app
cd frontend && npm run dev                   # UI on :3000
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

```
OPENROUTER_API_KEY=
DATABASE_URL=postgresql+asyncpg://relay:relay@db:5432/relay
JWT_SECRET=
FERNET_KEY=                 # encrypts OAuth refresh tokens
AWS_REGION=ap-south-1
S3_BUCKET=
DEFAULT_MONTHLY_CAP_USD=50
APP_BASE_URL=http://localhost:3000
```
