// POST-based Server-Sent Events reader. EventSource cannot POST, so we read
// the fetch() body stream and split it into `data: <json>\n\n` frames.

import { errorFromResponse } from "./api";

export interface RouteEvent {
  type: "route";
  message_id: string;
  model: string;
  category: string;
  reason: string;
  switched: boolean;
  forced: boolean;
  confidential: boolean;
}
export interface TokenEvent {
  type: "token";
  text: string;
}
export interface ToolStartEvent {
  type: "tool_start";
  tool_run_id: string;
  tool: string;
  input: Record<string, unknown>;
}
export interface ToolEndEvent {
  type: "tool_end";
  tool_run_id: string;
  tool: string;
  ok: boolean;
  summary: string | null;
  duration_ms: number | null;
}
export interface FileEvent {
  type: "file";
  attachment_id: string;
  filename: string;
  mime: string;
}
export interface ApprovalRequiredEvent {
  type: "approval_required";
  tool_run_id: string;
  tool: string;
  input: Record<string, unknown>;
}
export interface CitationEvent {
  type: "citation";
  index: number;
  url: string;
  title: string;
}
export interface UsageEvent {
  type: "usage";
  cost_usd: string;
  prompt_tokens: number;
  completion_tokens: number;
  month_spend_usd: string;
  monthly_cap_usd: string;
  budget_warning: boolean;
  budget_exceeded?: boolean;
}
export interface ErrorEvent {
  type: "error";
  message: string;
  message_id?: string;
}
export interface DoneEvent {
  type: "done";
  message_id: string;
  paused?: boolean;
}

export type StreamEvent =
  | RouteEvent
  | TokenEvent
  | ToolStartEvent
  | ToolEndEvent
  | FileEvent
  | ApprovalRequiredEvent
  | CitationEvent
  | UsageEvent
  | ErrorEvent
  | DoneEvent;

/**
 * Incremental frame parser. Feed it decoded text chunks in any split; it
 * returns the JSON payloads of every complete event seen so far.
 */
export class SSEParser {
  private buffer = "";

  push(chunk: string): unknown[] {
    this.buffer += chunk.replace(/\r\n?/g, "\n");
    const out: unknown[] = [];
    let sep: number;
    while ((sep = this.buffer.indexOf("\n\n")) !== -1) {
      const frame = this.buffer.slice(0, sep);
      this.buffer = this.buffer.slice(sep + 2);
      const parsed = parseFrame(frame);
      if (parsed !== undefined) out.push(parsed);
    }
    return out;
  }

  /** Flush a trailing frame that was not followed by a blank line. */
  end(): unknown[] {
    const rest = this.buffer;
    this.buffer = "";
    const parsed = rest.trim() ? parseFrame(rest) : undefined;
    return parsed === undefined ? [] : [parsed];
  }
}

function parseFrame(frame: string): unknown {
  const data = frame
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""))
    .join("\n");
  if (!data) return undefined; // comment / keep-alive frame
  try {
    return JSON.parse(data);
  } catch {
    return undefined;
  }
}

function isStreamEvent(value: unknown): value is StreamEvent {
  return typeof value === "object" && value !== null && typeof (value as { type?: unknown }).type === "string";
}

export interface StreamOptions {
  body?: unknown;
  signal?: AbortSignal;
  /** Called once the server accepted the request and the stream is open. */
  onOpen?: () => void;
  onEvent: (event: StreamEvent) => void;
}

/**
 * POSTs to `/api{path}` and calls `onEvent` for each SSE event.
 * Throws ApiError for non-2xx responses (e.g. 409 while another answer runs).
 */
export async function streamSSE(path: string, { body, signal, onOpen, onEvent }: StreamOptions): Promise<void> {
  const res = await fetch(`/api${path}`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  if (!res.ok) throw await errorFromResponse(res);
  onOpen?.();
  if (!res.body) return;

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SSEParser();
  const emit = (items: unknown[]) => {
    for (const item of items) if (isStreamEvent(item)) onEvent(item);
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    emit(parser.push(decoder.decode(value, { stream: true })));
  }
  emit(parser.push(decoder.decode()));
  emit(parser.end());
}
