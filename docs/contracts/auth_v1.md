# Contract: auth v1 (SPEC-03)

## Types

```ts
type Role = "admin" | "researcher" | "pathologist" | "viewer";

interface Me {
  id: string;
  email: string;
  display_name: string | null;
  role: Role;
  permissions: string[];          // e.g. ["case:read","batch:create",...] — SPEC-03 §4.1
}

interface User {
  id: string;
  email: string;
  display_name: string | null;
  role: Role;
  status: "invited" | "active" | "disabled";
  last_login_at: string | null;   // ISO 8601
  created_at: string;
}
```

## Endpoints

| Method | Path | Body | 2xx | Errors |
|---|---|---|---|---|
| POST | `/api/v1/auth/session` | `{ credential: string }` (Google ID token from Google Identity Services) | `200 { user: Me }`; sets cookies `og_session` (HttpOnly) and `og_csrf` (readable) | `401 invalid_token` · `403 domain_not_allowed` · `403 not_provisioned` · `403 user_disabled` |
| GET | `/api/v1/auth/me` | — | `200 Me` | `401 session_expired` |
| POST | `/api/v1/auth/logout` | — | `204` | — |
| GET | `/api/v1/admin/users` | — | `200 User[]` | `403 forbidden` |
| POST | `/api/v1/admin/users` | `{ email: string, role: Role }` | `201 User` (status `invited`) | `409 user_exists` · `422 invalid_email` |
| PATCH | `/api/v1/admin/users/{id}` | `{ role?: Role, status?: "active" \| "disabled" }` | `200 User` | `404 not_found` · `409 last_admin` |
| POST | `/api/v1/admin/users/{id}/revoke-sessions` | — | `204` | `404 not_found` |

## CSRF

Every non-GET request must send the header `X-CSRF-Token`, set to the current value of the `og_csrf` cookie. A missing or wrong token returns `403 csrf_failed`.

## Frontend environment

| Variable | Meaning |
|---|---|
| `NEXT_PUBLIC_AUTH_ENABLED` | `"1"` turns on the login flow and the middleware redirect. **Default off** until WP-4.1 is deployed; with it off, the app behaves as today |
| `NEXT_PUBLIC_GOOGLE_CLIENT_ID` | OAuth web client ID used by Google Identity Services |
| `NEXT_PUBLIC_AUTH_DOMAIN_HINT` | Text such as `example.org`, shown on the login page ("Use your example.org Google account") |

## Examples (mock fixture `frontend/lib/mock/auth.json`)

```json
{
  "me": {"id": "u_1", "email": "admin@example.org", "display_name": "Admin", "role": "admin",
         "permissions": ["case:read","case:create","case:delete","slide:upload","slide:set_mpp","stage:review",
                         "stage:confirm","stage:retry","batch:create","batch:cancel","batch:read","research:read",
                         "research:annotate","labels:qa","issue:write","eval:test_split","model:promote",
                         "user:manage","audit:read_all"]},
  "users": [
    {"id": "u_1", "email": "admin@example.org", "display_name": "Admin", "role": "admin", "status": "active",
     "last_login_at": "2026-09-28T10:00:00Z", "created_at": "2026-09-01T00:00:00Z"},
    {"id": "u_2", "email": "path@example.org", "display_name": null, "role": "pathologist", "status": "invited",
     "last_login_at": null, "created_at": "2026-09-20T00:00:00Z"}
  ]
}
```
