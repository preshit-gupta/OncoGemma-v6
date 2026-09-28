# WP-5.3 — Patient-level stratified splits, lock file, leakage checks

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate | S | SPEC-02 §5.2, SPEC-00 §4 | — (run after or with WP-5.1; both create `backend/eval/__init__.py`, so keep it empty) | C |

## Goal

Build the deterministic split machinery that keeps train and test patients apart across every dataset. **Pre-written tests define the behaviour:** `backend/tests/eval/test_splits.py`.

## Read first (only these)

- `docs/specs/02-validation-harness-and-batch.md` §5.2
- `backend/tests/eval/test_splits.py`
- `AGENTS.md`

## Files you may touch

- Create `backend/eval/splits.py`, and `backend/eval/__init__.py` if it is absent (keep it empty).
- Create `tools/check_splits.py` (the CLI wrapper used by CI later).

## Interfaces

```python
# backend/eval/splits.py   (pandas + stdlib)
SPLITS = ("train", "val", "test")

class SplitLeakError(Exception): ...
class UnassignedUnitError(Exception): ...
class LockMismatchError(Exception): ...

def make_splits(df: pd.DataFrame, *, seed: int, unit_col: str = "patient_id",
                strata_cols: tuple[str, ...] = ("gt_grade", "tss_group", "native_mag"),
                ratios: tuple[float, float, float] = (0.6, 0.2, 0.2),
                min_stratum: int = 10) -> pd.DataFrame        # returns a copy with a 'split' column

def check_disjoint(frames: dict[str, pd.DataFrame], unit_col: str = "patient_id") -> None
def inherit_splits(target: pd.DataFrame, source: pd.DataFrame, unit_col: str = "patient_id") -> pd.DataFrame
def write_lock(paths: list[Path], lock_path: Path, root: Path) -> dict[str, str]    # {relpath: sha256}
def verify_lock(lock_path: Path, root: Path) -> bool                                 # raises LockMismatchError
```

## Algorithm (`make_splits`)

1. Raise `ValueError` unless `abs(sum(ratios) − 1) < 1e-9`.
2. **One record per unit:** sort by `(unit_col, "slide_id" if present)` and take each unit's first row's strata values.
3. **Stratum key:** the tuple of `strata_cols` values. Every stratum with fewer than `min_stratum` units is merged into a single `("__other__",)` stratum.
4. **Within each stratum:**
   - Order the units by `sha256(f"{seed}:{unit}")` hex. This makes the result independent of input order.
   - Allocate counts by the **largest remainder** method: `n_i = floor(r_i·N)`, then give the remaining units to the splits with the largest fractional parts, breaking ties in `train, val, test` order.
   - Assign units in that order: the first `n_train` to train, the next `n_val` to val, the rest to test.
5. Map each unit's split back onto **every** row of that unit.

## Other functions

- **`check_disjoint`:** raise `SplitLeakError` if any unit has more than one distinct split within a frame, or across frames. The message lists the offending units.
- **`inherit_splits`:** left-join the split from `source` by unit, preserving the target's row order and columns. If any unit is missing from `source`, raise `UnassignedUnitError` listing the missing units.
- **Lock file:** a JSON lock file mapping POSIX paths relative to `root` to the SHA-256 of each file's bytes. `verify_lock` recomputes the hashes and raises on any mismatch or missing file.

## `tools/check_splits.py`

`python tools/check_splits.py --lock eval/splits/SPLITS.lock --root backend`

1. Verify the lock.
2. Load every `*.parquet` listed in it.
3. Run `check_disjoint` across all of them.
4. Exit 0 if everything passes, otherwise exit 1.

## Acceptance (run these)

```powershell
python -m pytest backend/tests/eval/test_splits.py -q -p no:cacheprovider     # all pass, 0 skipped
python -m pytest backend/tests -q -p no:cacheprovider
```

## Out of scope — do not do

- Do not generate real dataset splits (that needs WP-5.2 manifests).
- Do not build the test-split access guard (that is WP-5.5).

## Done checklist

- [ ] Tests pass, 0 skipped
- [ ] `tools/check_splits.py` works on a small synthetic lock (describe the manual check in the PR)
