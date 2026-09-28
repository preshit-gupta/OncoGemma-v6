"""
Acceptance tests for WP-5.3 (SPEC-02 §5.2): patient-level stratified splits, lock files, leakage checks.
Do NOT change assertions.
"""
import pandas as pd
import pytest

s = pytest.importorskip("eval.splits")


def _df(n_units, grade=2, tss="A1", mag=40, prefix="P", slides_per_unit=1):
    rows = []
    for i in range(n_units):
        for k in range(slides_per_unit):
            rows.append({"patient_id": f"{prefix}{i:03d}", "slide_id": f"{prefix}{i:03d}-S{k}",
                         "gt_grade": grade, "tss_group": tss, "native_mag": mag})
    return pd.DataFrame(rows)


def _counts(out, unit_col="patient_id"):
    per_unit = out.drop_duplicates(unit_col)["split"].value_counts().to_dict()
    return (per_unit.get("train", 0), per_unit.get("val", 0), per_unit.get("test", 0))


def test_exact_counts_ten_units():
    out = s.make_splits(_df(10), seed=1, min_stratum=5)
    assert _counts(out) == (6, 2, 2)


def test_largest_remainder_tie_breaks_in_split_order():
    # 7 units: 4.2 / 1.4 / 1.4 -> floors 4/1/1, remainder 1 -> tie val/test (.4) -> val (earlier) -> 4/2/1
    out = s.make_splits(_df(7), seed=1, min_stratum=5)
    assert _counts(out) == (4, 2, 1)


def test_per_stratum_allocation():
    df = pd.concat([_df(10, grade=1, prefix="A"), _df(10, grade=3, prefix="B")], ignore_index=True)
    out = s.make_splits(df, seed=3, min_stratum=5)
    for g in (1, 3):
        assert _counts(out[out.gt_grade == g]) == (6, 2, 2)


def test_small_strata_are_merged():
    # strata of 3 and 4 units (< min_stratum=5) merge into one 7-unit stratum -> 4/2/1 overall
    df = pd.concat([_df(3, tss="X", prefix="A"), _df(4, tss="Y", prefix="B")], ignore_index=True)
    out = s.make_splits(df, seed=5, min_stratum=5)
    assert _counts(out) == (4, 2, 1)


def test_all_slides_of_a_unit_share_split():
    out = s.make_splits(_df(20, slides_per_unit=3), seed=2, min_stratum=5)
    assert out.groupby("patient_id")["split"].nunique().max() == 1
    assert len(out) == 60


def test_deterministic_and_seed_sensitive():
    df = _df(40)
    a = s.make_splits(df, seed=11, min_stratum=5)
    b = s.make_splits(df, seed=11, min_stratum=5)
    c = s.make_splits(df, seed=12, min_stratum=5)
    assert a["split"].tolist() == b["split"].tolist()
    assert a["split"].tolist() != c["split"].tolist()


def test_input_order_does_not_matter():
    df = _df(30)
    a = s.make_splits(df, seed=9, min_stratum=5).set_index("slide_id")["split"]
    b = s.make_splits(df.sample(frac=1.0, random_state=0), seed=9, min_stratum=5).set_index("slide_id")["split"]
    assert a.sort_index().tolist() == b.sort_index().tolist()


def test_custom_ratios_must_sum_to_one():
    with pytest.raises(ValueError):
        s.make_splits(_df(10), seed=1, ratios=(0.5, 0.2, 0.2))


# ---- leakage ------------------------------------------------------------------------

def test_check_disjoint_passes_when_consistent():
    tcga = pd.DataFrame({"patient_id": ["P1", "P2"], "split": ["train", "test"]})
    bcss = pd.DataFrame({"patient_id": ["P1"], "split": ["train"]})
    s.check_disjoint({"tcga": tcga, "bcss": bcss})     # no exception


def test_check_disjoint_detects_cross_dataset_leak():
    tcga = pd.DataFrame({"patient_id": ["P1", "P2"], "split": ["train", "test"]})
    bcss = pd.DataFrame({"patient_id": ["P2"], "split": ["train"]})
    with pytest.raises(s.SplitLeakError) as ei:
        s.check_disjoint({"tcga": tcga, "bcss": bcss})
    assert "P2" in str(ei.value)


def test_check_disjoint_detects_within_dataset_leak():
    tcga = pd.DataFrame({"patient_id": ["P1", "P1"], "split": ["train", "val"]})
    with pytest.raises(s.SplitLeakError):
        s.check_disjoint({"tcga": tcga})


def test_inherit_splits():
    source = pd.DataFrame({"patient_id": ["P1", "P2"], "split": ["train", "test"]})
    target = pd.DataFrame({"patient_id": ["P2", "P1", "P2"], "roi": [1, 2, 3]})
    out = s.inherit_splits(target, source)
    assert out["split"].tolist() == ["test", "train", "test"]
    assert out["roi"].tolist() == [1, 2, 3]


def test_inherit_splits_missing_unit_raises():
    source = pd.DataFrame({"patient_id": ["P1"], "split": ["train"]})
    target = pd.DataFrame({"patient_id": ["P9"]})
    with pytest.raises(s.UnassignedUnitError) as ei:
        s.inherit_splits(target, source)
    assert "P9" in str(ei.value)


# ---- lock file ----------------------------------------------------------------------

def test_lock_roundtrip_and_tamper_detection(tmp_path):
    f1 = tmp_path / "splits" / "tcga.parquet"
    f1.parent.mkdir()
    s.make_splits(_df(10), seed=1, min_stratum=5).to_parquet(f1)
    lock = tmp_path / "splits" / "SPLITS.lock"
    s.write_lock([f1], lock, root=tmp_path)
    assert s.verify_lock(lock, root=tmp_path) is True

    df = pd.read_parquet(f1)
    df.loc[0, "split"] = "test" if df.loc[0, "split"] != "test" else "train"
    df.to_parquet(f1)
    with pytest.raises(s.LockMismatchError):
        s.verify_lock(lock, root=tmp_path)
