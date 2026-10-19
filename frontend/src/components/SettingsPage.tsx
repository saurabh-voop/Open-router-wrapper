"use client";

import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Connector } from "@/lib/api";
import { AppFrame, useMe } from "./AppFrame";
import { Button, ErrorText, Spinner } from "./ui";

export function SettingsPage() {
  return (
    <AppFrame>
      <div className="h-full overflow-y-auto">
        <div className="mx-auto max-w-2xl space-y-6 px-4 py-6">
          <h1 className="text-lg font-semibold">Settings</h1>
          <Account />
          <Connectors />
        </div>
      </div>
    </AppFrame>
  );
}

function Account() {
  const { me } = useMe();
  return (
    <section className="rounded-lg border border-border bg-surface p-4">
      <h2 className="mb-2 text-sm font-semibold">Account</h2>
      <dl className="grid grid-cols-[8rem_1fr] gap-y-1 text-sm">
        <dt className="text-muted">Name</dt>
        <dd>{me.user.name || "—"}</dd>
        <dt className="text-muted">Email</dt>
        <dd>{me.user.email}</dd>
        <dt className="text-muted">Role</dt>
        <dd className="capitalize">{me.user.role}</dd>
      </dl>
    </section>
  );
}

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; connectors: Connector[] }
  | { kind: "unavailable" }
  | { kind: "error"; message: string };

const GOOGLE_FALLBACK: Connector = { id: "google", name: "Google (Gmail + Drive)", connected: false, scopes: [] };

function Connectors() {
  const params = useSearchParams();
  const justConnected = params.get("connected");
  const callbackError = params.get("error");
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState(false);

  const load = useCallback(() => {
    api
      .connectors()
      .then((connectors) => setState({ kind: "ready", connectors }))
      .catch((e: unknown) => {
        if (e instanceof ApiError && (e.status === 404 || e.status === 501)) setState({ kind: "unavailable" });
        else setState({ kind: "error", message: e instanceof Error ? e.message : "Could not load connectors." });
      });
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function connect() {
    setBusy(true);
    setActionError(null);
    try {
      const { auth_url } = await api.startGoogle();
      window.location.assign(auth_url);
    } catch (e) {
      setActionError(
        e instanceof ApiError && (e.status === 404 || e.status === 501)
          ? "Google connectors are not set up on this server yet."
          : e instanceof Error
            ? e.message
            : "Could not start the Google sign-in.",
      );
      setBusy(false);
    }
  }

  async function disconnect() {
    if (!window.confirm("Disconnect Google? Relay will lose access to Gmail and Drive.")) return;
    setBusy(true);
    setActionError(null);
    try {
      await api.disconnectGoogle();
      load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Could not disconnect.");
    } finally {
      setBusy(false);
    }
  }

  const connectors =
    state.kind === "ready"
      ? state.connectors.some((c) => c.id === "google")
        ? state.connectors
        : [GOOGLE_FALLBACK, ...state.connectors]
      : [];

  return (
    <section className="rounded-lg border border-border bg-surface p-4">
      <h2 className="text-sm font-semibold">Connectors</h2>
      <p className="mt-0.5 mb-3 text-xs text-muted">
        Let Relay read your mail and files when you ask it to. Actions that write (sending mail, creating files) always
        ask for your approval first.
      </p>

      {justConnected === "google" && !dismissed && (
        <div
          role="status"
          className="mb-3 flex items-center gap-2 rounded-md border border-ok/30 bg-ok-soft px-3 py-2 text-sm text-ok"
        >
          <span className="flex-1">Google account connected.</span>
          <button type="button" className="text-xs underline" onClick={() => setDismissed(true)}>
            Dismiss
          </button>
        </div>
      )}
      {callbackError && !dismissed && (
        <div className="mb-3">
          <ErrorText>Google connection failed: {callbackError}</ErrorText>
        </div>
      )}
      {actionError && (
        <div className="mb-3">
          <ErrorText>{actionError}</ErrorText>
        </div>
      )}

      {state.kind === "loading" && (
        <div className="flex items-center gap-2 text-sm text-muted">
          <Spinner /> Loading…
        </div>
      )}
      {state.kind === "unavailable" && (
        <p className="text-sm text-muted">Connectors are not available on this server yet.</p>
      )}
      {state.kind === "error" && <ErrorText>{state.message}</ErrorText>}

      {connectors.length > 0 && (
        <ul className="divide-y divide-border rounded-md border border-border">
          {connectors.map((c) => (
            <li key={c.id} className="flex flex-wrap items-center gap-3 px-3 py-2.5">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 text-sm font-medium">
                  {c.name}
                  <span
                    className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${
                      c.connected ? "bg-ok-soft text-ok" : "bg-surface-2 text-muted"
                    }`}
                  >
                    {c.connected ? "Connected" : "Not connected"}
                  </span>
                </div>
                {c.connected && c.scopes.length > 0 && (
                  <div className="mt-0.5 truncate text-xs text-muted" title={c.scopes.join("\n")}>
                    {c.scopes.length} scope{c.scopes.length === 1 ? "" : "s"} granted
                  </div>
                )}
              </div>
              {c.id === "google" &&
                (c.connected ? (
                  <Button size="sm" onClick={disconnect} disabled={busy}>
                    Disconnect
                  </Button>
                ) : (
                  <Button size="sm" variant="primary" onClick={connect} disabled={busy}>
                    {busy ? "Redirecting…" : "Connect"}
                  </Button>
                ))}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
