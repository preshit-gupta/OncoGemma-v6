"""Slides from one GCS object or from a DICOM WSI series under a prefix (IDC; app/core/slide_source.py)."""
import base64
import hashlib
import os
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.gcs import resolve_slide_raw_uri, upload_blob_from_bytes
from app.core.slide_source import (
    SERIES_DIRNAME,
    SlideSourceMissingError,
    download_slide,
    is_series_uri,
    local_slide_sha256,
    series_sha256,
    slide_size_bytes,
)
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from tests.fakes.runtime import make_runtime
from worker.scratch import SlideObjectMissing, check_scratch

BUCKET = "idc-open-data"
INSTANCES = {"b-level0.dcm": b"level zero" * 100, "a-label.dcm": b"label", "c-level1.dcm": b"level one" * 10}


def seed_series(uid=None, instances=INSTANCES, extra=None):
    uid = uid or str(uuid.uuid4())
    for name, data in {**instances, **(extra or {})}.items():
        upload_blob_from_bytes(BUCKET, f"{uid}/{name}", data, "application/dicom")
    return f"gs://{BUCKET}/{uid}/"


def expected_fingerprint(instances):
    lines = "".join(f"{n} {base64.b64encode(hashlib.md5(instances[n]).digest()).decode()}\n" for n in sorted(instances))
    return hashlib.sha256(lines.encode()).hexdigest()


def test_a_trailing_slash_marks_a_series():
    assert is_series_uri("gs://idc-open-data/abc/")
    assert not is_series_uri("gs://raw/cases/x/slide.svs")
    assert not is_series_uri("/local/dir/")


def test_a_single_object_downloads_as_before(tmp_path):
    upload_blob_from_bytes("raw", "cases/c1/s.svs", b"svs bytes", "application/octet-stream")
    path = download_slide("gs://raw/cases/c1/s.svs", str(tmp_path))
    assert path == os.path.join(str(tmp_path), "slide.svs") and open(path, "rb").read() == b"svs bytes"
    assert slide_size_bytes("gs://raw/cases/c1/s.svs") == 9
    assert local_slide_sha256(path, "gs://raw/cases/c1/s.svs") == hashlib.sha256(b"svs bytes").hexdigest()
    with pytest.raises(SlideSourceMissingError):
        slide_size_bytes("gs://raw/cases/c1/missing.svs")


def test_a_series_downloads_every_instance_into_one_directory(tmp_path):
    uri = seed_series(extra={"notes.txt": b"not an instance"})
    path = download_slide(uri, str(tmp_path))
    series_dir = os.path.join(str(tmp_path), SERIES_DIRNAME)
    assert os.path.dirname(path) == series_dir and os.path.basename(path) == "a-label.dcm"
    assert sorted(os.listdir(series_dir)) == sorted(INSTANCES)
    assert slide_size_bytes(uri) == sum(len(v) for v in INSTANCES.values())


def test_the_series_fingerprint_follows_the_stored_md5s(tmp_path):
    uri = seed_series()
    fp = series_sha256(uri)
    assert fp == expected_fingerprint(INSTANCES)
    assert local_slide_sha256(download_slide(uri, str(tmp_path)), uri) == fp
    changed = {**INSTANCES, "c-level1.dcm": b"tampered"}
    assert series_sha256(seed_series(instances=changed)) != fp
    fewer = {k: v for k, v in INSTANCES.items() if k != "a-label.dcm"}
    assert series_sha256(seed_series(instances=fewer)) != fp


def test_a_series_needs_instances_and_their_md5(tmp_path):
    with pytest.raises(SlideSourceMissingError, match="no DICOM instance"):
        download_slide(f"gs://{BUCKET}/{uuid.uuid4()}/", str(tmp_path))
    blob = SimpleNamespace(name="x/a.dcm", md5_hash=None, size=1)
    bucket = MagicMock()
    bucket.list_blobs.return_value = [blob]
    with pytest.raises(SlideSourceMissingError, match="no stored MD5"):
        series_sha256(f"gs://{BUCKET}/x/", bucket)


def test_a_series_uri_is_its_own_raw_slide():
    uri = seed_series()
    assert resolve_slide_raw_uri("any-case", SimpleNamespace(gcs_uri_original=uri)) == uri


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def seed_case(db, uri, checksum=None):
    case_id, slide_id = uuid.uuid4(), uuid.uuid4()
    stage = StageExecution(case_id=case_id, stage="ingest", attempt=1, status="running", run_mode="eval",
                           input_ref={"slide_id": str(slide_id), "gcs_uri_original": uri})
    db.add_all([Case(id=case_id, created_by="t", status="open"),
                Slide(id=slide_id, case_id=case_id, gcs_uri_original=uri, checksum_sha256=checksum), stage])
    db.commit()
    return stage


def test_the_scratch_check_sums_a_series(db_session):
    uri = seed_series()
    stage = seed_case(db_session, uri)
    check = check_scratch(db_session, stage, 2.0)
    assert check.needed_bytes == 2 * sum(len(v) for v in INSTANCES.values())
    with pytest.raises(SlideObjectMissing):
        check_scratch(db_session, seed_case(db_session, f"gs://{BUCKET}/{uuid.uuid4()}/"), 2.0)


def test_ingest_refuses_a_series_whose_fingerprint_changed(db_session):
    from worker.ingest import run_ingest

    uri = seed_series()
    stage = seed_case(db_session, uri, checksum="0" * 63 + "1")
    with pytest.raises(ValueError, match="integrity verification failed"):
        run_ingest(stage, db_session, make_runtime(stage))


def test_ingest_records_the_series_fingerprint_and_never_rewrites_it(db_session):
    from worker.ingest import run_ingest

    uri = seed_series()
    stage = seed_case(db_session, uri, checksum=expected_fingerprint(INSTANCES))
    slide = MagicMock()
    slide.dimensions = (2048, 1024)
    slide.properties = {"openslide.vendor": "dicom", "openslide.mpp-x": "0.2485", "openslide.mpp-y": "0.2485",
                        "openslide.objective-power": "40"}
    with patch("openslide.OpenSlide", return_value=slide) as opened, \
         patch("worker.ingest.generate_dzi_pyramid", return_value="pyramid.dzi"), \
         patch("worker.ingest.upload_dzi_tree_to_gcs"), \
         patch("worker.ingest.upload_blob_from_file") as rewrite:
        run_ingest(stage, db_session, make_runtime(stage))
    row = db_session.get(Slide, uuid.UUID(stage.input_ref["slide_id"]))
    assert row.checksum_sha256 == expected_fingerprint(INSTANCES)
    assert row.mpp_x == pytest.approx(0.2485) and row.format == "dcm" and row.status == "ready"
    assert os.path.basename(opened.call_args_list[0].args[0]) == "a-label.dcm"
    rewrite.assert_not_called()  # an IDC series is read in place, never de-identified or rewritten
