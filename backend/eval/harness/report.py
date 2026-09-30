"""Metrics of a finished run and paired comparison of two runs (SPEC-02 §5.5, §7; SPEC-00 §2).

Every item of the run counts (SPEC-00 rule 2): a failed, QC-excluded or no-tumour item is a
``none`` prediction, which lowers coverage and F1 instead of leaving the denominator. Only
cancelled items and items without ground truth for a metric are left out, and both are counted.
Confidence intervals resample patients, the split unit.
"""
from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.validation import ValidationItem, ValidationRun
from eval.harness.documents import (
    ClassMetrics,
    FailureGroup,
    GradeMetrics,
    Interval,
    MetricsDocument,
    RunInfo,
)
from eval.harness.runs import read_manifest, split_rows
from eval.metrics import band_metrics, bootstrap, macro_f1, mcnemar_exact, paired_bootstrap_delta, qwk

NONE = "none"
COMPONENTS = {"tubule": "gt_tubule", "pleo": "gt_pleo", "mitoses": "gt_mitoses"}


class RunNotFinishedError(RuntimeError):
    """Metrics need every item to have ended."""


class RunsNotComparableError(ValueError):
    """Two runs must cover the same slides of the same dataset split."""


@dataclass(frozen=True)
class Case:
    """One item with its ground truth and prediction."""

    slide_id: str
    patient_id: str
    gt: dict
    pred: dict  # grade, total, tubule, pleo, mitoses (None when there is no prediction)


def _gt(row: pd.Series, column: str):
    value = row[column]
    return None if pd.isna(value) else int(value)


def run_cases(session: Session, run: ValidationRun) -> list[Case]:
    items = session.scalars(select(ValidationItem).where(ValidationItem.run_id == run.id)).all()
    active = [i for i in items if i.status in ("pending", "running")]
    if active:
        raise RunNotFinishedError(f"run {run.id} still has {len(active)} pending or running items")
    manifest, sha256 = read_manifest(run.manifest_uri)
    if sha256 != run.manifest_sha256:
        raise RunsNotComparableError(f"{run.manifest_uri} changed since run {run.id} was created")
    truth = split_rows(manifest, run)
    cases = []
    for item in sorted(items, key=lambda i: i.slide_id):
        if item.status == "cancelled":
            continue
        row = truth.loc[item.slide_id]
        gt = {"grade": _gt(row, "gt_grade"), "total": _gt(row, "gt_total"),
              **{name: _gt(row, column) for name, column in COMPONENTS.items()}}
        grading = (item.prediction or {}).get("grading") if item.status == "succeeded" else None
        pred = {key: None for key in ("grade", "total", *COMPONENTS)}
        if grading is not None:
            pred.update({key: grading.get(key) for key in pred})
        cases.append(Case(item.slide_id, item.patient_id, gt, pred))
    return cases


def by_patient(cases: Sequence[Case]) -> list[list[Case]]:
    groups: dict[str, list[Case]] = defaultdict(list)
    for case in cases:
        groups[case.patient_id].append(case)
    return [groups[p] for p in sorted(groups)]


def flat(units: Sequence[list[Case]]) -> list[Case]:
    return [case for unit in units for case in unit]


def _label(value) -> int | str:
    return NONE if value is None else value


def interval(fn: Callable[[list[Case]], float], units: list[list[Case]], B: int, seed: int) -> Interval:
    ci = bootstrap(lambda us: fn(flat(us)), units, B=B, seed=seed)
    return Interval(point=ci.point, low=ci.low, high=ci.high, B=B, seed=seed)


def class_metrics(key: str, units: list[list[Case]], B: int, seed: int) -> ClassMetrics:
    def score(cases):
        return macro_f1([c.gt[key] for c in cases], [_label(c.pred[key]) for c in cases]).macro

    point = macro_f1([c.gt[key] for c in flat(units)], [_label(c.pred[key]) for c in flat(units)])
    return ClassMetrics(
        n=point.n, coverage=point.coverage, macro_f1=interval(score, units, B, seed),
        per_class={str(k): v for k, v in point.per_class.items()},
    )


