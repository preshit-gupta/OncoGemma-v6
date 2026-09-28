# WP-5.2 — Dataset adapters and manifests (critical path)

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate + Claude review | M | SPEC-02 §3, §5.1; SPEC-00 §3–3.1 | WP-5.3 merged (shares `backend/eval/`) | C |

## Goal

Write the adapters that discover and fetch the benchmark datasets and emit **validated manifests** plus ground-truth files. Claude's WPs 5.4, 6.2, 7.3 and 7.4 consume these outputs, so correctness of the IDs, units and splits matters more than breadth.

## Read first (only these)

- `docs/specs/02-validation-harness-and-batch.md` §3 and §5.1
- `docs/specs/00-program-overview.md` §3 and §3.1
- `AGENTS.md`

## Files you may touch

- **Create:**
  - `backend/eval/datasets/__init__.py`, `__main__.py`, `base.py`, `manifest.py`, `storage.py`
  - `backend/eval/datasets/tcga.py`, `midogpp.py`, `bcss.py`, `bcnb.py`
  - `backend/eval/datasets/registry.yaml`, `backend/eval/datasets/config.yaml`
- **Tests:** `backend/tests/eval/datasets/__init__.py`, `test_*.py`, and small fixtures under `backend/tests/eval/datasets/fixtures/`
- `backend/requirements-dev.txt`: add only if needed

## Interfaces

```python
# base.py
class DatasetConfigMissing(Exception): ...      # message lists the missing keys
class FetchIntegrityError(Exception): ...

class DatasetAdapter(Protocol):
    key: str
    def discover(self) -> pd.DataFrame: ...                      # raw rows (no download)
    def fetch(self, row: pd.Series, dest: "Storage") -> FetchedFile: ...
    def labels(self) -> pd.DataFrame: ...                        # ground truth keyed by (patient_id, slide_id)
    def to_manifest(self, discovered: pd.DataFrame, fetched: dict[str, FetchedFile]) -> pd.DataFrame: ...

@dataclass(frozen=True)
class FetchedFile:
    slide_id: str; uri: str; sha256: str; md5: str | None; size_bytes: int

# storage.py
class Storage(Protocol):
    def open_write(self, relpath: str) -> BinaryIO: ...          # streaming
    def exists(self, relpath: str) -> bool: ...
    def uri(self, relpath: str) -> str: ...                      # file:///... or gs://bucket/...
class LocalStorage(Storage): ...                                 # for tests and dev
class GcsStorage(Storage): ...                                   # google-cloud-storage blob.open("wb"); never loads a file into memory

# manifest.py
MANIFEST_COLUMNS: dict[str, str]     # name -> dtype, exactly SPEC-02 §5.1 (split column may be null before WP-5.3 runs)
def validate_manifest(df: pd.DataFrame) -> None                  # raises ValueError listing every problem
```

## Tasks

1. **`registry.yaml`.** One entry per SPEC-00 §3 dataset key, with the fields `name, source_url, license_ref, license_scope (commercial_ok|research|pending), adapter, version_or_date`. BCNB's scope is `pending`.
2. **TCGA (`tcga.py`):**
   - **Discovery** via GDC `POST /files`, with the filters from SPEC-02 §3.1, paginated with `size` and `from`. Keep only file names containing `-DX`.
   - **Columns:** `file_id, file_name, md5sum, file_size, submitter_id` → `patient_id` = the first 12 characters of the barcode (`TCGA-XX-YYYY`), and `tss` = characters 6–7.
   - **Pathology reports** via a second query with `data_type = "Pathology Report"`.
   - **Download:** streaming `GET /data/{file_id}` in 8 MiB chunks. Verify the MD5 against GDC's value and compute SHA-256 while streaming. On mismatch, raise `FetchIntegrityError`. Support resume with `Range` when the partial file exists.
   - `fetch_many(rows, dest, confirm_bytes: int)` raises unless `confirm_bytes ≥` the sum of `file_size`.
   - **`slide_meta`:** after download, read `openslide` properties for mpp (`openslide.mpp-x`) and `aperio.AppMag`. If OpenSlide is not available in tests, inject a reader.
   - **Tests** use **recorded JSON responses** in fixtures (hand-write 3 hits including one `-TS1` to be filtered) and a local HTTP stub, e.g. `pytest-httpserver` or monkeypatched `httpx`. **No live network in tests.**
3. **MIDOG++ (`midogpp.py`):**
   - Parse the MS-COCO JSON: images, and annotations with categories for mitotic figure / non-mitotic figure. **Verify** the exact category names in the dataset release and record them in `config.yaml`.
   - Convert bounding-box centres to points, then pixel coordinates to µm using the per-image scanner mpp. The mpp map by scanner lives in `config.yaml`: Hamamatsu XR/S360 = 0.23 and Leica CS2 = 0.25, **verify**.
   - Emit a manifest (one row per ROI image: `dataset=midogpp_breast` or `midogpp_other` by tumour type, `specimen_type=resection`, `mpp_source=dataset_doc`).
   - Write one ground-truth GeoJSON per image (`regions_uri`), containing points with `class ∈ {MF, imposter}`.
   - **Test** with a 2-image COCO fixture.
4. **BCSS (`bcss.py`):**
   - Parse the mask file names and metadata to get the TCGA slide barcode, the ROI offset and the mask scale. **Verify** the naming convention from the BCSS repository, and keep the class-code map in `config.yaml`.
   - Output ROI rows `(patient_id, slide_barcode, roi_bbox_um, mask_uri, mask_mpp)`.
   - **Test** with a tiny synthetic mask and file name.
5. **BCNB (`bcnb.py`):**
   - Implement the configuration contract in SPEC-02 §3.2 exactly. Every key is required, and missing keys raise `DatasetConfigMissing`.
   - `convert_to_pyramidal_tiff(jpg_path, out_path, mpp)` uses pyvips with the exact `tiffsave` arguments in SPEC-02 §3.2. Tests skip it when `pyvips` is not importable (`pytest.importorskip`).
   - Grade mapping goes through `grade_map`; unmapped values produce an exclusion row with `excluded_reason`.
6. **Manifest validation.** Every adapter's `to_manifest` output passes `validate_manifest`, which is tested with good and bad frames.
7. **`python -m eval.datasets <key> discover --out <parquet>` and `... fetch --manifest <parquet> --dest <uri> --confirm-bytes N`.** A thin argparse wrapper; no business logic in it.

## Acceptance (run these)

```powershell
python -m pytest backend/tests/eval -q -p no:cacheprovider
python -m pytest backend/tests -q -p no:cacheprovider
```

Manual check, with the output in the PR: run TCGA `discover` against the live GDC API once, and report the row count and total bytes. Do not download.

## Out of scope — do not do

- Label extraction from reports (WP-5.4, Claude).
- Generating real splits (Claude, after reviewing this WP).
- Downloading datasets.

## Done checklist

- [ ] Adapters for TCGA, MIDOG++, BCSS and BCNB (parameterised) with offline tests
- [ ] Streaming download with MD5 + SHA-256 and resume; `confirm_bytes` guard
- [ ] `validate_manifest` enforced; registry and config files
- [ ] Live TCGA discovery count reported; every "verify" item in this card is answered in the PR
