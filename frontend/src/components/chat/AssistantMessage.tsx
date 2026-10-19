"use client";

import { useEffect, useRef, useState } from "react";
import type { Message, ModelInfo, ToolRun } from "@/lib/api";
import { pendingApproval } from "@/lib/answer";
import { categoryLabel, formatUsd, modelLabel } from "@/lib/format";
import { Button, Icon, IconButton, Spinner } from "../ui";
import { Markdown } from "./Markdown";
import { ApprovalCard, Citations, OutputFiles, RouteNotice, ToolRunLine } from "./parts";

export interface AnswerActions {
  onRegenerate: (msg: Message, model?: string) => void;
  onLock: (model: string) => void;
  onRate: (msg: Message, rating: 1 | -1) => void;
  onDecide: (msg: Message, run: ToolRun, approve: boolean) => void;
  onRefresh: () => void;
  loadModels: () => Promise<ModelInfo[]>;
}

export function answerModel(msg: Message): string | null {
  return msg.model ?? msg.routing?.answered_model ?? msg.routing?.overridden_to ?? msg.routing?.chosen_model ?? null;
}

export function AssistantMessage({
  msg,
  isLatest,
  live,
  busy,
  lockedModel,
  actions,
}: {
  msg: Message;
  isLatest: boolean;
  /** This answer is the one currently streaming in this tab. */
  live: boolean;
  /** Some stream is running; actions that start a new one are disabled. */
  busy: boolean;
  lockedModel: string | null;
  actions: AnswerActions;
}) {
  const model = answerModel(msg);
  const approval = pendingApproval(msg);
  const visibleRuns = msg.tool_runs.filter((r) => r.status !== "pending_approval" || !approval || r.id !== approval.id);
  const streamingElsewhere = msg.status === "streaming" && !live;
  const showCaret = live && msg.status === "streaming";

  return (
    <div data-testid="assistant-message" data-message-id={msg.id} className="group/answer">
      {msg.routing?.switched && model && <RouteNotice model={model} category={msg.routing.category} />}
      <div className="py-2">
        <div className="mb-1.5 flex min-h-7 items-center gap-1">
          {model ? (
            <ModelBadge
              model={model}
              category={msg.routing?.category ?? null}
              reason={msg.routing?.reason ?? null}
              forced={!!msg.routing?.forced}
              overridden={!!msg.routing?.overridden_to}
            />
          ) : (
            <span className="inline-flex items-center gap-1.5 text-xs text-muted">
              <Spinner className="h-3 w-3" /> Routing…
            </span>
          )}
          {(msg.status === "done" || (msg.status === "error" && !live)) && (
            <div className="ml-1 flex items-center gap-0.5 opacity-70 transition-opacity group-hover/answer:opacity-100 focus-within:opacity-100">
              {msg.status === "done" && (
                <>
                  <IconButton
                    label="Good answer"
                    aria-pressed={msg.rating === 1}
                    className={msg.rating === 1 ? "text-ok" : ""}
                    onClick={() => actions.onRate(msg, 1)}
                  >
                    <Icon.ThumbUp width={14} height={14} />
                  </IconButton>
                  <IconButton
                    label="Bad answer"
                    aria-pressed={msg.rating === -1}
                    className={msg.rating === -1 ? "text-danger" : ""}
                    onClick={() => actions.onRate(msg, -1)}
                  >
                    <Icon.ThumbDown width={14} height={14} />
                  </IconButton>
                </>
              )}
              <AnswerMenu
                msg={msg}
                model={model}
                isLatest={isLatest}
                busy={busy}
                lockedModel={lockedModel}
                actions={actions}
              />
            </div>
          )}
          {msg.cost_usd && msg.status === "done" && (
            <span className="ml-auto text-[11px] tabular-nums text-muted" title="Cost of this answer">
              {formatUsd(msg.cost_usd, 4)}
            </span>
          )}
        </div>

        {visibleRuns.length > 0 && (
          <div className="mb-2 space-y-0.5 border-l-2 border-border pl-2">
            {visibleRuns.map((r) => (
              <ToolRunLine key={r.id} run={r} />
            ))}
          </div>
        )}

        {msg.text ? (
          <div className={showCaret ? "caret" : undefined} data-testid="answer-text">
            <Markdown text={msg.text} />
          </div>
        ) : (
          showCaret && visibleRuns.length === 0 && <div className="caret text-muted" aria-label="Generating" />
        )}

        <OutputFiles files={msg.files} />
        <Citations citations={msg.citations} />

        {approval && (
          <ApprovalCard
            key={approval.id}
            run={approval}
            disabled={busy && !live}
            onDecide={(approve) => actions.onDecide(msg, approval, approve)}
          />
        )}

        {msg.status === "error" && (
          <div
            role="alert"
            className="mt-2 flex items-start gap-2 rounded-md border border-danger/30 bg-danger-soft px-3 py-2 text-sm text-danger"
          >
            <Icon.Alert width={14} height={14} className="mt-0.5 shrink-0" />
            <span>{msg.error || "Something went wrong while generating this answer."}</span>
          </div>
        )}

        {streamingElsewhere && (
          <div className="mt-2 flex items-center gap-2 rounded-md border border-border bg-surface-2 px-3 py-2 text-xs text-muted">
            <Spinner className="h-3 w-3" />
            <span>This answer is still generating, or was interrupted.</span>
            <Button size="sm" variant="ghost" className="ml-auto" onClick={actions.onRefresh}>
              Refresh
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

function ModelBadge({
  model,
  category,
  reason,
  forced,
  overridden,
}: {
  model: string;
  category: string | null;
  reason: string | null;
  forced: boolean;
  overridden: boolean;
}) {
  return (
    <span className="group/badge relative inline-flex" data-testid="model-badge">
      <span
        tabIndex={0}
        title={model}
        className="inline-flex h-6 cursor-default items-center gap-1 rounded-md border border-border bg-surface-2 px-1.5 font-mono text-[11px] text-foreground"
      >
        {modelLabel(model)}
        {category && <span className="font-sans text-muted">· {categoryLabel(category)}</span>}
      </span>
      <span
        role="tooltip"
        className="pointer-events-none invisible absolute top-full left-0 z-20 mt-1 w-72 rounded-md border border-border bg-surface p-2.5 text-xs opacity-0 shadow-lg transition-opacity group-focus-within/badge:visible group-focus-within/badge:opacity-100 group-hover/badge:visible group-hover/badge:opacity-100"
      >
        <span className="block font-mono text-[11px] break-all">{model}</span>
        {category && (
          <span className="mt-1.5 block">
            <span className="text-muted">Category:</span> {categoryLabel(category)}
            {forced && <span className="text-muted"> (forced switch)</span>}
            {overridden && <span className="text-muted"> (chosen by you)</span>}
          </span>
        )}
        {reason && (
          <span className="mt-1 block">
            <span className="text-muted">Why:</span> {reason}
          </span>
        )}
      </span>
    </span>
  );
}

function AnswerMenu({
  msg,
  model,
  isLatest,
  busy,
  lockedModel,
  actions,
}: {
  msg: Message;
  model: string | null;
  isLatest: boolean;
  busy: boolean;
  lockedModel: string | null;
  actions: AnswerActions;
}) {
  const [open, setOpen] = useState(false);
  const [models, setModels] = useState<ModelInfo[] | null>(null);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && isLatest && models === null) {
      actions
        .loadModels()
        .then(setModels)
        .catch((e: unknown) => setModelsError(e instanceof Error ? e.message : "Could not load models"));
    }
  }

  const isLocked = !!model && lockedModel === model;
  const itemClass =
    "flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-50";

  return (
    <div className="relative" ref={ref}>
      <IconButton label="Answer options" aria-haspopup="menu" aria-expanded={open} onClick={toggle}>
        <Icon.More width={14} height={14} />
      </IconButton>
      {open && (
        <div
          role="menu"
          className="absolute top-full left-0 z-30 mt-1 w-64 rounded-md border border-border bg-surface p-1 shadow-lg"
        >
          {isLatest && (
            <>
              <div className="px-2 pt-1 pb-0.5 text-[11px] font-medium uppercase tracking-wide text-muted">
                Re-run with…
              </div>
              <button
                role="menuitem"
                type="button"
                className={itemClass}
                disabled={busy}
                onClick={() => {
                  setOpen(false);
                  actions.onRegenerate(msg);
                }}
              >
                Same routing (regenerate)
              </button>
              {models === null && !modelsError && (
                <div className="flex items-center gap-1.5 px-2 py-1.5 text-xs text-muted">
                  <Spinner className="h-3 w-3" /> Loading models…
                </div>
              )}
              {modelsError && <div className="px-2 py-1.5 text-xs text-danger">{modelsError}</div>}
              <div className="max-h-56 overflow-y-auto">
                {models?.map((m) => (
                  <button
                    key={m.slug}
                    role="menuitem"
                    type="button"
                    className={itemClass}
                    disabled={busy}
                    title={m.slug}
                    onClick={() => {
                      setOpen(false);
                      actions.onRegenerate(msg, m.slug);
                    }}
                  >
                    <span className="min-w-0 flex-1 truncate">{m.name || modelLabel(m.slug)}</span>
                    {m.slug === model && <span className="text-[10px] text-muted">current</span>}
                  </button>
                ))}
              </div>
              <div className="my-1 h-px bg-border" />
            </>
          )}
          <button
            role="menuitem"
            type="button"
            className={itemClass}
            disabled={!model || isLocked}
            onClick={() => {
              setOpen(false);
              if (model) actions.onLock(model);
            }}
          >
            <Icon.Lock width={12} height={12} />
            {isLocked ? "Chat is locked to this model" : "Lock this chat to this model"}
          </button>
        </div>
      )}
    </div>
  );
}
