"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type CatalogStatus } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { Button, ErrorText, Icon, Spinner } from "../ui";
import { Card } from "./shared";

export function CatalogPanel() {
  const [status, setStatus] = useState<CatalogStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .adminCatalog()
      .then((s) => {
        setStatus(s);
        setError(null);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Could not load the catalog status."));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (error) return <ErrorText>{error}</ErrorText>;
  if (!status)
    return (
      <div className="flex items-center gap-2 text-sm text-muted">
        <Spinner /> Loading…
      </div>
    );

  return (
    <div className="space-y-4">
      {status.warnings.length > 0 && (
        <div role="alert" className="rounded-lg border border-warn/40 bg-warn-soft p-4 text-warn">
          <div className="mb-1.5 flex items-center gap-2 text-sm font-semibold">
            <Icon.Alert width={15} height={15} />
            {status.warnings.length} catalog warning{status.warnings.length === 1 ? "" : "s"}
          </div>
          <ul className="list-disc space-y-0.5 pl-6 text-sm">
            {status.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      )}
      {status.error && <ErrorText>Last sync failed: {status.error}</ErrorText>}
      <Card
        title="Model catalog"
        actions={
          <Button size="sm" onClick={load}>
            Refresh
          </Button>
        }
      >
        <dl className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Stat label="Last sync" value={formatDateTime(status.synced_at)} />
          <Stat label="Models" value={String(status.model_count)} />
          <Stat label="With ZDR endpoint" value={String(status.zdr_model_count)} />
        </dl>
        {status.warnings.length === 0 && !status.error && (
          <p className="mt-3 flex items-center gap-1.5 text-sm text-ok">
            <Icon.Check width={14} height={14} /> Every model in routing.yaml is available with a ZDR endpoint.
          </p>
        )}
      </Card>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border bg-subtle px-3 py-2">
      <dt className="text-[11px] font-medium uppercase tracking-wide text-muted">{label}</dt>
      <dd className="mt-0.5 text-base font-semibold tabular-nums">{value}</dd>
    </div>
  );
}
