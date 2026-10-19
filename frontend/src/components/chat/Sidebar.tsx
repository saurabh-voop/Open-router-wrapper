"use client";

import { useState, type FormEvent } from "react";
import type { Conversation } from "@/lib/api";
import { Button, Icon, IconButton, Spinner } from "../ui";

export function Sidebar({
  conversations,
  loading,
  error,
  selectedId,
  onSelect,
  onNew,
  onRename,
  onDelete,
}: {
  conversations: Conversation[];
  loading: boolean;
  error: string | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
}) {
  return (
    <div className="flex h-full flex-col">
      <div className="p-2">
        <Button variant="secondary" className="w-full justify-start" onClick={onNew}>
          <Icon.Plus width={14} height={14} />
          New chat
        </Button>
      </div>
      <nav aria-label="Conversations" className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {loading && conversations.length === 0 && (
          <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-muted">
            <Spinner className="h-3 w-3" /> Loading…
          </div>
        )}
        {error && <div className="px-2 py-1.5 text-xs text-danger">{error}</div>}
        {!loading && !error && conversations.length === 0 && (
          <div className="px-2 py-1.5 text-xs text-muted">No conversations yet.</div>
        )}
        <ul className="space-y-0.5" data-testid="conversation-list">
          {conversations.map((c) => (
            <ConversationItem
              key={c.id}
              conversation={c}
              selected={c.id === selectedId}
              onSelect={() => onSelect(c.id)}
              onRename={(t) => onRename(c.id, t)}
              onDelete={() => onDelete(c.id)}
            />
          ))}
        </ul>
      </nav>
    </div>
  );
}

function ConversationItem({
  conversation,
  selected,
  onSelect,
  onRename,
  onDelete,
}: {
  conversation: Conversation;
  selected: boolean;
  onSelect: () => void;
  onRename: (title: string) => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(conversation.title);
  const [busy, setBusy] = useState(false);

  async function save(e?: FormEvent) {
    e?.preventDefault();
    const title = draft.trim();
    if (!title || title === conversation.title) {
      setEditing(false);
      return;
    }
    setBusy(true);
    try {
      await onRename(title);
      setEditing(false);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!window.confirm(`Delete "${conversation.title}"? This also deletes its files.`)) return;
    setBusy(true);
    try {
      await onDelete();
    } finally {
      setBusy(false);
    }
  }

  if (editing) {
    return (
      <li>
        <form onSubmit={save} className="flex items-center gap-1 rounded-md bg-surface-2 px-1 py-0.5">
          <input
            autoFocus
            aria-label="Conversation title"
            value={draft}
            maxLength={300}
            disabled={busy}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={() => void save()}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                setDraft(conversation.title);
                setEditing(false);
              }
            }}
            className="h-7 min-w-0 flex-1 rounded border border-accent bg-surface px-1.5 text-sm focus:outline-none"
          />
        </form>
      </li>
    );
  }

  return (
    <li className="group relative">
      <button
        type="button"
        onClick={onSelect}
        aria-current={selected ? "page" : undefined}
        className={`flex h-8 w-full items-center gap-1.5 rounded-md px-2 pr-16 text-left text-sm ${
          selected ? "bg-surface-2 font-medium text-foreground" : "text-foreground/85 hover:bg-surface-2"
        }`}
        title={conversation.title}
      >
        {conversation.locked_model && <Icon.Lock width={11} height={11} className="shrink-0 text-muted" />}
        {conversation.confidential && (
          <span
            className="shrink-0 rounded bg-accent-soft px-1 text-[9px] font-semibold uppercase text-accent"
            title="Confidential"
          >
            C
          </span>
        )}
        <span className="truncate">{conversation.title || "Untitled"}</span>
      </button>
      <div
        className={`absolute top-0.5 right-1 flex items-center gap-0.5 ${
          selected ? "opacity-100" : "opacity-0 group-hover:opacity-100 group-focus-within:opacity-100"
        }`}
      >
        <IconButton
          label={`Rename ${conversation.title}`}
          title="Rename"
          disabled={busy}
          onClick={() => {
            setDraft(conversation.title);
            setEditing(true);
          }}
        >
          <Icon.Pencil width={13} height={13} />
        </IconButton>
        <IconButton label={`Delete ${conversation.title}`} title="Delete" disabled={busy} onClick={() => void remove()}>
          <Icon.Trash width={13} height={13} />
        </IconButton>
      </div>
    </li>
  );
}
