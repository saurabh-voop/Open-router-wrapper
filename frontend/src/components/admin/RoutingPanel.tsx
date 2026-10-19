"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type RoutingRow } from "@/lib/api";
import { categoryLabel, formatDateTime, modelLabel } from "@/lib/format";
import { Button, ErrorText, Spinner } from "../ui";
import { Table, td, th } from "./shared";

function Flag({ children, tone }: { children: string; tone: "accent" | "warn" | "muted" }) {
  const cls =
    tone === "accent"
      ? "bg-accent-soft text-accent"
      : tone === "warn"
        ? "bg-warn-soft text-warn"
        : "bg-surface-2 text-muted";
  return <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${cls}`}>{children}</span>;
}

export function RoutingPanel() {
  const [rows, setRows] = useState<RoutingRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .adminRouting(100)
      .then((r) => {
        setRows(r);
        setError(null);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Could not load routing decisions."));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-sm text-muted">
        <span>Latest 100 routing decisions. Rows with a manual override or re-run are highlighted.</span>
        <Button size="sm" className="ml-auto" onClick={load}>
          Refresh
        </Button>
      </div>
      {error && <ErrorText>{error}</ErrorText>}
      {!rows && !error && (
        <div className="flex items-center gap-2 text-sm text-muted">
          <Spinner /> Loading…
        </div>
      )}
      {rows && rows.length === 0 && <p className="text-sm text-muted">No routing decisions yet.</p>}
      {rows && rows.length > 0 && (
        <Table>
          <thead>
            <tr>
              <th className={th}>Time</th>
              <th className={th}>User</th>
              <th className={th}>Category</th>
              <th className={`${th} text-right`}>Conf.</th>
              <th className={th}>Chosen → answered</th>
              <th className={th}>Flags</th>
              <th className={th}>Reason</th>
              <th className={th}>Override</th>
              <th className={th}>Rating</th>
            </tr>
          </thead>
          <tbody className="text-xs">
            {rows.map((r) => {
              const highlighted = !!r.overridden_to || r.regenerated;
              return (
                <tr
                  key={r.id}
                  data-highlighted={highlighted || undefined}
                  className={highlighted ? "bg-warn-soft" : ""}
                >
                  <td className={`${td} whitespace-nowrap text-muted`}>{formatDateTime(r.created_at)}</td>
                  <td className={td}>{r.user_email}</td>
                  <td className={`${td} whitespace-nowrap`}>{categoryLabel(r.category)}</td>
                  <td className={`${td} text-right tabular-nums`}>
                    {r.confidence == null ? "—" : r.confidence.toFixed(2)}
                  </td>
                  <td className={`${td} font-mono whitespace-nowrap`}>
                    <span title={r.chosen_model}>{modelLabel(r.chosen_model)}</span>
                    {r.answered_model && r.answered_model !== r.chosen_model && (
                      <>
                        {" → "}
                        <span title={r.answered_model}>{modelLabel(r.answered_model)}</span>
                      </>
                    )}
                  </td>
                  <td className={td}>
                    <span className="flex flex-wrap gap-1">
                      {r.switched && <Flag tone="accent">switched</Flag>}
                      {r.forced && <Flag tone="muted">forced</Flag>}
                    </span>
                  </td>
                  <td className={`${td} min-w-[14rem] max-w-md`}>{r.reason}</td>
                  <td className={td}>
                    <span className="flex flex-wrap items-center gap-1">
                      {r.overridden_to && (
                        <span className="font-mono" title={r.overridden_to}>
                          {modelLabel(r.overridden_to)}
                        </span>
                      )}
                      {r.regenerated && <Flag tone="warn">re-run</Flag>}
                      {!r.overridden_to && !r.regenerated && <span className="text-muted">—</span>}
                    </span>
                  </td>
                  <td className={td}>
                    {r.rating === 1 ? (
                      <span className="text-ok" aria-label="thumbs up">
                        ▲
                      </span>
                    ) : r.rating === -1 ? (
                      <span className="text-danger" aria-label="thumbs down">
                        ▼
                      </span>
                    ) : (
                      <span className="text-muted">—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      )}
    </div>
  );
}
