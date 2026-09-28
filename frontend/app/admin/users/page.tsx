"use client";

import React, { useEffect, useState, useCallback } from "react";
import { L } from "@/lib/labels";
import {
  Role,
  User,
  getAdminUsers,
  createAdminUser,
  updateAdminUser,
  revokeUserSessions,
  getAuthErrorMessage,
} from "@/lib/api/auth";
import { useAuth } from "@/lib/auth/AuthProvider";

const ROLES: Role[] = ["admin", "researcher", "pathologist", "viewer"];

export default function AdminUsersPage() {
  const { can, loading: authLoading } = useAuth();
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  // Invite form state
  const [inviteEmail, setInviteEmail] = useState<string>("");
  const [inviteRole, setInviteRole] = useState<Role>("pathologist");
  const [inviting, setInviting] = useState<boolean>(false);

  // Row action loading map
  const [actionLoading, setActionLoading] = useState<Record<string, boolean>>({});

  const hasPermission = can("user:manage");

  const loadUsers = useCallback(async () => {
    try {
      setLoading(true);
      setErrorMsg(null);
      const data = await getAdminUsers();
      setUsers(data);
    } catch (err: any) {
      setErrorMsg(getAuthErrorMessage(err?.message));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!authLoading && hasPermission) {
      loadUsers();
    }
  }, [authLoading, hasPermission, loadUsers]);

  const handleInvite = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!inviteEmail.trim()) return;

    try {
      setInviting(true);
      setErrorMsg(null);
      setSuccessMsg(null);
      const created = await createAdminUser(inviteEmail.trim(), inviteRole);
      setUsers((prev) => [...prev, created]);
      setInviteEmail("");
      setSuccessMsg(L.status.done);
    } catch (err: any) {
      setErrorMsg(getAuthErrorMessage(err?.message));
    } finally {
      setInviting(false);
    }
  };

  const handleRoleChange = async (userId: string, newRole: Role) => {
    try {
      setActionLoading((prev) => ({ ...prev, [userId]: true }));
      setErrorMsg(null);
      setSuccessMsg(null);
      const updated = await updateAdminUser(userId, { role: newRole });
      setUsers((prev) => prev.map((u) => (u.id === userId ? updated : u)));
      setSuccessMsg(L.status.done);
    } catch (err: any) {
      setErrorMsg(getAuthErrorMessage(err?.message));
    } finally {
      setActionLoading((prev) => ({ ...prev, [userId]: false }));
    }
  };

  const handleToggleStatus = async (user: User) => {
    const nextStatus = user.status === "disabled" ? "active" : "disabled";
    try {
      setActionLoading((prev) => ({ ...prev, [user.id]: true }));
      setErrorMsg(null);
      setSuccessMsg(null);
      const updated = await updateAdminUser(user.id, { status: nextStatus });
      setUsers((prev) => prev.map((u) => (u.id === user.id ? updated : u)));
      setSuccessMsg(L.status.done);
    } catch (err: any) {
      setErrorMsg(getAuthErrorMessage(err?.message));
    } finally {
      setActionLoading((prev) => ({ ...prev, [user.id]: false }));
    }
  };

  const handleRevokeSessions = async (userId: string) => {
    try {
      setActionLoading((prev) => ({ ...prev, [userId]: true }));
      setErrorMsg(null);
      setSuccessMsg(null);
      await revokeUserSessions(userId);
      setSuccessMsg(L.status.done);
    } catch (err: any) {
      setErrorMsg(getAuthErrorMessage(err?.message));
    } finally {
      setActionLoading((prev) => ({ ...prev, [userId]: false }));
    }
  };

  if (!authLoading && !hasPermission) {
    return (
      <div className="flex-1 p-8 flex items-center justify-center">
        <div className="max-w-md w-full bg-slate-900 border border-slate-800 rounded-xl p-6 text-center space-y-4">
          <h2 className="text-xl font-bold text-white">{L.heading.adminUsers}</h2>
          <p className="text-rose-400 text-sm">{L.error.forbidden}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 p-8 overflow-y-auto space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">
            {L.heading.adminUsers}
          </h1>
        </div>
        <button
          type="button"
          onClick={loadUsers}
          disabled={loading}
          className="px-3 py-1.5 bg-slate-200 hover:bg-slate-300 text-slate-800 text-sm font-medium rounded-lg transition"
        >
          {loading ? L.status.processing : L.action.refresh}
        </button>
      </div>

      {errorMsg && (
        <div className="p-4 bg-rose-100 border border-rose-300 text-rose-800 rounded-lg text-sm">
          {errorMsg}
        </div>
      )}

      {successMsg && (
        <div className="p-4 bg-emerald-100 border border-emerald-300 text-emerald-800 rounded-lg text-sm">
          {successMsg}
        </div>
      )}

      {/* Invite user section */}
      <div className="bg-white border border-slate-200 rounded-xl shadow-sm p-6 space-y-4">
        <h2 className="text-lg font-semibold text-slate-900">
          {L.heading.inviteUser}
        </h2>
        <form onSubmit={handleInvite} className="flex flex-wrap items-center gap-4">
          <input
            type="email"
            placeholder={L.field.email}
            value={inviteEmail}
            onChange={(e) => setInviteEmail(e.target.value)}
            required
            className="flex-1 min-w-[240px] px-3.5 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-sky-500"
          />
          <select
            value={inviteRole}
            onChange={(e) => setInviteRole(e.target.value as Role)}
            className="px-3.5 py-2 border border-slate-300 rounded-lg text-sm bg-white focus:outline-none focus:ring-2 focus:ring-sky-500"
          >
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
          <button
            type="submit"
            disabled={inviting}
            className="px-5 py-2 bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-white text-sm font-medium rounded-lg shadow-sm transition"
          >
            {inviting ? L.status.processing : L.action.invite}
          </button>
        </form>
      </div>

      {/* Users table */}
      <div className="bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm text-slate-700">
            <thead className="bg-slate-50 border-b border-slate-200 text-xs uppercase font-medium text-slate-500">
              <tr>
                <th className="px-6 py-3.5">{L.field.email}</th>
                <th className="px-6 py-3.5">{L.field.name}</th>
                <th className="px-6 py-3.5">{L.field.role}</th>
                <th className="px-6 py-3.5">{L.field.status}</th>
                <th className="px-6 py-3.5">{L.field.lastLogin}</th>
                <th className="px-6 py-3.5 text-right">{L.action.details}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {loading && users.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-6 py-8 text-center text-slate-400">
                    {L.status.processing}
                  </td>
                </tr>
              ) : users.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-6 py-8 text-center text-slate-400">
                    {L.help.noUsersFound}
                  </td>
                </tr>
              ) : (
                users.map((user) => {
                  const isBusy = Boolean(actionLoading[user.id]);
                  return (
                    <tr key={user.id} className="hover:bg-slate-50/70 transition">
                      <td className="px-6 py-4 font-medium text-slate-900">
                        {user.email}
                      </td>
                      <td className="px-6 py-4 text-slate-600">
                        {user.display_name || "—"}
                      </td>
                      <td className="px-6 py-4">
                        <select
                          value={user.role}
                          disabled={isBusy}
                          onChange={(e) =>
                            handleRoleChange(user.id, e.target.value as Role)
                          }
                          className="px-2.5 py-1 border border-slate-300 rounded text-xs bg-white focus:outline-none focus:ring-1 focus:ring-sky-500"
                        >
                          {ROLES.map((r) => (
                            <option key={r} value={r}>
                              {r}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="px-6 py-4">
                        <span
                          className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium ${
                            user.status === "active"
                              ? "bg-emerald-100 text-emerald-800"
                              : user.status === "disabled"
                              ? "bg-rose-100 text-rose-800"
                              : "bg-amber-100 text-amber-800"
                          }`}
                        >
                          {user.status === "active"
                            ? L.status.active
                            : user.status === "disabled"
                            ? L.status.disabled
                            : L.status.invited}
                        </span>
                      </td>
                      <td className="px-6 py-4 text-slate-500 text-xs">
                        {user.last_login_at
                          ? new Date(user.last_login_at).toLocaleString()
                          : "—"}
                      </td>
                      <td className="px-6 py-4 text-right space-x-2">
                        <button
                          type="button"
                          disabled={isBusy}
                          onClick={() => handleToggleStatus(user)}
                          className="px-2.5 py-1 text-xs font-medium rounded border border-slate-300 hover:bg-slate-100 transition"
                        >
                          {user.status === "disabled"
                            ? L.action.enable
                            : L.action.disable}
                        </button>
                        <button
                          type="button"
                          disabled={isBusy}
                          onClick={() => handleRevokeSessions(user.id)}
                          className="px-2.5 py-1 text-xs font-medium rounded border border-slate-300 hover:bg-slate-100 transition"
                        >
                          {L.action.revokeSessions}
                        </button>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
