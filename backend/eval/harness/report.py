"""Metrics of a finished run and paired comparison of two runs (SPEC-02 §5.5, §7; SPEC-00 §2).

``metrics.json`` is the contract's ``MetricsV1`` (docs/contracts/research_v1.md). Every item of
the run counts (SPEC-00 rule 2): a failed, QC-excluded or no-tumour item is a ``none``
prediction, which lowers coverage and F1 instead of leaving the denominator. Only cancelled
items and items without ground truth for a metric are left out; both are counted, and every
metric the run cannot support is listed in ``unavailable`` with the reason. Intervals resample
patients, the split unit. All metric arithmetic is ``eval/metrics.py`` (SPEC-00 rule 4).
"""
from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tasks import ProducerKind
from app.models.decision_record import DecisionRecord
from app.models.validation import ValidationItem, ValidationRun
from eval.harness.documents import (
    Bootstrap,
    Calibration,
    Confusion,
    ConfusionSet,
    Cost,
    Counts,
    Curves,
    FailureGroup,
    Headline,
    Metric,
    MetricsDocument,
    ModelCost,
    RunInfo,
    S5Metrics,
    SliceRow,
    StageMetrics,
    dump_metrics,
)
from eval.harness.runs import read_manifest, split_rows
from eval.metrics import band_metrics, bootstrap, macro_f1, mcnemar_exact, paired_bootstrap_delta, qwk

NONE = "none"
COMPONENTS = {"tubule": "gt_tubule", "pleo": "gt_pleo", "mitoses": "gt_mitoses"}
# SPEC-00 §2.4: a headline metric is final only when its 95% CI half-width is at most this.
FINAL_HALF_WIDTH = 0.05
SLICE_COLUMNS = ("scanner", "native_mag")


class RunNotFinishedError(RuntimeError):
    """Metrics need every item to have ended."""


class RunsNotComparableError(ValueError):
    """Two runs must cover the same slides of the same dataset split."""


@dataclass(frozen=True)
class Case:
    """One item with its ground truth, prediction and slice keys."""

    slide_id: str
    patient_id: str
    gt: dict
    pred: dict  # grade, total, tubule, pleo, mitoses (None when there is no prediction)
    slices: dict


def _int_or_none(row: pd.Series, column: str):
    value = row[column]
    return None if pd.isna(value) else int(value)


def _slice_key(value) -> str:
    return "unknown" if value is None or pd.isna(value) else str(value)


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
        gt = {"grade": _int_or_none(row, "gt_grade"), "total": _int_or_none(row, "gt_total"),
              **{name: _int_or_none(row, column) for name, column in COMPONENTS.items()}}
        grading = (item.prediction or {}).get("grading") if item.status == "succeeded" else None
        pred = {key: None for key in ("grade", "total", *COMPONENTS)}
        if grading is not None:
            pred.update({key: grading.get(key) for key in pred})
        cases.append(Case(item.slide_id, item.patient_id, gt, pred,
                          {column: _slice_key(row[column]) for column in SLICE_COLUMNS}))
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


# --- metric functions over cases (all arithmetic in eval.metrics) ------------------


def class_f1(key: str) -> Callable[[list[Case]], float]:
    return lambda cases: macro_f1([c.gt[key] for c in cases], [_label(c.pred[key]) for c in cases]).macro


def _bands(cases: list[Case]):
    return band_metrics([c.gt["total"] for c in cases], [c.pred["total"] for c in cases],
                        [c.pred["grade"] for c in cases])


def grade_qwk(cases: list[Case]) -> float:
    return qwk([c.gt["grade"] for c in cases], [_label(c.pred["grade"]) for c in cases])


