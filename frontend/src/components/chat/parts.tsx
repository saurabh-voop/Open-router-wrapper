"use client";

import { useState } from "react";
import { fileUrl, type Attachment, type Citation, type OutputFile, type ToolRun } from "@/lib/api";
import { categoryLabel, formatBytes, formatDuration, hostOf, modelLabel } from "@/lib/format";
import { Button, Icon, Spinner } from "../ui";

export function RouteNotice({ model, category }: { model: string; category: string }) {
  return (
    <div data-testid="route-notice" className="flex items-center gap-2 py-1 text-xs text-muted" role="note">
      <span className="h-px flex-1 bg-border" />
      <span className="inline-flex items-center gap-1.5">
        <Icon.Shuffle width={12} height={12} />
        Switched to{" "}
        <span className="font-medium text-foreground" title={model}>
          {modelLabel(model)}
        </span>{" "}
        for {categoryLabel(category)}.
      </span>
      <span className="h-px flex-1 bg-border" />
    </div>
  );
}

export function ToolRunLine({ run }: { run: ToolRun }) {
  const base = "flex items-center gap-1.5 text-xs";
  if (run.status === "running") {
    return (
      <div className={`${base} text-muted`} data-testid="tool-run">
        <Spinner className="h-3 w-3" />
        Running <code className="font-mono">{run.tool}</code>…
      </div>
    );
  }
  if (run.status === "pending_approval") {
    return (
      <div className={`${base} text-warn`} data-testid="tool-run">
        <Icon.Lock width={12} height={12} />
        Waiting for approval: <code className="font-mono">{run.tool}</code>
      </div>
    );
  }
  if (run.status === "rejected") {
    return (
      <div className={`${base} text-muted`} data-testid="tool-run">
        <Icon.Close width={12} height={12} />
        Rejected <code className="font-mono">{run.tool}</code>
      </div>
    );
  }
  const ok = run.status !== "failed";
  const duration = formatDuration(run.duration_ms);
  return (
    <div
      className={`${base} ${ok ? "text-muted" : "text-danger"}`}
      data-testid="tool-run"
      title={run.output_summary ?? undefined}
    >
      {ok ? <Icon.Check width={12} height={12} className="text-ok" /> : <Icon.Alert width={12} height={12} />}
      <span>
        Ran <code className="font-mono">{run.tool}</code>
        {duration && ` · ${duration}`}
        {!ok && " · failed"}
      </span>
      {run.output_summary && <span className="truncate opacity-80">— {run.output_summary}</span>}
    </div>
  );
}

export function ApprovalCard({
  run,
  disabled,
  onDecide,
}: {
  run: ToolRun;
  disabled: boolean;
  onDecide: (approve: boolean) => void;
}) {
  const [choice, setChoice] = useState<"approve" | "reject" | null>(null);
  const decide = (approve: boolean) => {
    setChoice(approve ? "approve" : "reject");
    onDecide(approve);
  };
  return (
    <div
      data-testid="approval-card"
      className="my-2 rounded-lg border border-warn/40 bg-warn-soft p-3"
      role="group"
      aria-label={`Approval needed for ${run.tool}`}
    >
      <div className="mb-2 flex items-center gap-2 text-sm font-medium text-warn">
        <Icon.Alert width={14} height={14} />
        Approve this action?
      </div>
      <div className="mb-1 text-xs text-muted">
        Tool: <code className="font-mono text-foreground">{run.tool}</code>
      </div>
      <pre className="max-h-64 overflow-auto rounded-md border border-border bg-surface p-2 font-mono text-xs">
        {JSON.stringify(run.input, null, 2)}
      </pre>
      <div className="mt-2 flex gap-2">
        <Button variant="primary" size="sm" disabled={disabled || choice !== null} onClick={() => decide(true)}>
          {choice === "approve" ? "Approving…" : "Approve"}
        </Button>
        <Button size="sm" disabled={disabled || choice !== null} onClick={() => decide(false)}>
          {choice === "reject" ? "Rejecting…" : "Reject"}
        </Button>
      </div>
    </div>
  );
}

export function Citations({ citations }: { citations: Citation[] }) {
  if (citations.length === 0) return null;
  const sorted = [...citations].sort((a, b) => a.index - b.index);
  return (
    <div className="mt-2 border-t border-border pt-2">
      <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted">Sources</div>
      <ol className="space-y-0.5 text-xs">
        {sorted.map((c) => (
          <li key={c.index} className="flex gap-1.5">
            <span className="w-5 shrink-0 text-right font-mono text-muted">[{c.index}]</span>
            <a
              href={c.url}
              target="_blank"
              rel="noopener noreferrer"
              className="min-w-0 truncate text-accent hover:underline"
              title={c.url}
            >
              {c.title || c.url}
            </a>
            <span className="shrink-0 text-muted">{hostOf(c.url)}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

export function OutputFiles({ files }: { files: OutputFile[] }) {
  if (files.length === 0) return null;
  const images = files.filter((f) => f.mime.startsWith("image/"));
  const others = files.filter((f) => !f.mime.startsWith("image/"));
  return (
    <div className="mt-2 space-y-2">
      {images.map((f) => (
        <a key={f.attachment_id} href={fileUrl(f.attachment_id)} target="_blank" rel="noopener noreferrer">
          {/* eslint-disable-next-line @next/next/no-img-element -- authenticated same-origin file, not optimizable */}
          <img
            src={fileUrl(f.attachment_id)}
            alt={f.filename}
            className="max-h-96 max-w-full rounded-md border border-border bg-white"
            loading="lazy"
          />
        </a>
      ))}
      {others.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {others.map((f) => (
            <a
              key={f.attachment_id}
              href={fileUrl(f.attachment_id)}
              download={f.filename}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs hover:bg-surface-2"
            >
              <Icon.Download width={12} height={12} />
              {f.filename}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

export function AttachmentChip({
  attachment,
  onRemove,
  state,
}: {
  attachment: Pick<Attachment, "filename" | "size_bytes">;
  onRemove?: () => void;
  state?: "uploading" | "error";
}) {
  return (
    <span
      data-testid="attachment-chip"
      className={`inline-flex max-w-full items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs ${
        state === "error" ? "border-danger/40 bg-danger-soft text-danger" : "border-border bg-surface-2"
      }`}
    >
      {state === "uploading" ? <Spinner className="h-3 w-3" /> : <Icon.File width={12} height={12} />}
      <span className="max-w-[16rem] truncate" title={attachment.filename}>
        {attachment.filename}
      </span>
      <span className="text-muted">{formatBytes(attachment.size_bytes)}</span>
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${attachment.filename}`}
          className="-mr-1 rounded p-0.5 text-muted hover:bg-surface hover:text-foreground"
        >
          <Icon.Close width={11} height={11} />
        </button>
      )}
    </span>
  );
}
