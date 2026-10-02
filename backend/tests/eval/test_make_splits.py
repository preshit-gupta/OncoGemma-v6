"""The committed TCGA/BCSS splits (SPEC-02 §5.2; WP-6.2, decision D20)."""
import pandas as pd
import pytest

from eval.make_splits import (
    MissingMagnificationError,
    bcss_frame,
    main,
    native_mag_from_description,
    short_barcode,
    tcga_split_frame,
)
from eval.splits import verify_lock


@pytest.mark.parametrize(
    "description, expected",
    [
        ("Aperio Image Library v10.2.41\r\n101184x84856 (240x240) JPEG/RGB Q=30|AppMag = 40|MPP = 0.2521", ("40", "appmag")),
        ("Aperio|AppMag = 20|MPP = 0.4990", ("20", "appmag")),
        ("Aperio|MPP = 0.2268", ("40", "mpp")),
        ("Aperio|MPP = 0.5040", ("20", "mpp")),
        ("Aperio Image Library v12.1.3 \r\n21965x22826 (256x256) J2K/KDU Q=70", ("unknown", "none")),
    ],
)
def test_native_magnification_from_the_slide_header(description, expected):
    assert native_mag_from_description(description) == expected


def test_an_mpp_that_is_neither_20x_nor_40x_raises():
    with pytest.raises(MissingMagnificationError):
        native_mag_from_description("Aperio|MPP = 0.36")


def test_short_barcode():
    assert short_barcode("TCGA-A1-A0SK-01Z-00-DX1.A44D70FA-4D96-43F4-9DD7-A61535786297.svs") == "TCGA-A1-A0SK-DX1"
    with pytest.raises(ValueError):
        short_barcode("TCGA-A1-A0SK-01A-01-TS1.svs")


def _dx(n_patients=40):
    rows = []
    for p in range(n_patients):
        tss = "A1" if p < 30 else chr(ord("B") + p - 30) + "X"  # one patient per small site -> "other"
        pid = f"TCGA-{tss}-{p:04d}"
        rows.append({"file_id": f"f{p}", "file_name": f"{pid}-01Z-00-DX1.UUID{p}.svs", "patient_id": pid, "tss": tss})
    return pd.DataFrame(rows)


def test_split_frame_has_the_strata_columns():
    dx = _dx()
    mags = pd.DataFrame({"file_id": dx["file_id"], "native_mag": "40", "mag_source": "appmag"})
    grades = pd.DataFrame({"patient_id": dx["patient_id"][:20], "grade": [1, 2, 3, 2] * 5}).astype({"grade": "Int64"})
    frame = tcga_split_frame(dx, mags, grades, min_tss=10)
    assert set(frame["gt_grade"]) == {"1", "2", "3", "none"}
    assert set(frame["tss_group"]) == {"A1", "other"}
    assert frame["slide_id"].iloc[0].endswith("-DX1")


def test_a_slide_without_magnification_raises():
    dx = _dx()
    mags = pd.DataFrame({"file_id": dx["file_id"][:-1], "native_mag": "40", "mag_source": "appmag"})
    with pytest.raises(MissingMagnificationError):
        tcga_split_frame(dx, mags, pd.DataFrame({"patient_id": [], "grade": []}), min_tss=10)


def test_make_writes_disjoint_splits_and_a_verifiable_lock(tmp_path):
    dx = _dx()
    dx.to_parquet(tmp_path / "dx.parquet")
    pd.DataFrame({"file_id": dx["file_id"], "native_mag": "40", "mag_source": "appmag"}).to_parquet(tmp_path / "mag.parquet")
    pd.DataFrame({"patient_id": dx["patient_id"], "grade": [2] * len(dx)}).astype({"grade": "Int64"}).to_parquet(tmp_path / "g.parquet")
    bcss_ids = [f"{pid}-DX1" for pid in dx["patient_id"][:5]] + ["TCGA-XX-9999-DX1"]
    pd.DataFrame({"slide": bcss_ids, "xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1}).set_index("slide").to_csv(tmp_path / "rois.csv")
    out = tmp_path / "splits"
    argv = ["make", "--dx", str(tmp_path / "dx.parquet"), "--native-mag", str(tmp_path / "mag.parquet"),
            "--grades", str(tmp_path / "g.parquet"), "--bcss-roi-bounds", str(tmp_path / "rois.csv"),
            "--out-dir", str(out), "--lock-root", str(tmp_path), "--seed", "7", "--exclude-bcss", "TCGA-XX-9999-DX1"]
    assert main(argv) == 0
    verify_lock(out / "SPLITS.lock", tmp_path)
    tcga = pd.read_parquet(out / "tcga_brca_dx.parquet")
    bcss = pd.read_parquet(out / "bcss.parquet")
    assert set(tcga["split"]) == {"train", "val", "test"} and (tcga["seed"] == 7).all()
    assert len(bcss) == 5
    split_of = tcga.set_index("patient_id")["split"]
    assert all(split_of[p] == s for p, s in zip(bcss["patient_id"], bcss["split"]))


def test_excluding_an_unknown_bcss_slide_raises(tmp_path):
    pd.DataFrame({"slide": ["TCGA-AA-0001-DX1"], "xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1}).set_index("slide").to_csv(tmp_path / "r.csv")
    with pytest.raises(KeyError):
        bcss_frame(tmp_path / "r.csv", frozenset({"TCGA-BB-0002-DX1"}))
