# AGENTS.md — Working rules for coding agents on OncoGemma v6

Read this file completely before starting any task card.

## 1. What this repo is

OncoGemma is an AI copilot that helps pathologists grade H&E breast-cancer whole-slide images (WSIs). v6 is focused on **measured accuracy, with F1 as the north star**.

- **Specs:** `docs/specs/`. Read only the section your card points to.
- **Plan and status:** `docs/IMPLEMENTATION_PLAN.md`, `docs/STATUS.md`.
- **Your task:** a card in `docs/tasks/WP-*.md`. **The card is your contract.** Do only what it says.
- **API contracts for the frontend:** `docs/contracts/`.

| Path | Contents |
|---|---|
| `backend/app/` | FastAPI API (routers, models, core) |
| `backend/pipeline/` | Image and ML pipeline code |
| `backend/worker/` | Stage handlers run by the worker loop |
| `backend/eval/` | Evaluation package: metrics, datasets, splits (v6, new) |
| `backend/tests/` | pytest suite. `backend/tests/__init__.py` exists, and every new test directory needs an `__init__.py` |
| `frontend/` | Next.js 14 app router, Tailwind, OpenSeadragon viewer |
| `configs/` | YAML configuration and prompts |
| `tools/` | Repo tooling (v6, new) |

## 2. Environment (Windows, PowerShell)

The shell is **PowerShell**. Bash-style `VAR=value cmd` does **not** work.

```powershell
# backend tests (from repo root); conftest forces offline mocks and an in-memory SQLite DB
python -m pytest backend/tests -q -p no:cacheprovider
python -m pytest backend/tests/eval -q -p no:cacheprovider          # targeted
# set an env var for one session
$env:SOME_VAR = "value"
# frontend
cd frontend; npm ci; npx tsc --noEmit; npm run build
```

- Python is 3.12. If a test needs a missing library (e.g. `hypothesis`), add it to `backend/requirements-dev.txt` (create the file if absent), then `pip install` it.
- **Never** call live cloud services (GCS, Vertex AI, Gemini) from tests. Use fakes or fixtures.

## 3. Rules

1. **Stay inside your card's "Files you may touch" list.** If you believe another file must change, stop and write that in your PR description instead.
2. **Do not edit** `docs/specs/**`, `docs/contracts/**`, `docs/IMPLEMENTATION_PLAN.md` or `AGENTS.md`. Propose changes in the PR description.
3. **Pre-written tests are the acceptance criteria.** Do not change their assertions. If you think a test is wrong, explain why in the PR and leave the test unchanged.
4. **No silent fallbacks.**
   - Never add `except Exception: pass`, `except Exception: return <default>` or code that invents values when something fails.
   - Raise a specific exception instead.
   - v6 exists largely to remove this pattern.
5. **No hardcoded clinical or numeric constants** in `backend/pipeline`, `backend/worker` or `backend/app/routers` (thresholds, µm/px, radii, scores). They belong in `configs/` (SPEC-01 §3.8).
6. **No model or vendor names in UI text** (MedGemma, Gemini, Vertex, KongNet, …). After WP-9.3, every UI string comes from `frontend/lib/labels.ts`.
7. **No secrets.** Never commit `.env`, keys or tokens. Never put an API key in a URL.
8. **Keep changes minimal and match the existing code style.** Don't reformat files you don't otherwise change.
9. **Ambiguity.** When something is ambiguous, choose the most conservative reading, and list your assumption in the PR description under "Assumptions".

## 4. Git workflow

- Branch: `wp/<id>-<slug>`, e.g. `wp/5.1-metrics`.
- Commits: `<type>(<area>): <summary>`, where type is one of `feat|fix|refactor|test|docs|chore`.
- Open a PR to `main` titled `WP-<id>: <title>`. The PR body must contain:
  1. the card's done checklist, with every box ticked or explained;
  2. the exact acceptance commands you ran and their output tail;
  3. an "Assumptions" section;
  4. a "Proposed spec/contract changes" section, if any.
- Do not push to `main`. Do not merge your own PR.
