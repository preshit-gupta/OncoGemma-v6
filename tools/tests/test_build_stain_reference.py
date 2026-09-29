"""tools/build_stain_reference.py: the colour reference builder (SPEC-04 §3.3).

Run from the repo root:  python -m pytest tools/tests -q -p no:cacheprovider
"""
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
build = pytest.importorskip("build_stain_reference")

REFERENCE = REPO / "configs" / "stain_refs" / "v5_patch@v1.json"


def fit(slide_id: str, tss: str, h=(0.65, 0.70, 0.29), e=(0.07, 0.99, 0.11), maxc=(1.0, 0.8)) -> dict:
    return {"slide_id": slide_id, "tss": tss, "w_src": [list(h), list(e)], "maxc_src": list(maxc)}


def test_the_committed_v5_reference_is_what_the_builder_makes_of_the_v5_patch(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)  # the reference records the patch path as given
    out = tmp_path / "v5_patch@v1.json"
    assert build.main([
        "patch", "--patch", "configs/stain_reference.png", "--specimen", "resection",
        "--reference-id", "v5_patch@v1", "--out", str(out),
    ]) == 0
    made, committed = json.loads(out.read_text(encoding="utf-8")), json.loads(REFERENCE.read_text(encoding="utf-8"))
    assert made["source"] == committed["source"] and made["fitter_version"] == committed["fitter_version"]
    assert np.allclose(made["w_tgt"], committed["w_tgt"], atol=1e-9)
    assert np.allclose(made["maxc_tgt"], committed["maxc_tgt"], atol=1e-9)


def test_stratified_sampling_takes_from_every_site_before_repeating_one():
    records = [fit(f"a{i}", "A") for i in range(50)] + [fit("b0", "B"), fit("c0", "C"), fit("c1", "C")]
    chosen = build.stratified_sample(records, 5, seed=1)
    assert len(chosen) == 5
    assert {"b0", "c0"} <= {r["slide_id"] for r in chosen}  # the small sites are all in
    assert sum(r["tss"] == "A" for r in chosen) == 2


def test_stratified_sampling_is_seeded_and_bounded():
    records = [fit(f"{t}{i}", t) for t in "ABC" for i in range(20)]
    first = build.stratified_sample(records, 12, seed=3)
    assert first == build.stratified_sample(list(reversed(records)), 12, seed=3)
    assert first != build.stratified_sample(records, 12, seed=4)
    assert len(build.stratified_sample(records, 1000, seed=0)) == 60
    assert build.stratified_sample([], 5, seed=0) == []


def test_the_median_is_component_wise_and_renormalised():
    fits = [
        fit("s1", "A", maxc=(1.0, 0.5)),
        fit("s2", "A", maxc=(2.0, 0.7)),
        fit("s3", "B", maxc=(9.0, 0.9)),  # an outlier the median ignores
    ]
    reference = build.median_reference(fits, "median@v1", "test")
    assert reference.maxc_tgt == pytest.approx([2.0, 0.7])
    assert np.allclose(np.linalg.norm(reference.w_tgt, axis=1), 1.0)
    assert reference.n_slides == 3 and reference.reference_id == "median@v1"


def test_the_median_ignores_a_stain_vector_outlier():
    typical = [fit(f"s{i}", "A", h=(0.65 + 0.01 * i, 0.70, 0.29)) for i in range(4)]
    odd = fit("odd", "B", h=(0.2, 0.9, 0.4))
    reference = build.median_reference(typical + [odd], "median@v1", "test")
    assert reference.w_tgt[0][0] == pytest.approx(0.66, abs=0.02)


def test_the_slide_list_is_hashed_order_independently():
    fits = [fit("s1", "A"), fit("s2", "B")]
    one = build.median_reference(fits, "m@v1", "x").slide_ids_sha256
    assert one == build.median_reference(list(reversed(fits)), "m@v1", "x").slide_ids_sha256
    assert one != build.median_reference(fits[:1], "m@v1", "x").slide_ids_sha256


def test_a_median_of_nothing_is_refused():
    with pytest.raises(ValueError, match="no slide fits"):
        build.median_reference([], "m@v1", "x")


def test_the_median_command_writes_a_loadable_reference(tmp_path, capsys):
    fits = tmp_path / "fits.json"
    fits.write_text(json.dumps([fit(f"s{i}", "AB"[i % 2]) for i in range(10)]), encoding="utf-8")
    out = tmp_path / "refs" / "tcga_train_median@v1.json"
    assert build.main(["median", "--fits", str(fits), "--n", "6", "--reference-id", "tcga_train_median@v1", "--out", str(out)]) == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["n_slides"] == 6 and written["reference_id"] == "tcga_train_median@v1"
    assert written["slide_ids_sha256"] and "TSS-stratified" in written["source"]
    assert out.read_bytes().endswith(b"\n")


def test_the_median_command_warns_when_fewer_slides_exist(tmp_path, capsys):
    fits = tmp_path / "fits.json"
    fits.write_text(json.dumps([fit("s1", "A"), fit("s2", "B")]), encoding="utf-8")
    build.main(["median", "--fits", str(fits), "--reference-id", "m@v1", "--out", str(tmp_path / "m@v1.json")])
    assert "only 2 slides available" in capsys.readouterr().err
