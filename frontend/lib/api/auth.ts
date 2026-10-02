import { L } from "@/lib/labels";
import mockAuthData from "@/lib/mock/auth.json";

export type Role = "admin" | "researcher" | "pathologist" | "viewer";

export interface Me {
  id: string;
  email: string;
  display_name: string | null;
  role: Role;
  permissions: string[];
}

export interface User {
  id: string;
  email: string;
  display_name: string | null;
  role: Role;
  status: "invited" | "active" | "disabled";
  last_login_at: string | null;
  created_at: string;
}

export function getCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(new RegExp("(^|;\\s*)" + name + "=([^;]*)"));
  return match ? decodeURIComponent(match[2]) : null;
}

export function setMockCookies() {
  if (typeof document !== "undefined") {
    document.cookie = "og_session=mock_session_token; path=/; max-age=28800; SameSite=Lax";
    document.cookie = "og_csrf=mock_csrf_token; path=/; max-age=28800; SameSite=Lax";
  }
}

export function clearMockCookies() {
  if (typeof document !== "undefined") {
    document.cookie = "og_session=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT; SameSite=Lax";
    document.cookie = "og_csrf=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT; SameSite=Lax";
  }
}

export function getAuthErrorMessage(errorCode: string | null | undefined): string {
  if (!errorCode) return L.error.genericError;
  const code = String(errorCode).toLowerCase().trim();
  switch (code) {
    case "invalid_token":
      return L.error.invalidToken;
    case "domain_not_allowed":
      return L.error.domainNotAllowed;
    case "not_provisioned":
      return L.error.notProvisioned;
    case "user_disabled":
      return L.error.userDisabled;
    case "session_expired":
      return L.error.sessionExpired;
    case "last_admin":
      return L.error.lastAdmin;
    case "user_exists":
      return L.error.userExists;
    case "invalid_email":
      return L.error.invalidEmail;
    case "forbidden":
      return L.error.forbidden;
    case "csrf_failed":
      return L.error.csrfFailed;
    default:
      return L.error.genericError;
  }
}

// Confirm and approve routes require an Idempotency-Key header (SPEC-03 §5.3.3); one fresh key per user action.
export function idempotencyHeaders(): Record<string, string> {
  return { "Idempotency-Key": crypto.randomUUID() };
}

export async function apiFetch(
  input: RequestInfo | URL,
  init?: RequestInit
): Promise<Response> {
  const headers = new Headers(init?.headers || {});
  const method = (init?.method || "GET").toUpperCase();

  if (method !== "GET" && method !== "HEAD") {
    const csrfToken = getCookie("og_csrf");
    if (csrfToken && !headers.has("X-CSRF-Token")) {
      headers.set("X-CSRF-Token", csrfToken);
    }
  }

  const res = await fetch(input, {
    ...init,
    headers,
    credentials: "include",
  });

  if (res.status === 401 && process.env.NEXT_PUBLIC_AUTH_ENABLED === "1") {
    if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.href = `/login?next=${next}`;
    }
  }

  return res;
}

// In-memory mock storage
let mockUsersList: User[] = (mockAuthData.users as User[]).map((u) => ({ ...u }));

export async function createSession(credential: string): Promise<{ user: Me }> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    setMockCookies();
    return { user: mockAuthData.me as Me };
  }

  const res = await apiFetch("/api/v1/auth/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ credential }),
  });

  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const code = data.detail || data.error || `HTTP ${res.status}`;
    throw new Error(code);
  }

  return res.json();
}

export async function getMe(): Promise<Me> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const sessionCookie = getCookie("og_session");
    if (!sessionCookie && process.env.NEXT_PUBLIC_AUTH_ENABLED === "1") {
      throw new Error("session_expired");
    }
    return mockAuthData.me as Me;
  }

  const res = await apiFetch("/api/v1/auth/me");
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || data.error || "session_expired");
  }
  return res.json();
}

export async function logout(): Promise<void> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    clearMockCookies();
    return;
  }

  await apiFetch("/api/v1/auth/logout", {
    method: "POST",
  }).catch(() => null);
}

export async function getAdminUsers(): Promise<User[]> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    return [...mockUsersList];
  }

  const res = await apiFetch("/api/v1/admin/users");
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || data.error || "forbidden");
  }
  return res.json();
}

export async function createAdminUser(email: string, role: Role): Promise<User> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const exists = mockUsersList.some(
      (u) => u.email.toLowerCase() === email.toLowerCase().trim()
    );
    if (exists) {
      throw new Error("user_exists");
    }
    const newUser: User = {
      id: `u_${Date.now()}`,
      email: email.toLowerCase().trim(),
      display_name: null,
      role,
      status: "invited",
      last_login_at: null,
      created_at: new Date().toISOString(),
    };
    mockUsersList.push(newUser);
    return newUser;
  }

  const res = await apiFetch("/api/v1/admin/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, role }),
  });

  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  }
  return res.json();
}

export async function updateAdminUser(
  id: string,
  updates: { role?: Role; status?: "active" | "disabled" }
): Promise<User> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const userIndex = mockUsersList.findIndex((u) => u.id === id);
    if (userIndex === -1) {
      throw new Error("not_found");
    }
    const currentUser = mockUsersList[userIndex];

    // Check last_admin safeguard
    if (
      currentUser.role === "admin" &&
      (updates.role && updates.role !== "admin" || updates.status === "disabled")
    ) {
      const activeAdmins = mockUsersList.filter(
        (u) => u.role === "admin" && u.status === "active" && u.id !== id
      );
      if (activeAdmins.length === 0) {
        throw new Error("last_admin");
      }
    }

    const updatedUser: User = {
      ...currentUser,
      ...updates,
    };
    mockUsersList[userIndex] = updatedUser;
    return updatedUser;
  }

  const res = await apiFetch(`/api/v1/admin/users/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });

  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  }
  return res.json();
}

export async function revokeUserSessions(id: string): Promise<void> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    return;
  }

  const res = await apiFetch(`/api/v1/admin/users/${id}/revoke-sessions`, {
    method: "POST",
  });

  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  }
}