METRIC_FUNCTIONS: dict[str, Callable[[list[Case]], float]] = {
    "ns_g": class_f1("grade"),
    "f1_t": class_f1("tubule"),
    "f1_p": class_f1("pleo"),
    "f1_m": class_f1("mitoses"),
    "qwk": grade_qwk,
    "f1_high": lambda cs: _bands(cs).f1_high,
    "macro_f1_lm": lambda cs: _bands(cs).macro_f1_lm,
    "sum_mae": lambda cs: _bands(cs).sum_mae,
}


def metric(fn: Callable[[list[Case]], float], cases: list[Case], B: int, seed: int, *, valid: bool) -> Metric:
    if not cases:
        return Metric(value=None, ci_low=None, ci_high=None, n=0, status="invalid")
    ci = bootstrap(lambda units: fn(flat(units)), by_patient(cases), B=B, seed=seed)
    values = [ci.point, ci.low, ci.high]
    if not valid or any(v is None or np.isnan(v) for v in values):
        status = "invalid"
    else:
        status = "final" if (ci.high - ci.low) / 2 <= FINAL_HALF_WIDTH else "provisional"
    return Metric(value=ci.point, ci_low=ci.low, ci_high=ci.high, n=len(cases), status=status)


def no_data(reason: str, unavailable: dict[str, str], name: str) -> Metric:
    unavailable[name] = reason
    return Metric(value=None, ci_low=None, ci_high=None, n=0, status="invalid")


def confusion(key: str, cases: list[Case]) -> Confusion:
    labels: list = [1, 2, 3, NONE]
    index = {label: i for i, label in enumerate(labels)}
    matrix = [[0] * len(labels) for _ in labels]
    for c in cases:
        matrix[index[c.gt[key]]][index[_label(c.pred[key])]] += 1
    return Confusion(labels=labels, matrix=matrix)


def model_costs(session: Session, run_id) -> dict[str, ModelCost]:
    records = session.execute(
        select(DecisionRecord.producer_id, DecisionRecord.cost_usd, DecisionRecord.latency_ms)
        .where(DecisionRecord.run_id == run_id, DecisionRecord.producer_kind == ProducerKind.MODEL.value)
    ).all()
    grouped: dict[str, list] = defaultdict(list)
    for producer_id, cost, latency in records:
        grouped[producer_id].append((float(cost or 0), latency))
    return {
        producer: ModelCost(
            calls=len(rows), usd=sum(c for c, _ in rows),
            p50_ms=float(np.percentile([ms for _, ms in rows], 50)),
            p95_ms=float(np.percentile([ms for _, ms in rows], 95)),
        )
        for producer, rows in sorted(grouped.items())
    }


