# Relay

Relay is a team chat app that picks the best OpenRouter model for each task. It supports web search, document chat, code execution and Gmail/Drive connectors.

## Docs

- `docs/SPEC.md`: what we're building and why (the source of truth)
- `docs/TASKS.md`: the build, phase by phase, with acceptance checklists and the decisions log
- `docs/API.md`: the backend API and its SSE events
- `docs/CONNECTORS.md`: the Gmail/Drive MCP server choice and Google OAuth setup
- `docs/RUNBOOK.md`: deploying, rolling back, backups, key rotation, adding users
- `CLAUDE.md`: the rules Claude Code follows in this repo

## Layout

```
backend/    FastAPI API: router, agent loop, OpenRouter client, auth, files, admin, evals/
frontend/   Next.js app (chat, settings, admin) with Playwright smoke tests
sandbox/    code-execution service + per-job runtime image (+ security tests)
config/     routing.yaml (model policy), connectors.yaml (MCP servers and tool allowlist)
deploy/     production compose, Caddy, SSM secrets loader, backups, AWS bootstrap
```

## Run locally

Requirements: Docker, [uv](https://docs.astral.sh/uv/), Node 22, and an OpenRouter API key.

1. Copy the env file:
   ```sh
   cp .env.example .env
   ```
   Then set `OPENROUTER_API_KEY` and `SANDBOX_TOKEN` in `.env`.
2. Make sure `config/routing.yaml` has real model slugs (TASKS Phase 3).
3. Start the stack:
   ```sh
   docker compose up --build
   ```
4. Create the first admin:
   ```sh
   docker compose exec api python -m app.cli create-admin --email you@paikane.com --name "You"
   ```
5. Open http://localhost:3000.

### Without Docker, for development

```sh
docker compose up -d db s3
cd backend
uv run alembic upgrade head
uv run uvicorn app.main:app --reload    # API on :8000

cd frontend
npm install
npm run dev                             # UI on :3000; /api is proxied to :8000
```

## Checks

```sh
# Backend: needs Postgres on :5432 with a relay_test DB
cd backend && uv run pytest -q && uv run ruff check . && uv run mypy app tests evals

# Sandbox
cd sandbox && uv run pytest -q
# For the security tests, build the runtime image first:
#   docker build -t relay-sandbox-runtime:1 sandbox/runtime

# Frontend
cd frontend && npm run lint && npm run typecheck && npm run build && npm run test:e2e

# Router vs. baseline eval: needs a key and real slugs
cd backend && uv run python -m evals.run --set evals/prompts.yaml
```
