import type { Metadata } from "next";
import { LoginForm } from "@/components/LoginForm";

export const metadata: Metadata = { title: "Sign in · Relay" };

export default function LoginPage() {
  return (
    <main className="flex min-h-dvh items-center justify-center bg-subtle p-4">
      <LoginForm />
    </main>
  );
}