def compute_metrics(session: Session, run_id, *, B: int = 2000, seed: int = 7) -> MetricsDocument:
    run = session.get(ValidationRun, run_id)
    if run is None:
        raise LookupError(f"validation run {run_id} not found")
    items = session.scalars(select(ValidationItem).where(ValidationItem.run_id == run.id)).all()
    cases = run_cases(session, run)
    int_fall = len(session.scalars(
        select(DecisionRecord.id).where(
            DecisionRecord.run_id == run.id, DecisionRecord.producer_kind == ProducerKind.FALLBACK.value
        )
    ).all())
    valid = int_fall == 0
    unavailable: dict[str, str] = {
        "ns_m": "whole-slide runs have no annotated mitotic figures; NS-M comes from the mitosis_roi component harness",
        "s3": "tumour-tile F1 needs BCSS regions (the tumor_tiles component harness)",
        "s4": "detection metrics need annotated mitotic figures (the mitosis_roi component harness)",
    }

    graded = [c for c in cases if c.gt["grade"] is not None]
    headline = Headline()
    stages = StageMetrics()
    confusions = ConfusionSet()
    slices: list[SliceRow] = []
    if "grading" not in run.stages:
        unavailable["s5"] = "the run did not include grading"
        coverage = sum(1 for i in items if i.status == "succeeded") / max(len(cases), 1)
    elif not graded:
        unavailable["s5"] = "no item of the split has a ground-truth grade"
        coverage = sum(1 for c in cases if c.pred["grade"] is not None) / max(len(cases), 1)
    else:
        if len(graded) < len(cases):
            unavailable["ns_g_missing_truth"] = f"{len(cases) - len(graded)} items have no ground-truth grade and are left out"
        coverage = sum(1 for c in graded if c.pred["grade"] is not None) / len(graded)
        headline = Headline(ns_g=metric(METRIC_FUNCTIONS["ns_g"], graded, B, seed, valid=valid))
        with_total = [c for c in cases if c.gt["total"] is not None]
        s5 = {}
        for name, key in (("f1_t", "tubule"), ("f1_p", "pleo"), ("f1_m", "mitoses")):
            with_truth = [c for c in cases if c.gt[key] is not None]
            s5[name] = (metric(METRIC_FUNCTIONS[name], with_truth, B, seed, valid=valid) if with_truth
                        else no_data(f"no ground-truth {key} score", unavailable, name))
        for name in ("f1_high", "macro_f1_lm", "sum_mae"):
            s5[name] = (metric(METRIC_FUNCTIONS[name], with_total, B, seed, valid=valid) if with_total
                        else no_data("no ground-truth Nottingham sum", unavailable, name))
        s5["qwk"] = metric(grade_qwk, graded, B, seed, valid=valid)
        reason = "the manifest histotypes are not yet mapped to the SPEC-00 S5-HT classes (WP-8.4)"
        s5["histotype_f1"] = no_data(reason, unavailable, "histotype_f1")
        s5["ilc_f1"] = no_data(reason, unavailable, "ilc_f1")
        stages = StageMetrics(s5=S5Metrics(**s5))
        confusions = ConfusionSet(
            grade=confusion("grade", graded),
            **{name: confusion(key, [c for c in cases if c.gt[key] is not None])
               for name, key in (("tubule", "tubule"), ("pleo", "pleo"), ("mitotic", "mitoses"))
               if any(c.gt[key] is not None for c in cases)},
        )
        for column in SLICE_COLUMNS:
            groups: dict[str, list[Case]] = defaultdict(list)
            for c in graded:
                groups[c.slices[column]].append(c)
            for key, group in sorted(groups.items()):
                m = metric(METRIC_FUNCTIONS["ns_g"], group, B, seed, valid=valid)
                slices.append(SliceRow(slice=column, key=key, metric="ns_g", value=m.value,
                                       ci_low=m.ci_low, ci_high=m.ci_high, n=m.n))

    by_model = model_costs(session, run.id)
    total_usd = float(sum(float(i.cost_usd or 0) for i in items))
    failures = Counter((i.status, i.failed_stage, i.error_class) for i in items if i.status in ("failed", "excluded_qc"))
    return MetricsDocument(
        metrics_schema_version=1,
        run_id=str(run.id),
        generated_at=datetime.now(timezone.utc).isoformat(),
        bootstrap=Bootstrap(B=B, seed=seed),
        coverage=coverage,
        headline=headline,
        stages=stages,
        confusion=confusions,
        slices=slices,
        calibration=Calibration(),
        curves=Curves(),
        cost=Cost(usd_total=total_usd, usd_per_slide=total_usd / max(len(cases), 1), by_model=by_model),
        run=RunInfo(
            id=str(run.id), name=run.name, dataset=run.dataset, split=run.split, arm=run.arm,
            stages=list(run.stages), mode=run.mode, status=run.status, is_locked_test=run.is_locked_test,
            config_hash=run.config_hash, registry_sha256=run.registry_sha256, manifest_uri=run.manifest_uri,
            manifest_sha256=run.manifest_sha256, splits_lock_sha256=run.splits_lock_sha256,
        ),
        counts=Counts(
            items=dict(Counter(i.status for i in items)),
            no_invasive_tumor=sum(
                1 for i in items if i.status == "succeeded" and (i.prediction or {}).get("no_invasive_tumor")
            ),
            failures=[FailureGroup(status=s, stage=st, error_class=e, n=n)
                      for (s, st, e), n in sorted(failures.items(), key=lambda kv: (-kv[1], str(kv[0])))],
            int_fall=int_fall,
        ),
        unavailable=unavailable,
    )


