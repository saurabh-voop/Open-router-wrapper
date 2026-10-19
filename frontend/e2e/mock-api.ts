import type { Page, Route, Request } from "@playwright/test";

// Minimal fixtures shaped exactly like docs/API.md.

export const user = {
  id: "u1",
  email: "sam@example.com",
  name: "Sam",
  role: "member" as const,
  monthly_cap_usd: "50.00",
  is_active: true,
  created_at: "2026-10-01T09:00:00Z",
};

export const me = {
  user,
  month_spend_usd: "3.21",
  monthly_cap_usd: "50.00",
  budget_warning: false,
  budget_exceeded: false,
};

export function conversation(id: string, title = "New chat", extra: Record<string, unknown> = {}) {
  return {
    id,
    title,
    current_model: null,
    current_category: null,
    locked_model: null,
    confidential: false,
    created_at: "2026-10-19T10:00:00Z",
    updated_at: "2026-10-19T10:00:00Z",
    ...extra,
  };
}

export function message(partial: Record<string, unknown> & { id: string; role: "user" | "assistant" }) {
  return {
    parent_id: null,
    text: "",
    status: "done",
    error: null,
    citations: [],
    files: [],
    attachments: [],
    tool_runs: [],
    model: null,
    prompt_tokens: null,
    completion_tokens: null,
    cached_tokens: null,
    cost_usd: null,
    routing: null,
    rating: null,
    created_at: "2026-10-19T10:00:00Z",
    ...partial,
  };
}

/** Encodes events the way the backend does: `data: <json>\n\n`. */
export function sse(events: Record<string, unknown>[]): string {
  return events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
}

export type Handler = (route: Route, request: Request, url: URL) => Promise<void> | void;

export const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

export const stream = (route: Route, body: string) =>
  route.fulfill({ status: 200, headers: { "content-type": "text/event-stream", "cache-control": "no-cache" }, body });

/**
 * Routes every /api/* request through `handlers`, keyed by "METHOD /path"
 * (path without the /api prefix). Unmatched requests fail loudly with 599.
 */
export async function mockApi(page: Page, handlers: Record<string, Handler>) {
  const unmatched: string[] = [];
  await page.route("**/api/**", async (route, request) => {
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api/, "");
    const key = `${request.method()} ${path}`;
    const handler = handlers[key];
    if (!handler) {
      unmatched.push(key);
      await json(route, { detail: `unmocked ${key}` }, 599);
      return;
    }
    await handler(route, request, url);
  });
  return { unmatched };
}
