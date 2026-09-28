# WP-4.2 — Frontend auth: login screen, session handling, admin users page

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate | M | SPEC-03 §3–4.3 | WP-9.3 merged (labels) | B |

## Goal

Build the frontend side of Google Workspace SSO against [`docs/contracts/auth_v1.md`](../contracts/auth_v1.md), and remove the client-chosen identity headers. Everything new sits behind `NEXT_PUBLIC_AUTH_ENABLED`, so the app keeps working exactly as today until the backend (WP-4.1) ships.

## Read first (only these)

- `docs/contracts/auth_v1.md`, `docs/contracts/README.md`
- `docs/specs/03-auth-rbac-safety.md` §3.1, §4.1 (permission matrix), §4.3, §5.6 (headers)
- `AGENTS.md`

## Files you may touch

- **Create:**
  - `frontend/app/login/page.tsx`
  - `frontend/app/admin/users/page.tsx`
  - `frontend/middleware.ts`
  - `frontend/lib/api/auth.ts`
  - `frontend/lib/auth/AuthProvider.tsx` (context with `me`, `can(permission)`, `signOut()`)
  - `frontend/lib/mock/auth.json`
- **Edit:**
  - `frontend/lib/api.ts`: remove every `"X-User-Role"` header, and route all requests through a shared `apiFetch` that adds `credentials: "include"` and the CSRF header
  - `frontend/app/layout.tsx`: wrap the app in `AuthProvider`, and add a user menu with a sign-out button
  - `frontend/next.config.mjs`: add a `headers()` block only
  - `frontend/lib/labels.ts`: add new keys only

## Tasks

1. **`apiFetch(input, init)` in `lib/api/auth.ts`.**
   - Always sets `credentials: "include"`.
   - On non-GET requests, reads the `og_csrf` cookie and sets `X-CSRF-Token`.
   - On `401` with `NEXT_PUBLIC_AUTH_ENABLED === "1"`, redirects to `/login?next=<current path>`.
   - Migrate every function in `lib/api.ts` to `apiFetch`, and delete all `X-User-Role` headers. This is safe today: the v5 backend defaults to the `pathologist` role when the header is absent.
2. **Login page.**
   - Load the Google Identity Services script (`https://accounts.google.com/gsi/client`) and render its button with `NEXT_PUBLIC_GOOGLE_CLIENT_ID`.
   - On the credential callback, `POST /api/v1/auth/session`, then redirect to `next`, or to `/cases` when `next` is absent.
   - Show the domain hint.
   - Map the error codes `invalid_token`, `domain_not_allowed`, `not_provisioned`, `user_disabled` and `session_expired` to `L.error.*` messages.
3. **`middleware.ts`.** When `NEXT_PUBLIC_AUTH_ENABLED === "1"`, redirect every route except `/login`, `/_next/*` and static assets to `/login?next=…` if the `og_session` cookie is absent. When the flag is off, it does nothing.
4. **`AuthProvider`.**
   - Loads `GET /api/v1/auth/me`.
   - Exposes `can(p)` by checking `me.permissions`.
   - Hides navigation entries the user lacks permission for (Research, Admin). The server stays authoritative.
5. **Admin users page** (`user:manage`):
   - Table showing email, name, role, status and last login.
   - Actions: invite (email + role), change role, disable/enable, revoke sessions.
   - Handle the `409 last_admin` and `409 user_exists` errors.
6. **Mock mode.** With `NEXT_PUBLIC_API_MOCK=1`, the auth client serves `lib/mock/auth.json`, and the login page shows a "Mock sign in" button instead of the Google button.
7. **Security headers** in `next.config.mjs`, per SPEC-03 §5.6: the CSP (allowing `accounts.google.com/gsi/*`), `X-Frame-Options: DENY`, `Referrer-Policy` and HSTS. Keep the existing `rewrites()`.

## Acceptance (run these)

```powershell
cd frontend
npx tsc --noEmit; npm run build; npm run lint:labels
git grep -n "X-User-Role" -- frontend        # no output
```

Manual checks, with screenshots in the PR:
- Flag off: the app behaves as before.
- Flag on with mock: login, then cases, sign-out and the admin users page all work.
- Every error code maps to a message.

## Out of scope — do not do

- Any backend change (WP-4.1).
- Storing tokens in `localStorage`.
- Any password UI.

## Done checklist

- [ ] `apiFetch` used everywhere; no `X-User-Role` left
- [ ] Login, middleware, provider and admin page work in mock mode
- [ ] Security headers added
- [ ] Build, type-check and label lint pass
