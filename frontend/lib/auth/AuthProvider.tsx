"use client";

import React, { createContext, useContext, useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { L } from "@/lib/labels";
import { Me, getMe, logout } from "@/lib/api/auth";

export interface AuthContextType {
  me: Me | null;
  loading: boolean;
  error: string | null;
  can: (permission: string) => boolean;
  signOut: () => Promise<void>;
  refreshMe: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const isAuthEnabled = process.env.NEXT_PUBLIC_AUTH_ENABLED === "1";

  const refreshMe = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const user = await getMe();
      setMe(user);
    } catch (err: any) {
      setMe(null);
      setError(err?.message || "Failed to authenticate");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refreshMe();
  }, [refreshMe]);

  const can = useCallback(
    (permission: string): boolean => {
      if (!isAuthEnabled) {
        return true;
      }
      if (!me || !Array.isArray(me.permissions)) {
        return false;
      }
      return me.permissions.includes(permission);
    },
    [isAuthEnabled, me]
  );

  const signOut = useCallback(async () => {
    try {
      await logout();
    } finally {
      setMe(null);
      if (typeof window !== "undefined") {
        if (isAuthEnabled) {
          window.location.href = "/login";
        } else {
          window.location.reload();
        }
      }
    }
  }, [isAuthEnabled]);

  return (
    <AuthContext.Provider
      value={{
        me,
        loading,
        error,
        can,
        signOut,
        refreshMe,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextType {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}

export function UserMenu() {
  const { me, can, signOut } = useAuth();
  const isAuthEnabled = process.env.NEXT_PUBLIC_AUTH_ENABLED === "1";

  return (
    <div className="flex items-center space-x-4 text-sm">
      <nav className="flex items-center space-x-3 text-slate-300">
        <Link
          href="/cases"
          className="hover:text-white transition px-2 py-1 rounded hover:bg-slate-800"
        >
          {L.heading.cases}
        </Link>
        {can("research:read") && (
          <Link
            href="/research"
            className="hover:text-white transition px-2 py-1 rounded hover:bg-slate-800"
          >
            {L.heading.research}
          </Link>
        )}
        {can("user:manage") && (
          <Link
            href="/admin/users"
            className="hover:text-white transition px-2 py-1 rounded hover:bg-slate-800"
          >
            {L.heading.adminUsers}
          </Link>
        )}
      </nav>

      {me ? (
        <div className="flex items-center space-x-3 border-l border-slate-700 pl-3">
          <div className="flex flex-col text-right">
            <span className="text-xs font-medium text-slate-200">
              {me.display_name || me.email}
            </span>
            <span className="text-[10px] text-sky-400 capitalize">
              {me.role}
            </span>
          </div>
          <button
            type="button"
            onClick={() => signOut()}
            className="px-2.5 py-1 text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white rounded border border-slate-700 transition"
          >
            {L.action.signOut}
          </button>
        </div>
      ) : isAuthEnabled ? (
        <div className="flex items-center space-x-2 border-l border-slate-700 pl-3">
          <Link
            href="/login"
            className="px-2.5 py-1 text-xs bg-sky-600 hover:bg-sky-500 text-white rounded transition"
          >
            {L.action.signIn}
          </Link>
        </div>
      ) : (
        <div className="flex items-center space-x-1.5 text-slate-400">
          <span className="w-2 h-2 rounded-full bg-emerald-400"></span>
          <span>{L.status.done}</span>
        </div>
      )}
    </div>
  );
}
