"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, type AdminUser, type Role } from "@/lib/api";
import { formatUsd } from "@/lib/format";
import { Button, ErrorText, inputClass, Spinner } from "../ui";
import { Card, Table, td, th } from "./shared";

export function UsersPanel() {
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .adminUsers()
      .then((u) => {
        setUsers(u);
        setError(null);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Could not load users."));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="space-y-4">
      <Card title="Users">
        {error && <ErrorText>{error}</ErrorText>}
        {!users && !error && (
          <div className="flex items-center gap-2 text-sm text-muted">
            <Spinner /> Loading…
          </div>
        )}
        {users && (
          <Table>
            <thead>
              <tr>
                <th className={th}>Email</th>
                <th className={th}>Name</th>
                <th className={th}>Role</th>
                <th className={th}>Monthly cap</th>
                <th className={th}>Month spend</th>
                <th className={th}>Active</th>
                <th className={th}>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <UserRow key={u.id} user={u} onSaved={load} />
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <CreateUser onCreated={load} />
    </div>
  );
}

function UserRow({ user, onSaved }: { user: AdminUser; onSaved: () => void }) {
  const [role, setRole] = useState<Role>(user.role);
  const [cap, setCap] = useState(user.monthly_cap_usd);
  const [active, setActive] = useState(user.is_active);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const dirty = role !== user.role || Number(cap) !== Number(user.monthly_cap_usd) || active !== user.is_active;
  const spend = Number(user.month_spend_usd);
  const capN = Number(user.monthly_cap_usd);
  const spendTone = capN > 0 && spend >= capN ? "text-danger" : capN > 0 && spend >= capN * 0.8 ? "text-warn" : "";

  async function save() {
    setBusy(true);
    setError(null);
    const body: Parameters<typeof api.adminUpdateUser>[1] = {};
    if (role !== user.role) body.role = role;
    if (Number(cap) !== Number(user.monthly_cap_usd)) body.monthly_cap_usd = cap;
    if (active !== user.is_active) body.is_active = active;
    try {
      await api.adminUpdateUser(user.id, body);
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <tr className={user.is_active ? "" : "opacity-60"}>
      <td className={td}>{user.email}</td>
      <td className={td}>{user.name || "—"}</td>
      <td className={td}>
        <select
          aria-label={`Role for ${user.email}`}
          value={role}
          onChange={(e) => setRole(e.target.value as Role)}
          className="h-7 rounded-md border border-border bg-surface px-1.5 text-sm"
        >
          <option value="member">member</option>
          <option value="admin">admin</option>
        </select>
      </td>
      <td className={td}>
        <div className="flex items-center gap-1">
          <span className="text-muted">$</span>
          <input
            aria-label={`Monthly cap for ${user.email}`}
            type="number"
            min={0}
            step="0.01"
            value={cap}
            onChange={(e) => setCap(e.target.value)}
            className="h-7 w-24 rounded-md border border-border bg-surface px-1.5 text-sm tabular-nums"
          />
        </div>
      </td>
      <td className={`${td} tabular-nums ${spendTone}`}>{formatUsd(user.month_spend_usd)}</td>
      <td className={td}>
        <input
          type="checkbox"
          aria-label={`${user.email} is active`}
          checked={active}
          onChange={(e) => setActive(e.target.checked)}
          className="h-4 w-4 accent-[var(--accent)]"
        />
      </td>
      <td className={`${td} whitespace-nowrap`}>
        {dirty && (
          <span className="inline-flex gap-1">
            <Button size="sm" variant="primary" onClick={save} disabled={busy}>
              Save
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => {
                setRole(user.role);
                setCap(user.monthly_cap_usd);
                setActive(user.is_active);
              }}
            >
              Cancel
            </Button>
          </span>
        )}
        {error && <div className="mt-1 text-xs text-danger">{error}</div>}
      </td>
    </tr>
  );
}

function CreateUser({ onCreated }: { onCreated: () => void }) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [cap, setCap] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<string | null>(null);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (password.length < 12) {
      setError("Password must be at least 12 characters.");
      return;
    }
    setBusy(true);
    setError(null);
    setCreated(null);
    try {
      const u = await api.adminCreateUser({
        email: email.trim(),
        password,
        role,
        ...(name.trim() ? { name: name.trim() } : {}),
        ...(cap.trim() ? { monthly_cap_usd: cap.trim() } : {}),
      });
      setCreated(u.email);
      setEmail("");
      setName("");
      setPassword("");
      setRole("member");
      setCap("");
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the user.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Create user">
      <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <label className="space-y-1 text-xs font-medium">
          <span>Email</span>
          <input
            className={inputClass}
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className="space-y-1 text-xs font-medium">
          <span>Name (optional)</span>
          <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="space-y-1 text-xs font-medium">
          <span>Password (min 12)</span>
          <input
            className={inputClass}
            type="password"
            autoComplete="new-password"
            required
            minLength={12}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <label className="space-y-1 text-xs font-medium">
          <span>Role</span>
          <select className={inputClass} value={role} onChange={(e) => setRole(e.target.value as Role)}>
            <option value="member">member</option>
            <option value="admin">admin</option>
          </select>
        </label>
        <label className="space-y-1 text-xs font-medium">
          <span>Monthly cap USD (optional)</span>
          <input
            className={inputClass}
            type="number"
            min={0}
            step="0.01"
            placeholder="default"
            value={cap}
            onChange={(e) => setCap(e.target.value)}
          />
        </label>
        <div className="flex items-center gap-3 sm:col-span-2 lg:col-span-5">
          <Button type="submit" variant="primary" disabled={busy}>
            {busy ? "Creating…" : "Create user"}
          </Button>
          {created && <span className="text-sm text-ok">Created {created}.</span>}
        </div>
        {error && (
          <div className="sm:col-span-2 lg:col-span-5">
            <ErrorText>{error}</ErrorText>
          </div>
        )}
      </form>
    </Card>
  );
}
