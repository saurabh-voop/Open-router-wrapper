"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  type Conversation,
  type ConversationDetail,
  type Message,
  type ModelInfo,
  type ToolRun,
} from "@/lib/api";
import { applyEvent, emptyAnswer } from "@/lib/answer";
import { modelLabel } from "@/lib/format";
import { streamSSE } from "@/lib/sse";
import { useMe } from "../AppFrame";
import { Button, ErrorText, Icon, Spinner } from "../ui";
import { AssistantMessage, type AnswerActions } from "./AssistantMessage";
import { Composer, type SendRequest } from "./Composer";
import { Sidebar } from "./Sidebar";
import { UserMessage } from "./UserMessage";

/** An answer streaming in this tab, drawn over a snapshot of the thread. */
interface LiveState {
  conversationId: string;
  base: Message[];
  /** Optimistic user message for a new send; null for regenerate / approval. */
  user: Message | null;
  /** Stored message this live answer replaces in place (regenerate / approval). */
  replacesId: string | null;
  answer: Message;
  active: boolean;
}

interface StreamPlan {
  conversationId: string;
  path: string;
  body?: unknown;
  base: Message[];
  user: Message | null;
  replacesId: string | null;
  seed: Message;
}

const POLL_MS = 3000;
const MAX_POLLS = 60;

let tempSeq = 0;
const tempId = (prefix: string) => `${prefix}-${Date.now()}-${++tempSeq}`;

function errorMessage(e: unknown, fallback = "Something went wrong."): string {
  return e instanceof Error && e.message ? e.message : fallback;
}

