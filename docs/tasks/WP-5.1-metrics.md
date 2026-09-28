# WP-5.1 — Evaluation metrics module (`eval/metrics.py`)

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate | M | SPEC-00 §2, SPEC-02 §7 | — | C |

## Goal

Implement the program's single metrics module. Every F1 the program reports, whether from the CLI, the Research view or the promotion gate, comes from here. **Known-answer tests define the behaviour:** `backend/tests/eval/test_metrics.py` (currently skipped).

## Read first (only these)

- `docs/specs/00-program-overview.md` §2 (the metric definitions are normative)
- `backend/tests/eval/test_metrics.py`
- `AGENTS.md`

## Files you may touch

- Create `backend/eval/__init__.py` (empty) and `backend/eval/metrics.py`.
- Create or append `backend/requirements-dev.txt` only if needed. `numpy` and `scipy` are already available.

## Interfaces

```python
# backend/eval/metrics.py   (numpy + scipy only; pure functions; no I/O)

@dataclass(frozen=True)
class MatchResult:
    pairs: list[tuple[int, int]]    # (gt_index, pred_index) of matched pairs
    tp: int; fp: int; fn: int

@dataclass(frozen=True)
class PRF:
    tp: int; fp: int; fn: int
    precision: float; recall: float; f1: float   # math.nan when the denominator is 0

@dataclass
class RegionPoints:
    gt_um: np.ndarray                         # (n, 2) micrometres
    pred_um: np.ndarray                       # (m, 2)
    pred_scores: np.ndarray | None = None     # (m,) final scores, required for thresholds and PR curves

@dataclass
class CaseMitosis:
    case_id: str
    regions: list[RegionPoints]

@dataclass(frozen=True)
class PRCurve:
    thresholds: np.ndarray    # unique prediction scores, descending
    precision: np.ndarray     # at each threshold (predictions with score >= t)
    recall: np.ndarray
    f1: np.ndarray
    ap: float                 # sum_k (R_k - R_{k-1}) * P_k, with R_0 = 0

@dataclass(frozen=True)
class MacroF1:
    per_class: dict[int, float]   # classes present in y_true or y_pred (never 'none')
    macro: float
    coverage: float               # fraction of cases with a non-'none' prediction
    n: int

@dataclass(frozen=True)
class BandMetrics:
    f1_high: float; macro_f1_lm: float; sum_mae: float; n_high: int; n_lm: int

@dataclass(frozen=True)
class CI:
    point: float; low: float; high: float; B: int; seed: int

@dataclass(frozen=True)
class DeltaCI:
    delta: float; low: float; high: float; B: int; seed: int

def match_points(gt_um, pred_um, radius_um: float = 7.5) -> MatchResult
def mitosis_f1(cases, radius_um: float = 7.5, midog_compat: bool = False,
               score_threshold: float | None = None) -> PRF
def mitosis_pr_curve(cases, radius_um: float = 7.5) -> PRCurve
def macro_f1(y_true, y_pred, labels=(1, 2, 3), none_label="none") -> MacroF1
def band_metrics(gt_total, pred_total, pred_grade) -> BandMetrics
def qwk(y_true, y_pred, labels=(1, 2, 3)) -> float
def bootstrap(metric_fn, units, B: int = 2000, seed: int = 0, alpha: float = 0.05) -> CI
def paired_bootstrap_delta(metric_fn, units_a, units_b, B: int = 2000, seed: int = 0,
                           alpha: float = 0.05) -> DeltaCI
def mcnemar_exact(correct_a, correct_b) -> float
```

## Algorithm requirements

- **`match_points`:**
  - Use `scipy.optimize.linear_sum_assignment` on cost `C[i, j] = d_ij` if `d_ij ≤ radius` (inclusive), else `1e6`.
  - Keep only assigned pairs with `d ≤ radius`.
  - Handle empty arrays without calling scipy.
- **`mitosis_f1`:**
  - Apply `score_threshold` per region first, keeping predictions with `score >= threshold`.
  - Match per region, then sum TP, FP and FN over all regions of all cases (micro).
  - `midog_compat=True`: after 1:1 matching, an unmatched prediction within `radius` of **any** ground-truth point is not counted as FP.
- **`mitosis_pr_curve`:** for each unique score, recompute 1:1 matches with the predictions at or above it. Use the AP formula exactly as written (no interpolation).
- **`macro_f1`:**
  - The classes are the members of `labels` present in `y_true ∪ y_pred`.
  - `none` counts toward FN for the true class and is never a class itself.
  - `macro` is the plain mean over those classes.
- **`band_metrics`** (SPEC-00 §2.2):
  - `f1_high` is binary F1 for "total ∈ {8, 9}". A missing `pred_total` counts as a negative prediction.
  - `macro_f1_lm` is restricted to cases with `gt_total ∈ {3..6}`, with true class G1 for 3–5 and G2 for 6. Per-class TP, FP and FN use `pred_grade` inside the band; a G3 or missing prediction is a miss and never a FP. The macro is over the classes present in the band's ground truth.
  - `sum_mae` is over cases where both totals are present.
- **`qwk`:** quadratic-weighted Cohen's κ over the cases with a prediction. It must equal `sklearn.metrics.cohen_kappa_score(..., weights="quadratic")`. You may call sklearn.
- **`bootstrap`:**
  - `point = metric_fn(units)`.
  - For each of B resamples, draw indices with replacement using `np.random.default_rng(seed)`, and apply `metric_fn` to the resampled units, preserving element objects.
  - The CI is the `alpha/2` and `1 − alpha/2` percentiles, `method="linear"`.
- **`paired_bootstrap_delta`:**
  - Raise `ValueError` if the lengths differ.
  - Use the **same** index draws for both arms.
  - `delta = metric_fn(b) − metric_fn(a)`.
- **`mcnemar_exact`:**
  - `b = #(a correct, b wrong)`, `c = #(a wrong, b correct)`.
  - Result is the two-sided exact binomial `p = min(1, 2 · BinomCDF(min(b, c); b + c, 0.5))`.
  - Return `1.0` when `b + c = 0`.

## Acceptance (run these)

```powershell
python -m pytest backend/tests/eval/test_metrics.py -q -p no:cacheprovider     # all pass, 0 skipped
python -m pytest backend/tests -q -p no:cacheprovider
```

## Out of scope — do not do

- Do not build `metrics.json` assembly, file I/O or a CLI (that is WP-5.5).
- Do not add plotting.

## Done checklist

- [ ] All metrics tests pass, 0 skipped
- [ ] Module has no I/O and no global state; docstrings cite SPEC-00 section numbers