# --- files -----------------------------------------------------------------------


def render_html(doc: MetricsDocument) -> str:
    """A static page rendered from metrics.json."""
    def fmt(m: Metric) -> str:
        if m.value is None:
            return f"n/a ({m.status})"
        return f"{m.value:.3f} [{m.ci_low:.3f}, {m.ci_high:.3f}] n={m.n} · {m.status}"

    rows = [("Coverage", f"{doc.coverage:.3f}")]
    if doc.headline.ns_g is not None:
        rows.append(("NS-G grade macro-F1", fmt(doc.headline.ns_g)))
    if doc.stages.s5 is not None:
        rows += [(name, fmt(value)) for name, value in doc.stages.s5]
    rows += [(f"NS-G · {s.slice} = {s.key}", f"{s.value:.3f} n={s.n}" if s.value is not None else "n/a")
             for s in doc.slices]
    if doc.counts is not None:
        rows.append(("No invasive tumour (graded 'none')", str(doc.counts.no_invasive_tumor)))
        rows.append(("Fallback decisions (INT-FALL, must be 0)", str(doc.counts.int_fall)))
        rows += [(f"Failed at {f.stage}: {f.error_class}" if f.status == "failed" else "Excluded by QC", str(f.n))
                 for f in doc.counts.failures]
    rows += [(f"Unavailable: {k}", v) for k, v in (doc.unavailable or {}).items()]
    rows.append(("Cost", f"${doc.cost.usd_total:.2f} (${doc.cost.usd_per_slide:.2f} per slide)"))
    run = doc.run
    title = html.escape(run.name if run else doc.run_id)
    header = ""
    if run is not None:
        items = ", ".join(f"{html.escape(k)} {v}" for k, v in sorted(doc.counts.items.items())) if doc.counts else ""
        header = (f"<p>{html.escape(run.dataset)} / {html.escape(run.split)}"
                  f"{' (locked test)' if run.is_locked_test else ''} · arm {html.escape(run.arm or '-')} · items: {items}</p>"
                  f"<p>Run <code>{html.escape(run.id)}</code> · config <code>{run.config_hash[:12]}</code> · "
                  f"registry <code>{run.registry_sha256[:12]}</code> · manifest <code>{run.manifest_sha256[:12]}</code></p>")
    body = "\n".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in rows)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Run {title}</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem;max-width:60rem}}table{{border-collapse:collapse}}