export function ChatApp({
  sidebarOpen,
  onSidebarOpenChange,
}: {
  sidebarOpen: boolean;
  onSidebarOpenChange: (open: boolean) => void;
}) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const selectedId = searchParams.get("c");
  const { applyUsage, refreshMe } = useMe();

  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [detailError, setDetailError] = useState<{ id: string; message: string } | null>(null);
  const [live, setLive] = useState<LiveState | null>(null);
  const [composerError, setComposerError] = useState<string | null>(null);
  const [draftConfidential, setDraftConfidential] = useState(false);
  const [pollCount, setPollCount] = useState(0);

  const detailSeq = useRef(0);
  const modelsPromise = useRef<Promise<ModelInfo[]> | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  const onAuthError = useCallback(
    (e: unknown): boolean => {
      if (e instanceof ApiError && e.status === 401) {
        router.replace("/login");
        return true;
      }
      return false;
    },
    [router],
  );

  const refreshList = useCallback(() => {
    return api
      .listConversations()
      .then((list) => {
        setConversations(list);
        setListError(null);
      })
      .catch((e: unknown) => {
        if (!onAuthError(e)) setListError(errorMessage(e, "Could not load conversations."));
      })
      .finally(() => setListLoading(false));
  }, [onAuthError]);

  const loadDetail = useCallback(
    (id: string): Promise<boolean> => {
      const seq = ++detailSeq.current;
      return api
        .getConversation(id)
        .then((d) => {
          if (seq !== detailSeq.current) return true;
          setDetail(d);
          setDetailError(null);
          return true;
        })
        .catch((e: unknown) => {
          if (seq === detailSeq.current && !onAuthError(e)) {
            setDetailError({ id, message: errorMessage(e, "Could not load this conversation.") });
          }
          return false;
        });
    },
    [onAuthError],
  );

  useEffect(() => {
    void refreshList();
  }, [refreshList]);

  useEffect(() => {
    if (selectedId) void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  const current = detail && detail.id === selectedId ? detail : null;
  const liveHere = live && live.conversationId === selectedId ? live : null;
  const streaming = !!live?.active;

  // A stored answer still marked "streaming" (e.g. after a reload): poll until it settles.
  const hasStaleStreaming = !liveHere && !!current?.messages.some((m) => m.status === "streaming");
  useEffect(() => {
    if (!hasStaleStreaming || !selectedId || pollCount >= MAX_POLLS) return;
    const t = setTimeout(() => {
      setPollCount((n) => n + 1);
      void loadDetail(selectedId);
    }, POLL_MS);
    return () => clearTimeout(t);
  }, [hasStaleStreaming, selectedId, pollCount, loadDetail]);

  const messages: Message[] = liveHere
    ? liveHere.replacesId
      ? liveHere.base.map((m) => (m.id === liveHere.replacesId ? liveHere.answer : m))
      : [...liveHere.base, ...(liveHere.user ? [liveHere.user] : []), liveHere.answer]
    : (current?.messages ?? []);
  const lastAssistantId = [...messages].reverse().find((m) => m.role === "assistant")?.id ?? null;

  // Keep the view pinned to the bottom while new content arrives, unless the user scrolled up.
  const contentKey = `${messages.length}:${liveHere?.answer.text.length ?? 0}:${liveHere?.answer.tool_runs.length ?? 0}:${liveHere?.answer.status ?? ""}`;
  useEffect(() => {
    const el = scroller.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [contentKey, selectedId]);

  // ---------- navigation ----------
  const select = useCallback(
    (id: string | null, mode: "push" | "replace" = "push") => {
      const url = id ? `/?c=${encodeURIComponent(id)}` : "/";
      if (mode === "push") window.history.pushState(null, "", url);
      else window.history.replaceState(null, "", url);
      stickToBottom.current = true;
      setComposerError(null);
      setPollCount(0);
      onSidebarOpenChange(false);
    },
    [onSidebarOpenChange],
  );

  const patchConversation = useCallback((c: Conversation) => {
    setConversations((prev) => prev.map((x) => (x.id === c.id ? { ...x, ...c } : x)));
    setDetail((prev) => (prev && prev.id === c.id ? { ...prev, ...c } : prev));
  }, []);

  // ---------- streaming ----------
  const runStream = useCallback(
    (plan: StreamPlan): Promise<boolean> =>
      new Promise((resolve) => {
        let opened = false;
        stickToBottom.current = true;
        setLive({
          conversationId: plan.conversationId,
          base: plan.base,
          user: plan.user,
          replacesId: plan.replacesId,
          answer: plan.seed,
          active: true,
        });

        const finish = async () => {
          const ok = await loadDetail(plan.conversationId);
          if (ok) setLive(null);
          else setLive((prev) => (prev ? { ...prev, active: false } : prev));
          void refreshList();
          refreshMe();
        };

        streamSSE(plan.path, {
          body: plan.body,
          onOpen: () => {
            opened = true;
            resolve(true);
          },
          onEvent: (ev) => {
            if (ev.type === "usage") applyUsage(ev);
            setLive((prev) => (prev ? { ...prev, answer: applyEvent(prev.answer, ev) } : prev));
          },
        })
          .then(finish)
          .catch((e: unknown) => {
            if (onAuthError(e)) return;
            if (!opened) {
              setLive(null);
              setComposerError(errorMessage(e));
              resolve(false);
              return;
            }
            setLive((prev) =>
              prev
                ? { ...prev, answer: { ...prev.answer, status: "error", error: `Connection lost: ${errorMessage(e)}` } }
                : prev,
            );
            void finish();
          });
      }),
    [applyUsage, loadDetail, onAuthError, refreshList, refreshMe],
  );

  async function send(req: SendRequest): Promise<boolean> {
    if (streaming) return false;
    setComposerError(null);
    let conversationId = selectedId;
    let base = current?.messages ?? [];
    if (!conversationId) {
      try {
        const c = await api.createConversation({ confidential: draftConfidential });
        conversationId = c.id;
        base = [];
        setConversations((prev) => [c, ...prev.filter((x) => x.id !== c.id)]);
        setDetail({ ...c, messages: [] });
        select(c.id, "push");
      } catch (e) {
        if (!onAuthError(e)) setComposerError(errorMessage(e, "Could not start a new chat."));
        return false;
      }
    } else if (!current) {
      setComposerError("This conversation is still loading.");
      return false;
    }

    const userMsg: Message = {
      ...emptyAnswer(tempId("user"), null),
      role: "user",
      text: req.text,
      status: "done",
      attachments: req.attachments,
    };
    return runStream({
      conversationId,
      path: `/conversations/${conversationId}/messages`,
      body: {
        text: req.text,
        attachment_ids: req.attachments.map((a) => a.id),
        web_search: req.webSearch,
        code_execution: req.codeExecution,
        connectors: req.connectors,
      },
      base,
      user: userMsg,
      replacesId: null,
      seed: emptyAnswer(tempId("answer"), userMsg.id),
    });
  }

  const actions: AnswerActions = {
    onRegenerate: (msg, model) => {
      if (!current || streaming) return;
      void runStream({
        conversationId: current.id,
        path: `/messages/${msg.id}/regenerate`,
        body: model ? { model } : {},
        base: current.messages,
        user: null,
        replacesId: msg.id,
        seed: emptyAnswer(tempId("answer"), msg.parent_id),
      });
    },
    onDecide: (msg: Message, run: ToolRun, approve: boolean) => {
      if (!current || streaming) return;
      const seed: Message = {
        ...msg,
        status: "streaming",
        tool_runs: msg.tool_runs.map((r) => (r.id === run.id ? { ...r, status: approve ? "running" : "rejected" } : r)),
      };
      void runStream({
        conversationId: current.id,
        path: `/tool-runs/${run.id}/${approve ? "approve" : "reject"}`,
        base: current.messages,
        user: null,
        replacesId: msg.id,
        seed,
      });
    },
    onLock: (model) => {
      if (!current) return;
      api
        .updateConversation(current.id, { locked_model: model })
        .then(patchConversation)
        .catch((e: unknown) => {
          if (!onAuthError(e)) setComposerError(errorMessage(e));
        });
    },
    onRate: (msg, rating) => {
      if (!current) return;
      const previous = msg.rating;
      const setRating = (r: Message["rating"]) =>
        setDetail((prev) =>
          prev ? { ...prev, messages: prev.messages.map((m) => (m.id === msg.id ? { ...m, rating: r } : m)) } : prev,
        );
      setRating(rating);
      api.feedback(msg.id, rating).catch((e: unknown) => {
        setRating(previous);
        if (!onAuthError(e)) setComposerError(errorMessage(e, "Could not save feedback."));
      });
    },
    onRefresh: () => {
      if (selectedId) {
        setPollCount(0);
        void loadDetail(selectedId);
      }
    },
    loadModels: () => {
      if (!modelsPromise.current) {
        modelsPromise.current = api.models().catch((e: unknown) => {
          modelsPromise.current = null;
          throw e;
        });
      }
      return modelsPromise.current;
    },
  };

  function unlock() {
    if (!current) return;
    api
      .updateConversation(current.id, { locked_model: null })
      .then(patchConversation)
      .catch((e: unknown) => {
        if (!onAuthError(e)) setComposerError(errorMessage(e));
      });
  }

  function setConfidential(value: boolean) {
    if (!selectedId) {
      setDraftConfidential(value);
      return;
    }
    if (!current) return;
    patchConversation({ ...current, confidential: value });
    api
      .updateConversation(current.id, { confidential: value })
      .then(patchConversation)
      .catch((e: unknown) => {
        patchConversation({ ...current, confidential: !value });
        if (!onAuthError(e)) setComposerError(errorMessage(e));
      });
  }

  async function rename(id: string, title: string) {
    try {
      patchConversation(await api.updateConversation(id, { title }));
    } catch (e) {
      if (!onAuthError(e)) setListError(errorMessage(e, "Rename failed."));
    }
  }

  async function remove(id: string) {
    try {
      await api.deleteConversation(id);
      setConversations((prev) => prev.filter((c) => c.id !== id));
      if (id === selectedId) select(null, "replace");
    } catch (e) {
      if (!onAuthError(e)) setListError(errorMessage(e, "Delete failed."));
    }
  }

  const loadingThread = !!selectedId && !current && !liveHere && detailError?.id !== selectedId;
  const confidential = selectedId ? !!current?.confidential : draftConfidential;

  return (
    <div className="relative flex h-full">
      {/* Sidebar: static on md+, drawer on small screens */}
      {sidebarOpen && (
        <button
          type="button"
          aria-label="Close sidebar"
          className="fixed inset-0 top-12 z-30 bg-black/30 md:hidden"
          onClick={() => onSidebarOpenChange(false)}
        />
      )}
      <aside
        className={`fixed top-12 bottom-0 left-0 z-40 w-72 border-r border-border bg-subtle transition-transform md:static md:z-auto md:w-64 md:translate-x-0 ${
          sidebarOpen ? "translate-x-0" : "-translate-x-full"
        }`}
        aria-label="Sidebar"
      >
        <Sidebar
          conversations={conversations}
          loading={listLoading}
          error={listError}
          selectedId={selectedId}
          onSelect={(id) => select(id)}
          onNew={() => select(null)}
          onRename={rename}
          onDelete={remove}
        />
      </aside>

      <section className="flex min-w-0 flex-1 flex-col" aria-label="Conversation">
        {current && (
          <div className="flex h-10 shrink-0 items-center gap-2 border-b border-border px-4 text-sm">
            <h1 className="min-w-0 truncate font-medium" data-testid="thread-title">
              {current.title}
            </h1>
            {current.confidential && (
              <span className="rounded bg-accent-soft px-1.5 py-0.5 text-[11px] font-medium text-accent">
                Confidential
              </span>
            )}
            {current.locked_model && (
              <span
                data-testid="lock-indicator"
                className="ml-auto inline-flex min-w-0 shrink items-center gap-1.5 rounded-md border border-border bg-surface-2 px-2 py-0.5 text-xs whitespace-nowrap"
                title={`Locked to ${current.locked_model}`}
              >
                <Icon.Lock width={11} height={11} className="shrink-0" />
                <span className="hidden sm:inline">Locked to</span>
                <span className="min-w-0 truncate font-mono">{modelLabel(current.locked_model)}</span>
                <button
                  type="button"
                  onClick={unlock}
                  className="ml-1 shrink-0 font-medium text-accent hover:underline"
                >
                  Unlock
                </button>
              </span>
            )}
          </div>
        )}

        <div
          ref={scroller}
          className="min-h-0 flex-1 overflow-y-auto"
          onScroll={(e) => {
            const el = e.currentTarget;
            stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          }}
        >
          <div className="mx-auto max-w-3xl px-4 py-4">
            {loadingThread && (
              <div className="flex items-center gap-2 text-sm text-muted">
                <Spinner /> Loading conversation…
              </div>
            )}
            {selectedId && detailError?.id === selectedId && !liveHere && (
              <div className="space-y-2">
                <ErrorText>{detailError.message}</ErrorText>
                <Button size="sm" onClick={() => void loadDetail(selectedId)}>
                  Retry
                </Button>
              </div>
            )}
            {!selectedId && !liveHere && (
              <div className="flex min-h-[40vh] flex-col items-center justify-center text-center">
                <h2 className="text-lg font-semibold">Start a conversation</h2>
                <p className="mt-1 max-w-sm text-sm text-muted">
                  Each message is routed to the model best suited for the task. You will see which model answered and
                  why.
                </p>
              </div>
            )}
            {(current || liveHere) && messages.length === 0 && (
              <p className="text-sm text-muted">No messages yet. Say hello below.</p>
            )}
            <div className="space-y-3" data-testid="thread">
              {messages.map((m) =>
                m.role === "user" ? (
                  <UserMessage key={m.id} msg={m} />
                ) : (
                  <AssistantMessage
                    key={liveHere && m === liveHere.answer ? "live" : m.id}
                    msg={m}
                    isLatest={m.id === lastAssistantId}
                    live={!!liveHere && liveHere.active && m === liveHere.answer}
                    busy={streaming}
                    lockedModel={current?.locked_model ?? null}
                    actions={actions}
                  />
                ),
              )}
            </div>
          </div>
        </div>

        <Composer
          conversationId={selectedId}
          streaming={streaming}
          confidential={confidential}
          onConfidentialChange={setConfidential}
          onSend={send}
          error={composerError}
          onDismissError={() => setComposerError(null)}
        />
      </section>
    </div>
  );
}
