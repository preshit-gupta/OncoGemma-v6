"""
The local GCS mock must hold objects whose local path exceeds Windows MAX_PATH (260 chars).

Regression: with a long TMP dir, the triage Path Foundation parquet cache
(cases/<id>/triage/pathfoundation_<probe version>.parquet) was never written, so
test_run_triage_stage_e2e re-invoked the endpoint on its second run.
"""
import os

from app.core import gcs


def test_local_gcs_round_trips_object_beyond_max_path(tmp_path, monkeypatch):
    monkeypatch.setattr(gcs, "_LOCAL_STORAGE_DIR", str(tmp_path / ("d" * 100)))
    bucket_name = "oncogemma-test-artifacts"
    blob_name = "cases/test_case_long_path/triage/" + "e" * 150 + ".parquet"
    assert len(os.path.join(gcs._LOCAL_STORAGE_DIR, bucket_name, *blob_name.split("/"))) > 260

    gcs.upload_blob_from_bytes(bucket_name, blob_name, b"cached-embeddings", "application/octet-stream")

    assert gcs.get_bucket(bucket_name).blob(blob_name).exists()
    assert gcs.download_blob_as_bytes(bucket_name, blob_name) == b"cached-embeddings"
    listed = gcs.get_bucket(bucket_name).list_blobs(prefix="cases/test_case_long_path/")
    assert [b.name for b in listed] == [blob_name]
