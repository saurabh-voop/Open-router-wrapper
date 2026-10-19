// Typed client for the Relay backend. Mirrors docs/API.md.
// Every call goes to the same origin under /api/* with the session cookie.

export type Role = "admin" | "member";

/** Money values are decimal strings, e.g. "0.0021". */
export type Money = string;

export interface User {
  id: string;
  email: string;
  name: string;
  role: Role;
  monthly_cap_usd: Money;
  is_active: boolean;
  created_at: string;
}

export interface Me {
  user: User;
  month_spend_usd: Money;
  monthly_cap_usd: Money;
  budget_warning: boolean;
  budget_exceeded: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  current_model: string | null;
  current_category: string | null;
  locked_model: string | null;
  confidential: boolean;
  created_at: string;
  updated_at: string;
}

export type MessageStatus = "streaming" | "awaiting_approval" | "done" | "error";

export interface Citation {
  index: number;
  url: string;
  title: string;
}

export interface OutputFile {
  attachment_id: string;
  filename: string;
  mime: string;
}

export interface Attachment {
  id: string;
  filename: string;
  mime: string;
  size_bytes: number;
  page_count: number | null;
  est_tokens: number | null;
}

/** Backend statuses: pending_approval | running | succeeded | failed | rejected. */
export type ToolRunStatus = "pending_approval" | "running" | "succeeded" | "failed" | "rejected";

export interface ToolRun {
  id: string;
  tool: string;
  status: ToolRunStatus | string;
  input: Record<string, unknown>;
  output_summary: string | null;
  duration_ms: number | null;
}

export interface Routing {
  category: string;
  reason: string;
  switched: boolean;
  forced: boolean;
  chosen_model: string;
  answered_model: string | null;
  overridden_to: string | null;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  parent_id: string | null;
  text: string;
  status: MessageStatus;
  error: string | null;
  citations: Citation[];
  files: OutputFile[];
  attachments: Attachment[];
  tool_runs: ToolRun[];
  model: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  cached_tokens: number | null;
  cost_usd: Money | null;
  routing: Routing | null;
  rating: -1 | 1 | null;
  created_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: Message[];
}

export interface ModelInfo {
  slug: string;
  name: string;
  context_length: number | null;
  vision: boolean;
  file_input: boolean;
  tools: boolean;
  price_prompt: Money | null;
  price_completion: Money | null;
}

export interface Connector {
  id: string;
  name: string;
  connected: boolean;
  scopes: string[];
}

export interface AdminUser extends User {
  month_spend_usd: Money;
}

export interface UsageReport {
  month: string;
  per_user: { user_id: string; email: string; name: string; cost_usd: Money }[];
  per_model: { model: string; cost_usd: Money; messages: number }[];
  categories: { category: string; count: number }[];
}

export interface RoutingRow {
  id: string;
  created_at: string;
  user_email: string;
  category: string;
  confidence: number | null;
  chosen_model: string;
  answered_model: string | null;
  switched: boolean;
  forced: boolean;
  reason: string;
  overridden_to: string | null;
  regenerated: boolean;
  rating: -1 | 1 | null;
}

export interface CatalogStatus {
  synced_at: string | null;
  model_count: number;
  zdr_model_count: number;
  warnings: string[];
  error: string | null;
}

export interface SendMessageBody {
  text: string;
  attachment_ids?: string[];
  web_search?: boolean;
  code_execution?: boolean;
  connectors?: boolean;
}

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Turns FastAPI's `{detail}` (a string, or a list of validation errors) into one message. */
export async function errorFromResponse(res: Response): Promise<ApiError> {
  let detail = `${res.status} ${res.statusText || "Request failed"}`.trim();
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object" && "detail" in body) {
      const d = (body as { detail: unknown }).detail;
      if (typeof d === "string") detail = d;
      else if (Array.isArray(d)) {
        detail = d
          .map((item: unknown) =>
            item && typeof item === "object" && "msg" in item ? String((item as { msg: unknown }).msg) : String(item),
          )
          .join("; ");
      }
    }
  } catch {
    // Non-JSON error body; keep the status text.
  }
  if (res.status === 413 && !detail.toLowerCase().includes("mb")) detail = "File is larger than 25 MB.";
  return new ApiError(res.status, detail);
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`/api${path}`, { ...init, headers, credentials: "include" });
  if (!res.ok) throw await errorFromResponse(res);
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

const json = (body: unknown): string => JSON.stringify(body);

export const api = {
  // Auth
  login: (email: string, password: string) =>
    request<User>("/auth/login", { method: "POST", body: json({ email, password }) }),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  me: () => request<Me>("/me"),

  // Conversations
  listConversations: () => request<Conversation[]>("/conversations"),
  createConversation: (body: { title?: string; confidential?: boolean } = {}) =>
    request<Conversation>("/conversations", { method: "POST", body: json(body) }),
  getConversation: (id: string) => request<ConversationDetail>(`/conversations/${id}`),
  updateConversation: (id: string, body: { title?: string; locked_model?: string | null; confidential?: boolean }) =>
    request<Conversation>(`/conversations/${id}`, { method: "PATCH", body: json(body) }),
  deleteConversation: (id: string) => request<void>(`/conversations/${id}`, { method: "DELETE" }),

  // Messages
  feedback: (messageId: string, rating: 1 | -1, comment?: string) =>
    request<void>(`/messages/${messageId}/feedback`, {
      method: "POST",
      body: json(comment ? { rating, comment } : { rating }),
    }),
  models: () => request<ModelInfo[]>("/models"),

  // Files
  uploadFile: (file: File, conversationId?: string | null, signal?: AbortSignal) => {
    const form = new FormData();
    form.append("file", file);
    if (conversationId) form.append("conversation_id", conversationId);
    return request<Attachment>("/files", { method: "POST", body: form, signal });
  },

  // Connectors
  connectors: () => request<Connector[]>("/connectors"),
  startGoogle: () => request<{ auth_url: string }>("/connectors/google/start", { method: "POST" }),
  disconnectGoogle: () => request<void>("/connectors/google", { method: "DELETE" }),

  // Admin
  adminUsers: () => request<AdminUser[]>("/admin/users"),
  adminCreateUser: (body: { email: string; name?: string; password: string; role?: Role; monthly_cap_usd?: Money }) =>
    request<User>("/admin/users", { method: "POST", body: json(body) }),
  adminUpdateUser: (
    id: string,
    body: Partial<{ name: string; role: Role; monthly_cap_usd: Money; is_active: boolean; password: string }>,
  ) => request<User>(`/admin/users/${id}`, { method: "PATCH", body: json(body) }),
  adminUsage: (month: string) => request<UsageReport>(`/admin/usage?month=${encodeURIComponent(month)}`),
  adminRouting: (limit = 100) => request<RoutingRow[]>(`/admin/routing?limit=${limit}`),
  adminCatalog: () => request<CatalogStatus>("/admin/catalog"),
};

export const fileUrl = (attachmentId: string): string => `/api/files/${attachmentId}`;
