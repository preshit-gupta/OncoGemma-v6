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


class Interval(Strict):
    """A bootstrap percentile interval over patients (SPEC-00 §2.4). ``None`` when undefined (no cases)."""

    point: float | None
    low: float | None
    high: float | None
    B: int
    seed: int

    @field_validator("point", "low", "high", mode="before")
    @classmethod
    def _nan_is_none(cls, value):
        return None if isinstance(value, float) and math.isnan(value) else value


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


class ClassMetrics(Strict):
    """Macro-F1 over grades 1–3 with a ``none`` prediction for every case without one (SPEC-00 §2.5)."""

    n: int
    coverage: float
    macro_f1: Interval
    per_class: dict[str, float]


class GradeMetrics(ClassMetrics):
    qwk: Interval
    f1_high: Interval
    macro_f1_lm: Interval
    sum_mae: Interval
    n_high: int
    n_lm: int


class MetricsDocument(Strict):
    metrics_schema_version: Literal[1] = METRICS_SCHEMA_VERSION
    run: RunInfo
    items: dict[str, int]
    failures: list[FailureGroup]
    bootstrap_unit: Literal["patient"] = "patient"
    grade: GradeMetrics | None
    components: dict[Literal["tubule", "pleo", "mitoses"], ClassMetrics]
    # Metrics the run's ground truth cannot support, with the reason (never silently dropped).
    unavailable: dict[str, str]
    cost_usd: float
    runtime_s: dict[str, float | None]


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
