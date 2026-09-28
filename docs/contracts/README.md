# API contracts (v6)

Frontend task cards build against these contracts in **mock mode**, while the matching backend WPs, owned by Claude, implement them. **A contract changes only through a PR that updates this folder**, and that PR must say which cards and WPs are affected.

| Contract | Consumer card (frontend) | Provider WP (backend) |
|---|---|---|
| [auth_v1.md](auth_v1.md) | WP-4.2 | WP-4.1 |
| [triage_v6.md](triage_v6.md) | WP-6.4 | WP-6.1/6.3 (+ worker rewrite) |
| [mitosis_v6.md](mitosis_v6.md) | WP-7.7 | WP-7.6 |
| [grading_v6.md](grading_v6.md) | WP-8.5 | WP-8.1/8.4 |
| [research_v1.md](research_v1.md) | WP-9.2 | WP-5.5 (batches, metrics.json), WP-9.1 (research API) |

## Conventions (all contracts)

- **Coordinates** are slide level-0 **micrometres** (`*_um`). The frontend converts to pixels with the slide's `mpp_x` / `mpp_y`.
- **Errors** are JSON `{"error": "<snake_case_code>", "detail": "<human text>", ...extra}` with the listed HTTP status. The UI keys its messages off `error`, never off `detail`.
- **Provenance.** Every stage payload has a `provenance` object:
  ```ts
  interface Provenance { stage: string; model_versions: Record<string, string>; config_hash: string; run_mode: "clinical" | "eval" | "shadow" }
  ```
  The UI shows it only in the provenance popover (SPEC-10 R4).
- **Mutations** return the full, updated stage payload unless stated otherwise. The UI never recomputes counts, areas or scores; it displays server values.
- **Auth.** All routes except `/health` and `/api/v1/auth/session|logout` require the session cookie. Mutating requests send `X-CSRF-Token` (see `auth_v1.md`).
- **Mock mode.** With `NEXT_PUBLIC_API_MOCK=1`, the area client in `frontend/lib/api/<area>.ts` returns fixtures from `frontend/lib/mock/<area>.json`, built from the examples in each contract, and simulates mutations in memory.
