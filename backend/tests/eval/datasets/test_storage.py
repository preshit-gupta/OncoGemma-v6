"""
Tests for LocalStorage, GcsStorage, and storage factory.
SPEC-02 §3 and WP-5.2.
"""
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from eval.datasets.storage import GcsStorage, LocalStorage, storage_from_uri


def test_local_storage_write_read(tmp_path: Path):
    storage = LocalStorage(tmp_path)
    relpath = "sub/dir/test.txt"

    assert not storage.exists(relpath)
    assert storage.get_size(relpath) == 0

    with storage.open_write(relpath) as f:
        f.write(b"hello world")

    assert storage.exists(relpath)
    assert storage.get_size(relpath) == 11

    with storage.open_read(relpath) as f:
        content = f.read()
    assert content == b"hello world"

    # Test open_append
    with storage.open_append(relpath) as f:
        f.write(b" append")

    assert storage.get_size(relpath) == 18
    with storage.open_read(relpath) as f:
        assert f.read() == b"hello world append"

    uri = storage.uri(relpath)
    assert uri.startswith("file://")
    assert "test.txt" in uri


def test_gcs_storage_mocked():
    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()

    mock_client.bucket.return_value = mock_bucket
    mock_bucket.blob.return_value = mock_blob
    mock_blob.exists.return_value = True

    storage = GcsStorage(bucket_name="my-bucket", prefix="datasets/tcga", client=mock_client)

    assert storage.exists("slide1.svs") is True
    mock_bucket.blob.assert_called_with("datasets/tcga/slide1.svs")

    assert storage.uri("slide1.svs") == "gs://my-bucket/datasets/tcga/slide1.svs"

    storage.open_write("slide1.svs")
    mock_blob.open.assert_called_with("wb")

    storage.open_read("slide1.svs")
    mock_blob.open.assert_called_with("rb")


def test_storage_from_uri(tmp_path: Path):
    # gs:// URI
    gcs = storage_from_uri("gs://my-bucket/path/to/data")
    assert isinstance(gcs, GcsStorage)
    assert gcs.bucket_name == "my-bucket"
    assert gcs.prefix == "path/to/data"

    # local URI
    local1 = storage_from_uri(f"file:///{tmp_path.as_posix()}")
    assert isinstance(local1, LocalStorage)

    local2 = storage_from_uri(str(tmp_path))
    assert isinstance(local2, LocalStorage)
