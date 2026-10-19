"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, ApiError, type Me } from "@/lib/api";
import { formatUsd } from "@/lib/format";
import type { UsageEvent } from "@/lib/sse";
import { Button, ErrorText, Spinner } from "./ui";

interface MeContextValue {
  me: Me;
  refreshMe: () => void;
  applyUsage: (usage: UsageEvent) => void;
}

const MeContext = createContext<MeContextValue | null>(null);

export function useMe(): MeContextValue {
  const ctx = useContext(MeContext);
  if (!ctx) throw new Error("useMe must be used inside <AppFrame>");
  return ctx;
}

export function AppFrame({
  children,
  requireAdmin = false,
  headerStart,
}: {
  children: ReactNode;
  requireAdmin?: boolean;
  headerStart?: ReactNode;
}) {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .me()
      .then((m) => {
        setMe(m);
        setError(null);
      })
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 401) router.replace("/login");
        else setError(e instanceof Error ? e.message : "Could not reach the server.");
      });
  }, [router]);

  useEffect(() => {
    load();
  }, [load]);

  const applyUsage = useCallback((u: UsageEvent) => {
    setMe((prev) => {
      if (!prev) return prev;
      const spend = Number.parseFloat(u.month_spend_usd);
      const cap = Number.parseFloat(u.monthly_cap_usd);
      return {
        ...prev,
        month_spend_usd: u.month_spend_usd,
        monthly_cap_usd: u.monthly_cap_usd,
        budget_warning: u.budget_warning,
        budget_exceeded:
          u.budget_exceeded ??
          (Number.isFinite(spend) && Number.isFinite(cap) && cap > 0 ? spend >= cap : prev.budget_exceeded),
      };
    });
  }, []);

  const value = useMemo(() => (me ? { me, refreshMe: load, applyUsage } : null), [me, load, applyUsage]);

  if (!value) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        {error ? (
          <div className="max-w-sm space-y-3">
            <ErrorText>{error}</ErrorText>
            <Button onClick={load}>Retry</Button>
          </div>
        ) : (
          <span className="flex items-center gap-2 text-muted">
            <Spinner /> Loading…
          </span>
        )}
      </div>
    );
  }

  const forbidden = requireAdmin && value.me.user.role !== "admin";

  return (
    <MeContext.Provider value={value}>
      <div className="flex h-dvh flex-col">
        <Header me={value.me} start={headerStart} />
        <div className="min-h-0 flex-1">
          {forbidden ? (
            <div className="mx-auto max-w-md p-6">
              <ErrorText>This page is only available to admins.</ErrorText>
            </div>
          ) : (
            children
          )}
        </div>
      </div>
    </MeContext.Provider>
  );
}

function Header({ me, start }: { me: Me; start?: ReactNode }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  const spendTone = me.budget_exceeded
    ? "border-danger/40 bg-danger-soft text-danger"
    : me.budget_warning
      ? "border-warn/40 bg-warn-soft text-warn"
      : "border-border bg-surface text-muted";
  const spendTitle = me.budget_exceeded
    ? "Monthly budget reached. Ask an admin to raise your cap."
    : me.budget_warning
      ? "You have used over 80% of your monthly budget."
      : "Month-to-date spend vs. monthly cap";

  async function logout() {
    setBusy(true);
    try {
      await api.logout();
    } catch {
      // Logging out should always land on the login page.
    }
    router.replace("/login");
  }

  return (
    <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border bg-subtle px-3">
      {start}
      <Link href="/" className="text-[15px] font-semibold tracking-tight">
        Relay
      </Link>
      <div className="ml-auto flex items-center gap-1 sm:gap-2">
        <span
          data-testid="spend"
          title={spendTitle}
          className={`rounded-md border px-2 py-0.5 font-mono text-xs tabular-nums ${spendTone}`}
        >
          {formatUsd(me.month_spend_usd)} / {formatUsd(me.monthly_cap_usd)}
        </span>
        <nav className="flex items-center gap-0.5 text-sm">
          <Link href="/settings" className="rounded-md px-2 py-1 text-muted hover:bg-surface-2 hover:text-foreground">
            Settings
          </Link>
          {me.user.role === "admin" && (
            <Link href="/admin" className="rounded-md px-2 py-1 text-muted hover:bg-surface-2 hover:text-foreground">
              Admin
            </Link>
          )}
        </nav>
        <Button variant="ghost" size="sm" onClick={logout} disabled={busy} title={`Signed in as ${me.user.email}`}>
          Log out
        </Button>
      </div>
    </header>
  );
}
