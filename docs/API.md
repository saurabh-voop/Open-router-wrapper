# Relay API contract

Backend: FastAPI. The browser always calls it on the same origin under `/api/*`.
Next.js rewrites `/api/:path*` to the API in development; Caddy does it in production.
Paths below are API paths, so `GET /me` is fetched as `/api/me`.

Auth is an httpOnly session cookie set by `POST /auth/login`. Send `credentials: "include"`.
Errors use FastAPI's shape: `{"detail": "human readable message"}`.
Status codes: 401 (not signed in), 403 (not admin), 404 (not found, including other users' data), 409 (conflict), 422 (validation).
Money values are decimal strings, e.g. `"0.0021"`. Timestamps are ISO 8601.

## Auth

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/auth/login` | `{email, password}` | `User`; sets the cookie. 401 wrong credentials, 429 rate limited |
| POST | `/auth/logout` | none | 204 |
| GET | `/me` | none | `{user: User, month_spend_usd, monthly_cap_usd, budget_warning: bool, budget_exceeded: bool}` |

`User = {id, email, name, role: "admin"|"member", monthly_cap_usd, is_active, created_at}`

## Conversations

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/conversations` | none | `Conversation[]`, newest first |
| POST | `/conversations` | `{title?, confidential?}` | `Conversation` (201) |
| GET | `/conversations/{id}` | none | `Conversation & {messages: Message[]}` |
| PATCH | `/conversations/{id}` | any of `{title, locked_model (null unlocks), confidential}` | `Conversation` |
| DELETE | `/conversations/{id}` | none | 204; also deletes its files |

`Conversation = {id, title, current_model, current_category, locked_model, confidential, created_at, updated_at}`

`Message`, as displayed in the thread (user messages and final answers only):

```
{
  id, role: "user"|"assistant", parent_id, text,
  status: "streaming"|"awaiting_approval"|"done"|"error",
  error: string|null,
  citations: [{index, url, title}],
  files: [{attachment_id, filename, mime}],          // outputs, e.g. charts from run_python
  attachments: [{id, filename, mime, size_bytes, page_count, est_tokens}],  // user uploads
  tool_runs: [{id, tool, status, input, output_summary, duration_ms}],
  model, prompt_tokens, completion_tokens, cached_tokens, cost_usd,
  routing: {category, reason, switched, forced, chosen_model, answered_model, overridden_to} | null,
  rating: -1|1|null,
  created_at
}
```

## Sending a message (SSE)

`POST /conversations/{id}/messages`, with body:

```
{text, attachment_ids?: uuid[], web_search?: bool, code_execution?: bool, connectors?: bool}
```

The response is `text/event-stream`. Each event is one line, `data: <json>`, followed by a blank line.
Every JSON object has a `type`. Use `fetch()` and read the body stream (EventSource cannot POST).
A 409 before the stream starts means an answer is still running, or an approval is pending.

| type | fields | meaning |
|---|---|---|
| `route` | `message_id, model, category, reason, switched, forced, confidential` | Sent first. Show "Switched to X for category" when `switched` |
| `token` | `text` | Append to the answer |
| `tool_start` | `tool_run_id, tool, input` | Show a progress line |
| `tool_end` | `tool_run_id, tool, ok, summary, duration_ms` | Close the progress line |
| `file` | `attachment_id, filename, mime` | Output file; images render inline from `GET /files/{attachment_id}` |
| `approval_required` | `tool_run_id, tool, input` | Show an Approve/Reject card; the stream then ends with `done {paused: true}` |
| `citation` | `index, url, title` | Numbered source under the answer |
| `usage` | `cost_usd, prompt_tokens, completion_tokens, month_spend_usd, monthly_cap_usd, budget_warning, budget_exceeded` | Update the header spend |
| `error` | `message, message_id?` | Show in place of, or under, the answer |
| `done` | `message_id, paused?` | Stream finished |

Related endpoints:

- `POST /messages/{id}/regenerate` with `{model?: slug}`. Re-runs the latest answer, optionally with a chosen model. Returns the same SSE stream.
- `POST /messages/{id}/feedback` with `{rating: 1|-1, comment?}`. Returns 204.
- `POST /tool-runs/{id}/approve` and `POST /tool-runs/{id}/reject`. Both resume the paused answer and return the same SSE stream.
- `GET /models` returns the allowed models for "Re-run with…": `[{slug, name, context_length, vision, file_input, tools, price_prompt, price_completion}]`.

## Files

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/files` | multipart: `file`, optional `conversation_id` | `Attachment` (201). 413 over 25 MB; 422 unsupported type |
| GET | `/files/{id}` | none | file bytes, with the right content type (own files only) |

Pass the returned ids as `attachment_ids` when sending. A message takes at most 10 files.

## Connectors

| Method | Path | Returns |
|---|---|---|
| GET | `/connectors` | `[{id: "google", name, connected: bool, scopes: string[]}]` |
| POST | `/connectors/google/start` | `{auth_url}`; the browser navigates there and comes back to `/settings?connected=google` |
| DELETE | `/connectors/google` | 204; revokes and deletes the stored token |

## Admin (role `admin` only)

| Method | Path | Returns |
|---|---|---|
| GET | `/admin/users` | `User & {month_spend_usd}`[] |
| POST | `/admin/users` | body `{email, name?, password (min 12), role?, monthly_cap_usd?}`; returns `User` (201) |
| PATCH | `/admin/users/{id}` | body: any of `{name, role, monthly_cap_usd, is_active, password}`; returns `User` |
| GET | `/admin/usage?month=YYYY-MM` | `{month, per_user: [{user_id, email, name, cost_usd}], per_model: [{model, cost_usd, messages}], categories: [{category, count}]}` |
| GET | `/admin/routing?limit=100` | `[{id, created_at, user_email, category, confidence, chosen_model, answered_model, switched, forced, reason, overridden_to, regenerated, rating}]`, newest first |
| GET | `/admin/catalog` | `{synced_at, model_count, zdr_model_count, warnings: string[], error}` |
