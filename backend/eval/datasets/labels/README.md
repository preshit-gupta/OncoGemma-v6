# TCGA-BRCA Nottingham grade labels (SPEC-02 §4, WP-5.4)

| File | Contents |
|---|---|
| `tcga_grade.parquet` | One row per TCGA-BRCA patient with a diagnostic slide (1,062): `grade, total, tubule, pleo, mitoses`, `label_source`, `label_confidence`, `report_sha256`, `text_source`, `excluded_reason` |
| `tcga_grade_qa_queue.parquet` | Patients for researcher review, with both extractions and their evidence |
| `tcga_grade_summary.json` | Counts, corpus version, model, prompt and config hash of the run |

**Status (2026-09-30): before QA.** 720 labels are accepted (`label_source = both`, `high`).
The 141 rows with `excluded_reason = qa_pending` have no label until their QA decision is applied.
Label accuracy is not measured yet. Use the labels for development only until
`apply-qa` reports `label_accuracy ≥ 0.95` (SPEC-02 §4 step 5).

## Sources
- Report text: TCGA-Reports v1 (Kefeli J, Tatonetti N; Mendeley Data, doi:10.17632/hyg5xkznpx.1,
  CC BY 4.0) for 999 patients; GDC report PDF text layer for 62; 1 needs OCR (excluded).
- LLM: `gemini_labeler` (gemini-2.5-flash, temperature 0), prompt `label_extract@v2.md`.

## Regenerate
From `backend/`, with `DATABASE_URL` set (the run never uses the database) and Google ADC:
```
python -m eval.labels.tcga_reports fetch-pdfs --patients dx.parquet --corpus TCGA_Reports.csv.zip --dest pdfs/
python -m eval.labels.tcga_reports extract --patients dx.parquet --corpus TCGA_Reports.csv.zip --pdf-dir pdfs/ --work work/ --out-dir out/
python -m eval.labels.tcga_reports apply-qa --labels tcga_grade.parquet --queue tcga_grade_qa_queue.parquet --decisions qa.csv --out tcga_grade.parquet
```
`dx.parquet` is `python -m eval.datasets tcga_brca_dx discover --out dx.parquet`. The gateway caches
every answer under `work/`, so a rerun with the same prompt makes no model calls.
`tcga_report_text.parquet` (the report text for the QA view, 1.7 MB) is written by `extract`
and is not kept in git.
