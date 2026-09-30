"""The harness's JSON documents and their schemas (SPEC-02 §5.5, §7; AC3, AC4).

``metrics.json`` and the ``one-shot`` result are built as these models, so every document the
harness writes is valid by construction. ``eval/schemas/*.schema.json`` are exported from them
(``python -m eval.harness.documents``); a test fails when a committed schema is stale.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

METRICS_SCHEMA_VERSION = 1
ONE_SHOT_SCHEMA_VERSION = 1
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Metric(Strict):
    """A metric with its bootstrap interval over patients (contract ``Metric``; SPEC-00 §2.4).

    ``final`` when the 95% CI half-width is at most 0.05, ``provisional`` otherwise, ``invalid``
    when there is no value or the run broke the measurement-validity gate (SPEC-00 §2.5).
    """

    value: float | None
    ci_low: float | None
    ci_high: float | None
    n: int
    status: Literal["final", "provisional", "invalid"]

    @field_validator("value", "ci_low", "ci_high", mode="before")
    @classmethod
    def _nan_is_none(cls, value):
        return None if isinstance(value, float) and math.isnan(value) else value


class Bootstrap(Strict):
    B: int
    seed: int


class Headline(Strict):
    ns_m: Metric | None = None
    ns_g: Metric | None = None


class S3Metrics(Strict):
    f1: Metric
    p_at_k: Metric
    coverage: float


class S4Metrics(Strict):
    f1: Metric
    precision: Metric
    recall: Metric
    ap: float | None
    count_mae_per_2mm2: Metric
    count_bias_per_2mm2: Metric
    per_scanner: dict[str, Metric]
    per_mag: dict[str, Metric]


class S5Metrics(Strict):
    f1_t: Metric
    f1_p: Metric
    f1_m: Metric
    f1_high: Metric
    macro_f1_lm: Metric
    sum_mae: Metric
    qwk: Metric
    histotype_f1: Metric
    ilc_f1: Metric


class StageMetrics(Strict):
    s3: S3Metrics | None = None
    s4: S4Metrics | None = None
    s5: S5Metrics | None = None


class Confusion(Strict):
    labels: list[Literal[1, 2, 3, "none"]]
    matrix: list[list[int]]  # rows = true, columns = predicted


class ConfusionSet(Strict):
    grade: Confusion | None = None
    tubule: Confusion | None = None
    pleo: Confusion | None = None
    mitotic: Confusion | None = None


class SliceRow(Strict):
    slice: str
    key: str
    metric: str
    value: float | None
    ci_low: float | None
    ci_high: float | None
    n: int

    @field_validator("value", "ci_low", "ci_high", mode="before")
    @classmethod
    def _nan_is_none(cls, value):
        return None if isinstance(value, float) and math.isnan(value) else value


class ReliabilityBin(Strict):
    p_mean: float
    frac_pos: float
    n: int


class Reliability(Strict):
    bins: list[ReliabilityBin]
    ece: float


class Calibration(Strict):
    p_a: Reliability | None = None
    p_b: Reliability | None = None
    p_tumor: Reliability | None = None


class PRCurveDoc(Strict):
    thresholds: list[float]
    precision: list[float]
    recall: list[float]
    f1: list[float]


class Curves(Strict):
    mitosis_pr: PRCurveDoc | None = None


class ModelCost(Strict):
    calls: int
    usd: float
    p50_ms: float
    p95_ms: float


class Cost(Strict):
    usd_total: float
    usd_per_slide: float
    by_model: dict[str, ModelCost]


# --- additions to the contract (optional fields, docs/contracts/research_v1.md) ---------


class RunInfo(Strict):
    id: str
    name: str
    dataset: str
    split: str
    arm: str | None
    stages: list[str]
    mode: str
    status: str
    is_locked_test: bool
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_uri: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    splits_lock_sha256: str | None


class FailureGroup(Strict):
    status: Literal["failed", "excluded_qc"]
    stage: str | None
    error_class: str | None
    n: int


class Counts(Strict):
    items: dict[str, int]  # by item status
    # Succeeded items whose triage found no invasive tumour. They stay in every denominator as a
    # 'none' grade (SPEC-00 rule 2); this count says how much of the missing coverage they explain.
    no_invasive_tumor: int
    failures: list[FailureGroup]
    int_fall: int  # fallback decision records in the run (SPEC-00 §2.3; must be 0)


class MetricsDocument(Strict):
    """``reports/<run_id>/metrics.json``: the contract's ``MetricsV1`` (docs/contracts/research_v1.md).

    Optional members are omitted when the run cannot support them; ``unavailable`` says why.
    Serialise with ``dump_metrics`` so that omitted members are left out rather than written as null.
    """

    metrics_schema_version: Literal[1]
    run_id: str
    generated_at: str
    bootstrap: Bootstrap
    coverage: float
    headline: Headline
    stages: StageMetrics
    confusion: ConfusionSet
    slices: list[SliceRow]
    calibration: Calibration
    curves: Curves
    cost: Cost
    run: RunInfo | None = None
    counts: Counts | None = None
    unavailable: dict[str, str] | None = None


def dump_metrics(doc: MetricsDocument) -> str:
    """JSON with optional members left out (``exclude_defaults``); required nulls are kept."""
    return doc.model_dump_json(indent=2, exclude_defaults=True)


class StageResult(Strict):
    stage: str
    attempt: int
    status: str
    output_ref: str | None
    model_versions: dict | None
    config_hash: str | None
    started_at: str | None
    completed_at: str | None
    latency_s: float | None
    error: dict | None


class Decision(Strict):
    """One DecisionRecord of the slide (the chain behind each candidate, patch and score)."""

    id: str
    stage: str
    task: str
    entity_type: str
    entity_id: str
    producer_kind: str
    producer_id: str
    producer_version: str
    status: str
    error_class: str | None
    output: dict | None
    latency_ms: int
    cost_usd: float | None
    cache_hit: bool
    supersedes_id: str | None


class OneShotResult(Strict):
    schema_version: Literal[1] = ONE_SHOT_SCHEMA_VERSION
    slide_uri: str
    specimen_type: Literal["resection", "core_biopsy"]
    run_id: str
    case_id: str
    status: Literal["succeeded", "failed", "excluded_qc"]
    failed_stage: str | None
    error_class: str | None
    error_detail: str | None
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_versions: dict[str, str]
    stages: list[StageResult]
    prediction: dict | None
    hotspots: list[dict]
    candidates: list[dict]
    hpfs: list[dict]
    patches: list[dict]
    decisions: list[Decision]
    cost_usd: float
    runtime_s: float | None


SCHEMAS = {"metrics": MetricsDocument, "one_shot": OneShotResult}


def schema_text(name: str) -> str:
    return json.dumps(SCHEMAS[name].model_json_schema(), indent=2, sort_keys=True) + "\n"


def write_schemas(directory: Path = SCHEMA_DIR) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in SCHEMAS:
        path = directory / f"{name}.schema.json"
        path.write_text(schema_text(name), encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


if __name__ == "__main__":
    for written in write_schemas():
        print(written, file=sys.stdout)