def grade_metrics(units: list[list[Case]], B: int, seed: int) -> GradeMetrics:
    base = class_metrics("grade", units, B, seed)

    def bands(cases):
        with_total = [c for c in cases if c.gt["total"] is not None]
        return band_metrics([c.gt["total"] for c in with_total], [c.pred["total"] for c in with_total],
                            [c.pred["grade"] for c in with_total])

    all_bands = bands(flat(units))
    return GradeMetrics(
        **base.model_dump(),
        qwk=interval(lambda cs: qwk([c.gt["grade"] for c in cs], [_label(c.pred["grade"]) for c in cs]), units, B, seed),
        f1_high=interval(lambda cs: bands(cs).f1_high, units, B, seed),
        macro_f1_lm=interval(lambda cs: bands(cs).macro_f1_lm, units, B, seed),
        sum_mae=interval(lambda cs: bands(cs).sum_mae, units, B, seed),
        n_high=all_bands.n_high, n_lm=all_bands.n_lm,
    )


def compute_metrics(session: Session, run_id, *, B: int = 2000, seed: int = 7) -> MetricsDocument:
    run = session.get(ValidationRun, run_id)
    if run is None:
        raise LookupError(f"validation run {run_id} not found")
    items = session.scalars(select(ValidationItem).where(ValidationItem.run_id == run.id)).all()
    cases = run_cases(session, run)
    unavailable: dict[str, str] = {}

    graded = [c for c in cases if c.gt["grade"] is not None]
    if "grading" not in run.stages:
        unavailable["grade"] = "the run did not include grading"
    elif not graded:
        unavailable["grade"] = "no item of the split has a ground-truth grade"
    elif len(graded) < len(cases):
        unavailable["grade_missing_truth"] = f"{len(cases) - len(graded)} items have no ground-truth grade and are left out"
    grade = grade_metrics(by_patient(graded), B, seed) if "grade" not in unavailable else None

    components = {}
    for name in COMPONENTS:
        with_truth = [c for c in cases if c.gt[name] is not None]
        if "grading" in run.stages and with_truth:
            components[name] = class_metrics(name, by_patient(with_truth), B, seed)
        else:
            unavailable[name] = "no ground-truth component score" if "grading" in run.stages else "the run did not include grading"
    # Point-level mitosis F1 needs annotated figures (regions_uri); whole-slide runs have none yet.
    unavailable["mitosis_f1"] = "whole-slide runs have no point ground truth; use the mitosis_roi component harness"

    failures = Counter((i.status, i.failed_stage, i.error_class) for i in items if i.status in ("failed", "excluded_qc"))
    runtimes = [i.runtime_s for i in items if i.runtime_s is not None]
    return MetricsDocument(
        run=RunInfo(
            id=str(run.id), name=run.name, dataset=run.dataset, split=run.split, arm=run.arm,
            stages=list(run.stages), mode=run.mode, status=run.status, is_locked_test=run.is_locked_test,
            config_hash=run.config_hash, registry_sha256=run.registry_sha256, manifest_uri=run.manifest_uri,
            manifest_sha256=run.manifest_sha256, splits_lock_sha256=run.splits_lock_sha256,
        ),
        items=dict(Counter(i.status for i in items)),
        failures=[FailureGroup(status=s, stage=st, error_class=e, n=n) for (s, st, e), n in sorted(
            failures.items(), key=lambda kv: (-kv[1], str(kv[0])))],
        grade=grade,
        components=components,
        unavailable=unavailable,
        cost_usd=float(sum(float(i.cost_usd or 0) for i in items)),
        runtime_s={
            "total": float(sum(runtimes)) if runtimes else None,
            "median": float(pd.Series(runtimes).median()) if runtimes else None,
        },
    )


# --- files -----------------------------------------------------------------------


def render_html(doc: MetricsDocument) -> str:
    """A static page rendered from metrics.json."""
    def ci(i: Interval) -> str:
        if i.point is None:
            return "n/a"
        return f"{i.point:.3f} [{i.low:.3f}, {i.high:.3f}]"

    rows = []
    if doc.grade is not None:
        g = doc.grade
        rows += [("Grade macro-F1", ci(g.macro_f1)), ("Grade coverage", f"{g.coverage:.3f} (n={g.n})"),
                 ("QWK", ci(g.qwk)), ("F1 high (total 8–9)", f"{ci(g.f1_high)} (n={g.n_high})"),
                 ("Macro-F1 low/moderate", f"{ci(g.macro_f1_lm)} (n={g.n_lm})"), ("Sum MAE", ci(g.sum_mae))]
    rows += [(f"{name} macro-F1", f"{ci(m.macro_f1)} (coverage {m.coverage:.3f})") for name, m in doc.components.items()]
    rows += [(f"Unavailable: {k}", v) for k, v in doc.unavailable.items()]
    rows += [(f"Failed at {f.stage}: {f.error_class}" if f.status == "failed" else "Excluded by QC", str(f.n))
             for f in doc.failures]
    run = doc.run
    body = "\n".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in rows)
    items = ", ".join(f"{html.escape(k)} {v}" for k, v in sorted(doc.items.items()))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Run {html.escape(run.name)}</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem;max-width:60rem}}table{{border-collapse:collapse}}