th,td{{text-align:left;padding:.3rem .8rem;border-bottom:1px solid #ddd}}code{{font-size:.85em}}</style></head>
<body><h1>{title}</h1>
{header}
<table>{body}</table>
<p>Intervals: 95% percentile bootstrap over patients, B={doc.bootstrap.B}, seed {doc.bootstrap.seed}. Generated {html.escape(doc.generated_at)}. Schema v{doc.metrics_schema_version}.</p>
</body></html>
"""


def write_report(doc: MetricsDocument, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = out_dir / "metrics.json"
    metrics_path.write_text(dump_metrics(doc), encoding="utf-8")
    page = out_dir / "report.html"
    page.write_text(render_html(doc), encoding="utf-8")
    return metrics_path, page


# --- compare ---------------------------------------------------------------------

COMPARE_METRICS = ("ns_g", "qwk", "f1_high", "macro_f1_lm", "sum_mae", "f1_t", "f1_p", "f1_m")
# An error metric: a negative delta is an improvement.
LOWER_IS_BETTER = ("sum_mae",)
FLIP_COMPONENTS = ("grade", *COMPONENTS)


def compare_runs(session: Session, run_a, run_b, metric_name: str, *, B: int = 2000, seed: int = 7) -> dict:
    """A contract ``CompareRow``: paired bootstrap of ``metric(b) - metric(a)`` over patients, and
    McNemar on per-slide grade correctness."""
    if metric_name not in COMPARE_METRICS:
        raise ValueError(f"metric must be one of {list(COMPARE_METRICS)}; ns_m needs the mitosis_roi harness")
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
    order = sorted(cases_a)
    list_a = [cases_a[s] for s in order]
    list_b = [cases_b[s] for s in order]
    units_a = by_patient(list_a)
    units_b = [[cases_b[c.slide_id] for c in unit] for unit in units_a]
    fn = METRIC_FUNCTIONS[metric_name]
    delta = paired_bootstrap_delta(lambda us: fn(flat(us)), units_a, units_b, B=B, seed=seed)
    return {
        "metric": metric_name,
        "a": metric(fn, list_a, B, seed, valid=True).model_dump(),
        "b": metric(fn, list_b, B, seed, valid=True).model_dump(),
        "delta": delta.delta, "delta_low": delta.low, "delta_high": delta.high,
        "mcnemar_p": mcnemar_exact([c.pred["grade"] == c.gt["grade"] for c in list_a],
                                   [c.pred["grade"] == c.gt["grade"] for c in list_b]),
        "run_a": str(a.id), "run_b": str(b.id), "n_slides": len(order), "B": B, "seed": seed,
    }


def compare_detail(session: Session, run_a, run_b, metric_name: str = "ns_g", *, B: int = 2000, seed: int = 7) -> dict:
    """Per-slice deltas of ``metric_name`` (paired bootstrap over patients) and the slides whose
    correctness flipped between the runs, over the graded slides both runs cover."""
    if metric_name not in COMPARE_METRICS:
        raise ValueError(f"metric must be one of {list(COMPARE_METRICS)}")
    runs = [session.get(ValidationRun, r) for r in (run_a, run_b)]
    if None in runs:
        raise LookupError("both runs must exist")
    a, b = runs
    if (a.dataset, a.split) != (b.dataset, b.split):
        raise RunsNotComparableError(f"{a.dataset}/{a.split} vs {b.dataset}/{b.split}")
    cases_a = {c.slide_id: c for c in run_cases(session, a) if c.gt["grade"] is not None}
    cases_b = {c.slide_id: c for c in run_cases(session, b) if c.gt["grade"] is not None}
    if set(cases_a) != set(cases_b):
        raise RunsNotComparableError(
            f"the runs cover different slides ({len(set(cases_a) ^ set(cases_b))} differ); pairing needs the same set"
        )
    fn = METRIC_FUNCTIONS[metric_name]
    slices = []
    for column in SLICE_COLUMNS:
        groups: dict[str, list[str]] = defaultdict(list)
        for slide_id, case in cases_a.items():
            groups[case.slices[column]].append(slide_id)
        for key, slide_ids in sorted(groups.items()):
            units_a = by_patient([cases_a[s] for s in sorted(slide_ids)])
            units_b = [[cases_b[c.slide_id] for c in unit] for unit in units_a]
            delta = paired_bootstrap_delta(lambda us: fn(flat(us)), units_a, units_b, B=B, seed=seed)
            slices.append({"slice": column, "key": key, "metric": metric_name,
                           "delta": delta.delta, "delta_low": delta.low, "delta_high": delta.high})
    flips = []
    for slide_id in sorted(cases_a):
        for component in FLIP_COMPONENTS:
            truth = cases_a[slide_id].gt[component]
            if truth is None:
                continue
            a_correct = cases_a[slide_id].pred[component] == truth
            b_correct = cases_b[slide_id].pred[component] == truth
            if a_correct != b_correct:
                flips.append({"slide_id": slide_id, "component": component,
                              "a_correct": a_correct, "b_correct": b_correct})
    return {"slices": slices, "flips": flips}


def dumps(value: dict) -> str:
    return json.dumps(value, indent=2, sort_keys=True)
