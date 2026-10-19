"use client";

import { useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from "react";
import { api, type Attachment } from "@/lib/api";
import { Icon, IconButton, Spinner } from "../ui";
import { AttachmentChip } from "./parts";

export const MAX_FILES = 10;
export const MAX_FILE_BYTES = 25 * 1024 * 1024;

export interface SendRequest {
  text: string;
  attachments: Attachment[];
  webSearch: boolean;
  codeExecution: boolean;
  connectors: boolean;
}

interface Upload {
  key: string;
  filename: string;
  size: number;
  status: "uploading" | "done" | "error";
  attachment?: Attachment;
  error?: string;
}

let uploadSeq = 0;

export function Composer({
  conversationId,
  streaming,
  confidential,
  onConfidentialChange,
  onSend,
  error,
  onDismissError,
}: {
  conversationId: string | null;
  streaming: boolean;
  confidential: boolean;
  onConfidentialChange: (value: boolean) => void;
  /** Resolves true when the message was accepted, so the input can be cleared. */
  onSend: (req: SendRequest) => Promise<boolean>;
  error: string | null;
  onDismissError: () => void;
}) {
  const [text, setText] = useState("");
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [webSearch, setWebSearch] = useState(false);
  const [connectors, setConnectors] = useState(false);
  const [code, setCode] = useState(false);
  const [sending, setSending] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const uploading = uploads.some((u) => u.status === "uploading");
  const ready = uploads.filter((u) => u.status === "done" && u.attachment);
  const canSend = text.trim().length > 0 && !streaming && !sending && !uploading;

  function addFiles(list: FileList | null) {
    if (!list) return;
    const files = Array.from(list);
    const slots = MAX_FILES - uploads.filter((u) => u.status !== "error").length;
    const next: Upload[] = [];
    files.forEach((file, i) => {
      const key = `u${++uploadSeq}`;
      if (i >= slots) {
        next.push({
          key,
          filename: file.name,
          size: file.size,
          status: "error",
          error: `A message takes at most ${MAX_FILES} files.`,
        });
      } else if (file.size > MAX_FILE_BYTES) {
        next.push({ key, filename: file.name, size: file.size, status: "error", error: "Over the 25 MB limit." });
      } else {
        next.push({ key, filename: file.name, size: file.size, status: "uploading" });
        api
          .uploadFile(file, conversationId)
          .then((attachment) =>
            setUploads((prev) => prev.map((u) => (u.key === key ? { ...u, status: "done", attachment } : u))),
          )
          .catch((e: unknown) =>
            setUploads((prev) =>
              prev.map((u) =>
                u.key === key ? { ...u, status: "error", error: e instanceof Error ? e.message : "Upload failed" } : u,
              ),
            ),
          );
      }
    });
    setUploads((prev) => [...prev, ...next]);
  }

  async function submit() {
    if (!canSend) return;
    setSending(true);
    const ok = await onSend({
      text: text.trim(),
      attachments: ready.flatMap((u) => (u.attachment ? [u.attachment] : [])),
      webSearch: webSearch && !confidential,
      codeExecution: code,
      connectors,
    });
    setSending(false);
    if (ok) {
      setText("");
      setUploads([]);
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void submit();
    }
  }

  return (
    <div className="border-t border-border bg-background px-3 pt-2 pb-3">
      <div className="mx-auto max-w-3xl">
        {error && (
          <div
            role="alert"
            className="mb-2 flex items-start gap-2 rounded-md border border-danger/30 bg-danger-soft px-3 py-1.5 text-sm text-danger"
          >
            <span className="flex-1">{error}</span>
            <button
              type="button"
              onClick={onDismissError}
              aria-label="Dismiss error"
              className="shrink-0 opacity-70 hover:opacity-100"
            >
              <Icon.Close width={14} height={14} />
            </button>
          </div>
        )}
        <div className="rounded-xl border border-border bg-surface shadow-sm focus-within:border-border-strong">
          {uploads.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-2.5 pt-2.5">
              {uploads.map((u) => (
                <span key={u.key} className="inline-flex flex-col">
                  <AttachmentChip
                    attachment={{ filename: u.filename, size_bytes: u.size }}
                    state={u.status === "done" ? undefined : u.status}
                    onRemove={() => setUploads((prev) => prev.filter((x) => x.key !== u.key))}
                  />
                  {u.error && <span className="mt-0.5 text-[11px] text-danger">{u.error}</span>}
                </span>
              ))}
            </div>
          )}
          <label htmlFor="composer-input" className="sr-only">
            Message
          </label>
          <textarea
            id="composer-input"
            data-testid="composer-input"
            rows={2}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Message Relay…  (Enter to send, Shift+Enter for a new line)"
            className="block max-h-60 min-h-[3.25rem] w-full resize-none bg-transparent px-3 pt-2.5 text-sm text-foreground placeholder:text-muted focus:outline-none"
            style={{ fieldSizing: "content" } as CSSProperties}
          />
          <div className="flex flex-wrap items-center gap-1 px-2 pb-2">
            <input
              ref={fileInput}
              type="file"
              multiple
              className="hidden"
              data-testid="file-input"
              onChange={(e) => {
                addFiles(e.target.files);
                e.target.value = "";
              }}
            />
            <IconButton label="Attach files" onClick={() => fileInput.current?.click()} disabled={streaming}>
              <Icon.Paperclip width={15} height={15} />
            </IconButton>
            <Toggle
              label="Web search"
              checked={webSearch && !confidential}
              disabled={confidential}
              title={confidential ? "Web search is off in confidential chats" : "Let the model search the web"}
              onChange={setWebSearch}
            />
            <Toggle
              label="Confidential"
              checked={confidential}
              title="Confidential chats never use web search"
              onChange={onConfidentialChange}
            />
            <Toggle
              label="Connectors"
              checked={connectors}
              title="Offer Gmail / Drive tools"
              onChange={setConnectors}
            />
            <Toggle label="Code" checked={code} title="Allow running Python in the sandbox" onChange={setCode} />
            <button
              type="button"
              onClick={() => void submit()}
              disabled={!canSend}
              aria-label="Send message"
              title={streaming ? "Wait for the current answer to finish" : "Send"}
              className="ml-auto inline-flex h-8 w-8 items-center justify-center rounded-lg bg-accent text-accent-fg transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {sending || streaming ? <Spinner className="h-4 w-4" /> : <Icon.Send width={16} height={16} />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function Toggle({
  label,
  checked,
  disabled = false,
  title,
  onChange,
}: {
  label: ReactNode;
  checked: boolean;
  disabled?: boolean;
  title?: string;
  onChange: (value: boolean) => void;
}) {
  return (
    <span title={title} className="inline-flex">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={`inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
          checked
            ? "border-accent/40 bg-accent-soft text-accent"
            : "border-border text-muted hover:bg-surface-2 hover:text-foreground"
        }`}
      >
        <span className={`h-1.5 w-1.5 rounded-full ${checked ? "bg-accent" : "bg-border-strong"}`} />
        {label}
      </button>
    </span>
  );
}
