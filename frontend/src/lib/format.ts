export function formatUsd(value: string | number | null | undefined, digits = 2): string {
  const n = typeof value === "number" ? value : Number.parseFloat(value ?? "0");
  if (!Number.isFinite(n)) return "$0.00";
  // Show sub-cent amounts with enough precision to be meaningful.
  const d = n !== 0 && Math.abs(n) < 0.01 ? Math.max(digits, 4) : digits;
  return `$${n.toFixed(d)}`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return "";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/** "anthropic/claude-sonnet-4.5" -> "claude-sonnet-4.5". */
export function modelLabel(slug: string | null | undefined): string {
  if (!slug) return "unknown model";
  const last = slug.split("/").pop();
  return last || slug;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function currentMonth(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

export function categoryLabel(category: string | null | undefined): string {
  return (category ?? "general").replace(/_/g, " ");
}
