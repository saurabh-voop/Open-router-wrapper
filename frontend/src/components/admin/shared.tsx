import type { ReactNode } from "react";

export const th =
  "border-b border-border px-2.5 py-1.5 text-left text-[11px] font-medium uppercase tracking-wide text-muted whitespace-nowrap";
export const td = "border-b border-border px-2.5 py-1.5 align-middle";

export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-border bg-surface">
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  );
}

export function Card({ title, children, actions }: { title: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-surface p-4">
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-sm font-semibold">{title}</h2>
        {actions && <div className="ml-auto">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

/** Horizontal bars, pure CSS. Values are plain numbers; labels are formatted by the caller. */
export function Bars({ rows }: { rows: { key: string; label: string; value: number; display: string }[] }) {
  const max = Math.max(0, ...rows.map((r) => r.value));
  if (rows.length === 0) return <p className="text-sm text-muted">No data for this month.</p>;
  return (
    <ul className="space-y-1.5">
      {rows.map((r) => (
        <li key={r.key} className="grid grid-cols-[minmax(6rem,12rem)_1fr_auto] items-center gap-2 text-xs">
          <span className="truncate" title={r.label}>
            {r.label}
          </span>
          <span className="h-2.5 overflow-hidden rounded-sm bg-surface-2" aria-hidden="true">
            <span
              className="block h-full rounded-sm bg-accent"
              style={{ width: max > 0 ? `${(r.value / max) * 100}%` : "0%" }}
            />
          </span>
          <span className="tabular-nums text-muted">{r.display}</span>
        </li>
      ))}
    </ul>
  );
}
