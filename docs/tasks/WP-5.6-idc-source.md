# WP-5.6 — TCGA-BRCA slides read in place from IDC DICOM

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Claude | M | SPEC-02 §3.1, §5.1–5.3, §6.2 | WP-5.5 (merged) | Claude (`backend/app/core`, `backend/worker`, `backend/eval/**`, `requirements/**`) |

## Goal

Let the harness run whole TCGA-BRCA slides without copying them into our buckets, so WP-8.7 can validate the grading baseline on the locked val split.

**Owner decisions:**
- 2026-09-30: no bulk copy of TCGA into our buckets.
- 2026-10-04: use NCI Imaging Data Commons (IDC) DICOM.

## Verified facts (2026-10-04)

- IDC hosts TCGA-BRCA as DICOM WSI series in `gs://idc-open-data/<crdc_series_uuid>/`, one `.dcm` per pyramid level, label or overview.
- The bucket is public and not requester-pays: anonymous listing works.
- `ContainerIdentifier` is the slide barcode, which is the GDC file name up to the first dot.
- idc-index v24 has exactly one series for each of the 1,133 locked TCGA-BRCA DX slides.
- Every object carries `md5Hash`, and `google-cloud-storage` checks it on download (`checksum="auto"`).
- OpenSlide 4 (`openslide-bin`) opens a series from any one of its files. On a real TCGA series it reports `vendor=dicom`, MPP 0.2485 and 40×, and `read_region_at_mpp` reads through it.

## Files

- `backend/app/core/slide_source.py` (new): `download_slide`, `slide_size_bytes`, `series_sha256` and `local_slide_sha256`. A URI ending in `/` is a series.
- Every slide download goes through it: ingest, preprocess, QC, triage, mitosis, grading, `core/slide_access.py`, the scratch check and one-shot. `resolve_slide_raw_uri` returns a series URI as is. The GCS fake reports `md5_hash`.
- `backend/eval/datasets/idc.py` (new), `eval/datasets/idc/tcga_brca_dx_series.parquet` (barcode to series map, IDC v24) and `eval/manifests/tcga_brca_idc_val.parquet`.
- `requirements/worker.in`, `backend/requirements.txt` and the worker and dev lockfiles: `openslide-bin`, plus `openslide-python>=1.4`, which loads it instead of the apt library.

## Design

- **Series fingerprint:** the SHA-256 of `<instance> <md5>\n` lines from a listing, instances sorted by name.
  - The manifest records it before any download.
  - Ingest recomputes it and refuses a mismatch.
  - The storage client has already checked the downloaded bytes against those MD5s.
  - Hashing about 1.2 TB locally is not needed.
- **Read in place:** a series is never de-identified or rewritten, and never copied into the clinical slide cache.
- **Scratch check:** sums the sizes of the series' instances.

## Acceptance

```powershell
python -m pytest backend/tests/test_slide_source.py backend/tests/eval/datasets/test_idc.py -q -p no:cacheprovider
python -m pytest backend/tests -q -p no:cacheprovider
```

## Out of scope

- The WP-8.7 live run.
- Building train or test manifests. The test split stays locked; `python -m eval.datasets.idc manifest --split test` exists but is not run.
- DICOM upload through the app.
- Removing the apt OpenSlide from the images.
