# WP-2.5 — Hardcoded-literal scanner

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate | S | SPEC-01 §3.8 (N14 "stupidity check") | — | D |

## Goal

Write an AST-based scanner that flags hardcoded clinical and numeric fallbacks in backend code. CI will use it to block new ones (CI wiring happens in WP-0.1). **Pre-written tests define the behaviour:** `tools/tests/test_lint_literals.py`.

## Read first (only these)

- `docs/specs/01-measurement-integrity.md` §3.8
- `tools/tests/test_lint_literals.py`
- `AGENTS.md`

## Files you may touch

- Create:
  - `tools/lint_literals.py`
  - `tools/literals_allowlist.yaml` (start as `[]`)
  - `tools/literals_baseline.json` (generated, see Tasks)
  - `tools/tests/__init__.py` (empty)
- Create `backend/requirements-dev.txt` if you need `pyyaml` or another dev-only dependency (`pyyaml` is already installed).

## Interfaces

```python
# tools/lint_literals.py  (stdlib + pyyaml only)
@dataclass(frozen=True)
class Finding:
    rule: str      # "L1".."L4"
    path: str      # repo-relative, forward slashes, e.g. "backend/worker/x.py"
    line: int
    col: int
    snippet: str   # stripped source line

def scan_source(source: str, path: str) -> list[Finding]: ...
def line_hash(line_text: str) -> str: ...              # sha1 hex of line_text.strip()
def filter_allowlisted(findings: list[Finding], allowlist: list[dict]) -> list[Finding]: ...
def main(argv: list[str] | None = None) -> int: ...    # returns an exit code; also callable as a script
```

### CLI

`python tools/lint_literals.py [--root DIR] [--allowlist FILE] [--baseline FILE] [--write-baseline]`

- `--root` defaults to the repo root. The scanner walks `backend/**/*.py`, excluding `backend/tests/**`, `backend/alembic/**` and any `__pycache__`.
- Read files with `encoding="utf-8-sig"`: some repo files start with a UTF-8 BOM, and `ast.parse` rejects it.
- **Expected scale** (measured with a reference implementation on 2026-09-28, before WP-1.2 deletions): 400 findings, split L1 84, L2 149, L3 30, L4 137. After WP-1.2 the count will be lower. Explain any large difference in the PR.
- **Exit codes:**
  - `0` when no findings remain after the allowlist and baseline are applied;
  - `1` otherwise, after printing `path:line:col: RULE snippet` per finding.
- `--write-baseline` writes all current non-allowlisted findings to the baseline file as a JSON list of `{path, rule, line_hash}` and returns `0`.
- **Matching:** a finding matches the baseline or allowlist by `(path, rule, line_hash)`, **not** by line number, so it survives edits elsewhere in the file.

### Rules

- **L1** — any file under `backend/`: `BoolOp(Or)` whose **last** operand is a numeric or string `Constant`.
- **L2** — only under `backend/pipeline/`, `backend/worker/`, `backend/app/routers/`: a call `X.get(key, default)` whose default is a numeric or string `Constant`. `None`, containers and names are allowed.
- **L3** — any file under `backend/`: a function parameter whose name contains `mpp`, `threshold`, `radius`, `conf` or ends in `_um`, and whose default is a numeric `Constant`.
- **L4** — only under `backend/pipeline/`: a numeric `Constant` operand inside a `Compare` node, unless the value is in the allowed set `{0, 1, -1, 2, 255, 1e-6, 1e-8, 0.5}`.

## Tasks

1. Implement until `python -m pytest tools/tests -q -p no:cacheprovider` passes with 0 skipped.
2. Run `python tools/lint_literals.py --baseline tools/literals_baseline.json --write-baseline` on the real repo and commit the baseline. Its size is the burn-down counter. Report the count per rule in the PR.

## Acceptance (run these)

```powershell
python -m pytest tools/tests -q -p no:cacheprovider
python tools/lint_literals.py --baseline tools/literals_baseline.json     # exit 0
```

## Out of scope — do not do

- Do not fix any finding in backend code. The burn-down happens in the Claude WPs.
- Do not add CI configuration (that is WP-0.1).

## Done checklist

- [ ] Tests pass (0 skipped)
- [ ] Baseline committed; counts per rule reported in the PR
