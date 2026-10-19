"use client";

import { useState } from "react";
import { AppFrame } from "../AppFrame";
import { CatalogPanel } from "./CatalogPanel";
import { RoutingPanel } from "./RoutingPanel";
import { UsagePanel } from "./UsagePanel";
import { UsersPanel } from "./UsersPanel";

const TABS = [
  { id: "users", label: "Users" },
  { id: "usage", label: "Usage" },
  { id: "routing", label: "Routing" },
  { id: "catalog", label: "Catalog" },
] as const;
type TabId = (typeof TABS)[number]["id"];

export function AdminPage() {
  const [tab, setTab] = useState<TabId>("users");
  return (
    <AppFrame requireAdmin>
      <div className="h-full overflow-y-auto">
        <div className="mx-auto max-w-6xl px-4 py-5">
          <h1 className="mb-3 text-lg font-semibold">Admin</h1>
          <div role="tablist" aria-label="Admin sections" className="mb-4 flex gap-1 border-b border-border">
            {TABS.map((t) => (
              <button
                key={t.id}
                role="tab"
                type="button"
                id={`tab-${t.id}`}
                aria-selected={tab === t.id}
                aria-controls={`panel-${t.id}`}
                onClick={() => setTab(t.id)}
                className={`-mb-px border-b-2 px-3 py-1.5 text-sm ${
                  tab === t.id
                    ? "border-accent font-medium text-foreground"
                    : "border-transparent text-muted hover:text-foreground"
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
          <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
            {tab === "users" && <UsersPanel />}
            {tab === "usage" && <UsagePanel />}
            {tab === "routing" && <RoutingPanel />}
            {tab === "catalog" && <CatalogPanel />}
          </div>
        </div>
      </div>
    </AppFrame>
  );
}
