"use client";

import { useEffect, useState } from "react";
import { api, type UsageReport } from "@/lib/api";
import { categoryLabel, currentMonth, formatUsd, modelLabel } from "@/lib/format";
import { ErrorText, Spinner } from "../ui";
import { Bars, Card, Table, td, th } from "./shared";

type State = { month: string; report: UsageReport | null; error: string | null };

export function UsagePanel() {
  const [month, setMonth] = useState(currentMonth);
  const [state, setState] = useState<State | null>(null);

  useEffect(() => {
    if (!/^\d{4}-\d{2}$/.test(month)) return;
    let cancelled = false;
    api
      .adminUsage(month)
      .then((report) => !cancelled && setState({ month, report, error: null }))
      .catch(
        (e: unknown) =>
          !cancelled &&
          setState({ month, report: null, error: e instanceof Error ? e.message : "Could not load usage." }),
      );
    return () => {
      cancelled = true;
    };
  }, [month]);

  const loading = !state || state.month !== month;
  const report = !loading ? state.report : null;
  const total = report ? report.per_user.reduce((s, u) => s + Number(u.cost_usd), 0) : 0;
  const totalMessages = report ? report.categories.reduce((s, c) => s + c.count, 0) : 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted">Month</span>
          <input
            type="month"
            value={month}
            onChange={(e) => e.target.value && setMonth(e.target.value)}
            className="h-8 rounded-md border border-border bg-surface px-2 text-sm"
          />
        </label>
        {report && (
          <span className="text-sm text-muted">
            Total <span className="font-medium text-foreground tabular-nums">{formatUsd(total)}</span>
          </span>
        )}
        {loading && <Spinner />}
      </div>
      {!loading && state.error && <ErrorText>{state.error}</ErrorText>}
      {report && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card title="Spend per user">
            <Bars
              rows={[...report.per_user]
                .sort((a, b) => Number(b.cost_usd) - Number(a.cost_usd))
                .map((u) => ({
                  key: u.user_id,
                  label: u.name || u.email,
                  value: Number(u.cost_usd),
                  display: formatUsd(u.cost_usd),
                }))}
            />
            <div className="mt-3">
              <Table>
                <thead>
                  <tr>
                    <th className={th}>User</th>
                    <th className={th}>Email</th>
                    <th className={`${th} text-right`}>Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {report.per_user.map((u) => (
                    <tr key={u.user_id}>
                      <td className={td}>{u.name || "—"}</td>
                      <td className={td}>{u.email}</td>
                      <td className={`${td} text-right tabular-nums`}>{formatUsd(u.cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </div>
          </Card>
          <Card title="Spend per model">
            <Bars
              rows={[...report.per_model]
                .sort((a, b) => Number(b.cost_usd) - Number(a.cost_usd))
                .map((m) => ({
                  key: m.model,
                  label: modelLabel(m.model),
                  value: Number(m.cost_usd),
                  display: formatUsd(m.cost_usd),
                }))}
            />
            <div className="mt-3">
              <Table>
                <thead>
                  <tr>
                    <th className={th}>Model</th>
                    <th className={`${th} text-right`}>Messages</th>
                    <th className={`${th} text-right`}>Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {report.per_model.map((m) => (
                    <tr key={m.model}>
                      <td className={`${td} font-mono text-xs`} title={m.model}>
                        {m.model}
                      </td>
                      <td className={`${td} text-right tabular-nums`}>{m.messages}</td>
                      <td className={`${td} text-right tabular-nums`}>{formatUsd(m.cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </div>
          </Card>
          <Card title="Category mix">
            <Bars
              rows={[...report.categories]
                .sort((a, b) => b.count - a.count)
                .map((c) => ({
                  key: c.category,
                  label: categoryLabel(c.category),
                  value: c.count,
                  display: `${c.count}${totalMessages ? ` · ${Math.round((c.count / totalMessages) * 100)}%` : ""}`,
                }))}
            />
          </Card>
        </div>
      )}
    </div>
  );
}
