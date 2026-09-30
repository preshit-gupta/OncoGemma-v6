"""metrics, compare and one-shot (SPEC-02 §5.5, §7; AC3, AC4)."""
import json

import pytest
from sqlalchemy import select

from app.core.gcs import upload_blob_from_bytes
from app.models.validation import ValidationRun
from eval import cli
from eval.harness.documents import SCHEMA_DIR, SCHEMAS, MetricsDocument, OneShotResult, schema_text
from eval.harness.one_shot import one_shot
from eval.harness.report import RunNotFinishedError, RunsNotComparableError, compare_runs, compute_metrics
from eval.harness.runs import create_run
from tests.fakes.gateway import make_gateway
from tests.harness import fake_pipeline
from tests.harness.test_controller import (  # noqa: F401  (fixtures)
    ALL_STAGES,
    Session,
    db,
    drive,
    lock,
    manifest_row,
    request,
    write_manifest,
)

FIVE = [("s1", "ok"), ("s2", "qcfail"), ("s3", "boom"), ("s4", "notumour"), ("s5", "ok")]


def finished_run(db, tmp_path, lock, **values):
    manifest = write_manifest(tmp_path, [manifest_row(s, kind) for s, kind in FIVE])
    run = create_run(db, request(manifest, lock, **values))
    drive(db, run.id)
    return run


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_committed_schemas_match_the_models(name):
    committed = (SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8")
    assert committed == schema_text(name), "run `python -m eval.harness.documents` in backend/"


# --- metrics (AC4) -----------------------------------------------------------------


def test_every_item_counts_and_missing_predictions_are_none(db, tmp_path, lock):
    run = finished_run(db, tmp_path, lock)
    doc = compute_metrics(db, run.id, B=200, seed=7)

    # s1, s5 predict grade 2; s2 (QC), s3 (failed), s4 (no tumour) have no grade: 'none'.
    ns_g = doc.headline.ns_g
    assert ns_g.n == 5 and doc.coverage == pytest.approx(0.4)
    assert ns_g.value == pytest.approx(4 / 7)  # class 2: tp 2, fn 3
    assert ns_g.ci_low <= ns_g.value <= ns_g.ci_high and ns_g.status in ("final", "provisional")
    assert doc.confusion.grade.labels == [1, 2, 3, "none"]
    assert doc.confusion.grade.matrix[1] == [0, 2, 0, 3]  # true G2: 2 predicted G2, 3 none
    assert doc.counts.items == {"succeeded": 3, "excluded_qc": 1, "failed": 1}
    assert doc.counts.no_invasive_tumor == 1  # s4: counted, and still a 'none' grade in the denominator
    assert doc.counts.int_fall == 0
    groups = {(f.status, f.stage, f.error_class): f.n for f in doc.counts.failures}
    assert groups == {("failed", "triage", "RuntimeError"): 1, ("excluded_qc", "qc", "QcHardFail"): 1}
    s5 = doc.stages.s5
    assert s5.f1_t.n == 5 and s5.sum_mae.n == 5
    assert s5.histotype_f1.status == "invalid" and "histotype_f1" in doc.unavailable
    assert doc.headline.ns_m is None and "ns_m" in doc.unavailable
    assert {(r.slice, r.key) for r in doc.slices} == {("scanner", "unknown"), ("native_mag", "unknown")}


def test_bootstrap_is_deterministic_under_a_seed(db, tmp_path, lock):
    run = finished_run(db, tmp_path, lock)
    a = compute_metrics(db, run.id, B=100, seed=11).model_dump(exclude={"generated_at"})
    b = compute_metrics(db, run.id, B=100, seed=11).model_dump(exclude={"generated_at"})
    assert a == b


def test_metrics_cli_writes_the_contract_document_and_links_it(db, tmp_path, lock):
    run = finished_run(db, tmp_path, lock)
    out = tmp_path / "reports"
    assert cli.main(["metrics", "--run", str(run.id), "--bootstrap", "50", "--out-dir", str(out)],
                    session_factory=Session) == 0
    metrics_path = out / str(run.id) / "metrics.json"
    text = metrics_path.read_text(encoding="utf-8")
    doc = MetricsDocument.model_validate_json(text)
    assert doc.metrics_schema_version == 1 and doc.run_id == str(run.id)
    raw = json.loads(text)
    # MetricsV1: optional members are omitted, not null; required members are all present.
    assert set(raw) >= {"metrics_schema_version", "run_id", "generated_at", "bootstrap", "coverage", "headline",
                        "stages", "confusion", "slices", "calibration", "curves", "cost"}
    assert "ns_m" not in raw["headline"] and set(raw["stages"]) == {"s5"} and raw["calibration"] == {}
    assert raw["bootstrap"] == {"B": 50, "seed": 7}
    page = (out / str(run.id) / "report.html").read_text(encoding="utf-8")
    assert "NS-G grade macro-F1" in page and "No invasive tumour" in page
    db.expire_all()
    assert db.get(ValidationRun, run.id).metrics_uri == str(metrics_path)


def test_metrics_wait_for_the_run_to_finish(db, tmp_path, lock):
    manifest = write_manifest(tmp_path, [manifest_row("s1", "ok")])
    run = create_run(db, request(manifest, lock))
    with pytest.raises(RunNotFinishedError):
        compute_metrics(db, run.id)
    assert cli.main(["metrics", "--run", str(run.id)], session_factory=Session) == 1


def test_a_run_without_grading_reports_s5_as_unavailable(db, tmp_path, lock):
    run = finished_run(db, tmp_path, lock, stages=ALL_STAGES[:4])
    doc = compute_metrics(db, run.id, B=20)
    assert doc.stages.s5 is None and doc.headline.ns_g is None
    assert doc.unavailable["s5"] == "the run did not include grading"


# --- compare ---------------------------------------------------------------------


def test_compare_pairs_the_same_slides(db, tmp_path, lock):
    a = finished_run(db, tmp_path, lock, name="a")
    fake_pipeline.BOOM["on"] = False
    manifest = str(tmp_path / "manifest.parquet")
    b = create_run(db, request(manifest, lock, name="b"))
    drive(db, b.id)

    row = compare_runs(db, a.id, b.id, "ns_g", B=200, seed=3)

    assert row["a"]["value"] == pytest.approx(4 / 7) and row["b"]["value"] == pytest.approx(6 / 8)
    assert row["delta"] == pytest.approx(6 / 8 - 4 / 7) and row["delta_low"] <= row["delta_high"]
    assert row["n_slides"] == 5 and row["mcnemar_p"] == 1.0  # one discordant slide
    with pytest.raises(ValueError):
        compare_runs(db, a.id, b.id, "ns_m")


def test_compare_refuses_different_slides(db, tmp_path, lock):
    a = finished_run(db, tmp_path, lock)
    other = tmp_path / "other"
    other.mkdir()
    manifest = write_manifest(other, [manifest_row("s1", "ok")])
    b = create_run(db, request(manifest, lock))
    drive(db, b.id)
    with pytest.raises(RunsNotComparableError):
        compare_runs(db, a.id, b.id, "ns_g")


# --- one-shot (AC3) ----------------------------------------------------------------


def put_slide(name: str) -> str:
    upload_blob_from_bytes("og-datasets", f"one-shot/{name}", b"not really a slide", "application/octet-stream")
    return f"gs://og-datasets/one-shot/{name}"


def fake_gateway(config, log):
    return make_gateway(config, {}, log=log)


def test_one_shot_writes_a_schema_valid_document(db, tmp_path):
    out = tmp_path / "result.json"
    code, result = one_shot(db, put_slide("ok-x1.svs"), "resection", out, actor="cli:test", mpp=0.25,
                            handlers=fake_pipeline.HANDLERS, gateway_factory=fake_gateway)
    assert code == 0 and result.status == "succeeded"
    doc = OneShotResult.model_validate_json(out.read_text(encoding="utf-8"))
    assert [s.stage for s in doc.stages] == list(ALL_STAGES)
    assert doc.prediction["grading"]["grade"] == 2 and len(doc.candidates) == 3 and len(doc.hotspots) == 1
    assert doc.model_versions and len(doc.config_hash) == 64
    run = db.get(ValidationRun, doc.run_id)
    assert (run.dataset, run.split, run.splits_lock_sha256) == ("adhoc", "adhoc", None)


def test_one_shot_exits_2_when_a_stage_fails(db, tmp_path):
    out = tmp_path / "result.json"
    code, result = one_shot(db, put_slide("boom-x2.svs"), "resection", out, actor="cli:test",
                            handlers=fake_pipeline.HANDLERS, gateway_factory=fake_gateway)
    assert code == 2
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert (doc["status"], doc["failed_stage"], doc["error_class"]) == ("failed", "triage", "RuntimeError")
    triage = next(s for s in doc["stages"] if s["stage"] == "triage")
    assert triage["error"]["class"] == "RuntimeError"


def test_one_shot_needs_a_gs_uri(db, tmp_path):
    with pytest.raises(ValueError, match="gs://"):
        one_shot(db, "/local/slide.svs", "resection", tmp_path / "r.json", actor="cli:test")
    assert db.scalars(select(ValidationRun)).first() is None
