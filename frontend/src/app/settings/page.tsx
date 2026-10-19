import type { Metadata } from "next";
import { Suspense } from "react";
import { FullPageLoading } from "@/components/FullPageLoading";
import { SettingsPage } from "@/components/SettingsPage";

export const metadata: Metadata = { title: "Settings · Relay" };

export default function Settings() {
  return (
    <Suspense fallback={<FullPageLoading />}>
      <SettingsPage />
    </Suspense>
  );
}