th,td{{text-align:left;padding:.3rem .8rem;border-bottom:1px solid #ddd}}code{{font-size:.85em}}</style></head>
<body><h1>{html.escape(run.name)}</h1>
<p>{html.escape(run.dataset)} / {html.escape(run.split)}{' (locked test)' if run.is_locked_test else ''} · arm {html.escape(run.arm or '-')} · items: {items}</p>
<p>Run <code>{html.escape(run.id)}</code> · config <code>{run.config_hash[:12]}</code> · registry <code>{run.registry_sha256[:12]}</code> · manifest <code>{run.manifest_sha256[:12]}</code></p>
<table>{body}</table>
<p>Intervals: 95% percentile bootstrap over patients, B={doc.grade.macro_f1.B if doc.grade else '-'}. Schema v{doc.metrics_schema_version}.</p>
</body></html>
"""


def write_report(doc: MetricsDocument, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics = out_dir / "metrics.json"
    metrics.write_text(doc.model_dump_json(indent=2), encoding="utf-8")
    page = out_dir / "report.html"
    page.write_text(render_html(doc), encoding="utf-8")
    return metrics, page


# --- compare ---------------------------------------------------------------------

COMPARE_METRICS: dict[str, Callable[[list[Case]], float]] = {
    "grade_macro_f1": lambda cs: macro_f1([c.gt["grade"] for c in cs], [_label(c.pred["grade"]) for c in cs]).macro,
    "qwk": lambda cs: qwk([c.gt["grade"] for c in cs], [_label(c.pred["grade"]) for c in cs]),
    "f1_high": lambda cs: band_metrics([c.gt["total"] for c in cs], [c.pred["total"] for c in cs],
                                       [c.pred["grade"] for c in cs]).f1_high,
    "sum_mae": lambda cs: band_metrics([c.gt["total"] for c in cs], [c.pred["total"] for c in cs],
                                       [c.pred["grade"] for c in cs]).sum_mae,
}


def compare_runs(session: Session, run_a, run_b, metric: str, *, B: int = 2000, seed: int = 7) -> dict:
    """Paired bootstrap of ``metric(b) - metric(a)`` over patients, and McNemar on grade correctness."""
    if metric not in COMPARE_METRICS:
        raise ValueError(f"metric must be one of {sorted(COMPARE_METRICS)}; point-level mitosis F1 needs the ROI harness")
    runs = [session.get(ValidationRun, r) for r in (run_a, run_b)]
    if None in runs:
        raise LookupError("both runs must exist")
    a, b = runs
    if (a.dataset, a.split) != (b.dataset, b.split):
        raise RunsNotComparableError(f"{a.dataset}/{a.split} vs {b.dataset}/{b.split}")
    cases_a = {c.slide_id: c for c in run_cases(session, a) if c.gt["grade"] is not None and c.gt["total"] is not None}
    cases_b = {c.slide_id: c for c in run_cases(session, b) if c.gt["grade"] is not None and c.gt["total"] is not None}
    if set(cases_a) != set(cases_b):
        raise RunsNotComparableError(
            f"the runs cover different slides ({len(set(cases_a) ^ set(cases_b))} differ); pairing needs the same set"
        )
    patients = sorted({c.patient_id for c in cases_a.values()})
    units_a = [[c for c in cases_a.values() if c.patient_id == p] for p in patients]
    units_b = [[cases_b[c.slide_id] for c in unit] for unit in units_a]
    fn = COMPARE_METRICS[metric]
    delta = paired_bootstrap_delta(lambda us: fn(flat(us)), units_a, units_b, B=B, seed=seed)
    order = sorted(cases_a)
    p_value = mcnemar_exact([cases_a[s].pred["grade"] == cases_a[s].gt["grade"] for s in order],
                            [cases_b[s].pred["grade"] == cases_b[s].gt["grade"] for s in order])
    return {
        "metric": metric, "run_a": str(a.id), "run_b": str(b.id), "n_slides": len(order), "n_patients": len(patients),
        "a": fn(flat(units_a)), "b": fn(flat(units_b)),
        "delta": delta.delta, "low": delta.low, "high": delta.high, "B": B, "seed": seed,
        "mcnemar_grade_p": p_value,
    }


def dumps(value: dict) -> str:
    return json.dumps(value, indent=2, sort_keys=True)
