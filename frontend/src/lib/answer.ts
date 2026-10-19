// Builds a live assistant Message from SSE events, so streaming answers and
// stored answers render through the same component.

import type { Message, ToolRun } from "./api";
import type { StreamEvent } from "./sse";

export function emptyAnswer(id: string, parentId: string | null): Message {
  return {
    id,
    role: "assistant",
    parent_id: parentId,
    text: "",
    status: "streaming",
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
    created_at: new Date().toISOString(),
  };
}

function upsertRun(runs: ToolRun[], id: string, patch: Partial<ToolRun> & Pick<ToolRun, "tool">): ToolRun[] {
  const idx = runs.findIndex((r) => r.id === id);
  if (idx === -1) {
    return [...runs, { id, status: "running", input: {}, output_summary: null, duration_ms: null, ...patch }];
  }
  const next = runs.slice();
  next[idx] = { ...runs[idx], ...patch };
  return next;
}

export function applyEvent(msg: Message, ev: StreamEvent): Message {
  switch (ev.type) {
    case "route":
      return {
        ...msg,
        id: ev.message_id || msg.id,
        model: ev.model,
        routing: {
          category: ev.category,
          reason: ev.reason,
          switched: ev.switched,
          forced: ev.forced,
          chosen_model: ev.model,
          answered_model: null,
          overridden_to: null,
        },
      };
    case "token":
      return { ...msg, text: msg.text + ev.text };
    case "tool_start":
      return {
        ...msg,
        tool_runs: upsertRun(msg.tool_runs, ev.tool_run_id, {
          tool: ev.tool,
          input: ev.input ?? {},
          status: "running",
        }),
      };
    case "tool_end":
      return {
        ...msg,
        tool_runs: upsertRun(msg.tool_runs, ev.tool_run_id, {
          tool: ev.tool,
          status: ev.ok ? "succeeded" : "failed",
          output_summary: ev.summary ?? null,
          duration_ms: ev.duration_ms ?? null,
        }),
      };
    case "file":
      if (msg.files.some((f) => f.attachment_id === ev.attachment_id)) return msg;
      return {
        ...msg,
        files: [...msg.files, { attachment_id: ev.attachment_id, filename: ev.filename, mime: ev.mime }],
      };
    case "approval_required":
      return {
        ...msg,
        status: "awaiting_approval",
        tool_runs: upsertRun(msg.tool_runs, ev.tool_run_id, {
          tool: ev.tool,
          input: ev.input ?? {},
          status: "pending_approval",
        }),
      };
    case "citation":
      if (msg.citations.some((c) => c.index === ev.index)) return msg;
      return { ...msg, citations: [...msg.citations, { index: ev.index, url: ev.url, title: ev.title }] };
    case "usage":
      return {
        ...msg,
        cost_usd: ev.cost_usd,
        prompt_tokens: ev.prompt_tokens,
        completion_tokens: ev.completion_tokens,
      };
    case "error":
      return { ...msg, status: "error", error: ev.message };
    case "done":
      if (msg.status === "error") return msg;
      return { ...msg, status: ev.paused ? "awaiting_approval" : "done" };
    default:
      return msg;
  }
}

/** The tool run waiting for Approve / Reject, if any. */
export function pendingApproval(msg: Message): ToolRun | null {
  if (msg.status !== "awaiting_approval") return null;
  return msg.tool_runs.find((r) => r.status === "pending_approval") ?? null;
}
