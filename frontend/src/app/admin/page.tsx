import type { Metadata } from "next";
import { AdminPage } from "@/components/admin/AdminPage";

export const metadata: Metadata = { title: "Admin · Relay" };

export default function Admin() {
  return <AdminPage />;
}
