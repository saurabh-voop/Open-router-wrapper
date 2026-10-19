import { Spinner } from "./ui";

export function FullPageLoading() {
  return (
    <div className="flex h-dvh items-center justify-center text-muted">
      <span className="flex items-center gap-2">
        <Spinner /> Loading…
      </span>
    </div>
  );
}
