"""Nottingham grade labels from TCGA-BRCA pathology reports (SPEC-02 §4, WP-5.4).

Steps, per patient:
1. Text. The TCGA-Reports corpus (Kefeli et al., CC BY 4.0) replaces OCR where it has the
   patient; otherwise the report PDF's text layer (pypdf). A PDF with fewer than
   ``MIN_LAYER_CHARS_PER_PAGE`` characters per page needs OCR, which is not run here: the
   patient is excluded with ``needs_ocr``.
2. Deterministic extraction: ``report_grammar``.
3. Structured LLM extraction through the gateway (``Task.LABEL_EXTRACT``, temperature 0), the
   report in its own delimited part. A field whose quote is not in the report is nulled.
4. Reconciliation: accepted when both agree and are consistent; otherwise QA.
5. QA: every disagreement plus a stratified random sample of agreements. ``apply_qa`` folds
   the researcher's decisions back in and reports label accuracy.
6. Output: one row per patient (``OUTPUT_COLUMNS``).

Command line (from ``backend/``)::

    python -m eval.labels.tcga_reports extract --patients dx.parquet --corpus TCGA_Reports.csv.zip \
        [--pdf-dir reports/] --work work/ --out-dir eval/datasets/labels/
    python -m eval.labels.tcga_reports apply-qa --labels tcga_grade.parquet --queue tcga_grade_qa_queue.parquet \
        --decisions qa.csv --out tcga_grade.parquet
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import numpy as np
import pandas as pd

from app.core.run_context import DecisionContext, RunMode
from app.core.tasks import EntityType, Task
from app.inference.errors import GatewayError
from app.inference.gateway import EntityRef, ModelGateway, ModelInputs
from app.inference.schemas import ReportGradeExtraction
from eval.labels import report_grammar
from eval.labels.report_grammar import COMPONENTS, FIELDS, ConflictingValues

# TCGA-Reports v1: Kefeli J, Tatonetti N. Mendeley Data, doi:10.17632/hyg5xkznpx.1, CC BY 4.0.
# The zip in github.com/tatonetti-lab/tcga-path-reports, checked 2026-09-30: 9,523 reports,
# columns patient_filename ("<patient>.<GDC report UUID>") and text; 1,034 are TCGA-BRCA.
CORPUS_VERSION = "TCGA-Reports v1 (doi:10.17632/hyg5xkznpx.1)"
CORPUS_ZIP_SHA256 = "bbbc0ee0c75d8feb0bb246b4fa40e8e29bba9775d4a9e4f41e0b73a7955e1c12"
CORPUS_MEMBER = "TCGA_Reports.csv"

MIN_LAYER_CHARS_PER_PAGE = 200            # SPEC-02 §4 step 1: below this, the PDF needs OCR
PRODUCER_ID = "gemini_labeler"
PROMPT_ID = "label_extract@v2.md"  # v1 inferred scores from descriptions and quoted bare digits
QA_AGREEMENT_FRACTION = 0.10              # SPEC-02 §4 step 5
QA_AGREEMENT_MINIMUM = 30
LABEL_ACCURACY_GATE = 0.95                # below this, extraction is reworked before P2
GRADE_BANDS = {1: range(3, 6), 2: range(6, 8), 3: range(8, 10)}  # Nottingham total -> grade

OUTPUT_COLUMNS = [
    "patient_id", "grade", "total", "tubule", "pleo", "mitoses",
    "label_source", "label_confidence", "report_sha256", "text_source", "excluded_reason",
]
QA_COLUMNS = [
    "patient_id", "qa_reason", "report_sha256", "text_source",
    *[f"regex_{f}" for f in FIELDS], *[f"llm_{f}" for f in FIELDS],
    "regex_evidence", "llm_evidence", "llm_error",
]


class ReportTextError(ValueError):
    """A report source is missing, altered or unreadable."""


class QADecisionError(ValueError):
    """A QA decision is incomplete or contradicts the Nottingham rules."""


# --- 1. text ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportText:
    patient_id: str
    text: str
    text_source: str        # corpus | layer
    source_ref: str         # corpus version, or the PDF file name

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def load_corpus(zip_path: Path, expected_sha256: str = CORPUS_ZIP_SHA256) -> dict[str, ReportText]:
    """Report text by patient from the TCGA-Reports zip, after checking the zip's SHA-256."""
    digest = hashlib.sha256(Path(zip_path).read_bytes()).hexdigest()
    if digest != expected_sha256:
        raise ReportTextError(f"{zip_path}: SHA-256 {digest} is not the checked {CORPUS_VERSION} ({expected_sha256})")
    with zipfile.ZipFile(zip_path) as archive, archive.open(CORPUS_MEMBER) as member:
        frame = pd.read_csv(member)
    reports: dict[str, ReportText] = {}
    for filename, text in zip(frame["patient_filename"], frame["text"]):
        patient_id = str(filename)[:12]
        if patient_id in reports:
            raise ReportTextError(f"{CORPUS_VERSION} has two reports for {patient_id}")
        reports[patient_id] = ReportText(patient_id, str(text), "corpus", f"{CORPUS_VERSION}:{filename}")
    return reports


def pdf_text_layer(pdf_path: Path) -> tuple[str, int]:
    """(text, page count) of a PDF's text layer."""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    # pypdf gives None for a page without a text layer (a scanned page): it has no characters,
    # which is what the per-page threshold in report_from_pdf measures.
    pages = [page.extract_text() for page in reader.pages]
    return "\n".join(text for text in pages if text is not None), len(pages)


def report_from_pdfs(patient_id: str, pdf_paths: list[Path]) -> ReportText | None:
    """The text layer of a patient's report PDFs, in name order, or None when it is too thin
    to use without OCR. A patient with two reports is read as one document, so two
    different grades show up as a conflict rather than one report being picked."""
    texts, pages = [], 0
    for path in sorted(pdf_paths):
        text, count = pdf_text_layer(path)
        texts.append(text)
        pages += count
    text = "\n\n".join(texts)
    if pages == 0 or len(text.strip()) < MIN_LAYER_CHARS_PER_PAGE * pages:
        return None
    return ReportText(patient_id, text, "layer", ";".join(p.name for p in sorted(pdf_paths)))


def pdfs_by_patient(pdf_dir: Path) -> dict[str, list[Path]]:
    """GDC report PDFs (``TCGA-XX-YYYY.<uuid>.PDF``) keyed by patient."""
    found: dict[str, list[Path]] = {}
    for path in sorted(Path(pdf_dir).iterdir()):
        if path.suffix.lower() == ".pdf":
            found.setdefault(path.name[:12], []).append(path)
    return found


def download_report_pdfs(reports: pd.DataFrame, dest_dir: Path, client, api_url: str) -> list[Path]:
    """Stream GDC report PDFs (``discover_reports`` rows) into ``dest_dir``, checking each MD5.

    A file already present with the right MD5 is kept. A mismatch raises FetchIntegrityError
    and leaves no file behind.
    """
    from eval.datasets.base import FetchIntegrityError

    dest_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for _, row in reports.iterrows():
        target = dest_dir / str(row["file_name"])
        expected = str(row["md5sum"]).lower()
        if target.is_file() and hashlib.md5(target.read_bytes()).hexdigest() == expected:
            written.append(target)
            continue
        partial = target.with_suffix(target.suffix + ".part")
        md5 = hashlib.md5()
        with client.stream("GET", f"{api_url.rstrip('/')}/data/{row['file_id']}") as response:
            response.raise_for_status()
            with open(partial, "wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
                    md5.update(chunk)
        if md5.hexdigest() != expected:
            partial.unlink()
            raise FetchIntegrityError(f"{row['file_name']}: MD5 {md5.hexdigest()} != GDC {expected}")
        partial.replace(target)
        written.append(target)
    return written


# --- 2 and 3. extraction ------------------------------------------------------------------------


@dataclass(frozen=True)
class Extraction:
    """One extractor's reading of a report. ``conflicts`` lists fields stated with two values."""

    values: dict[str, int | None]
    evidence: tuple[dict[str, Any], ...] = ()
    conflicts: tuple[str, ...] = ()
    derived: tuple[str, ...] = ()

    def as_row(self, prefix: str) -> dict[str, Any]:
        return {f"{prefix}_{f}": self.values.get(f) for f in FIELDS}


def regex_extraction(text: str) -> Extraction:
    result = report_grammar.extract(text)
    values: dict[str, int | None] = {}
    conflicts = []
    for name in FIELDS:
        try:
            values[name] = result.value(name)
        except ConflictingValues:
            values[name] = None
            conflicts.append(name)
    evidence = tuple(
        {"field": c.field, "value": c.value, "start": c.start, "end": c.end, "quote": c.text} for c in result.captures
    )
    return Extraction(values, evidence, tuple(conflicts))


def _collapse(text: str) -> str:
    return " ".join(text.split())


def verify_quotes(output: ReportGradeExtraction, text: str) -> Extraction:
    """Null every field without a quote that is a substring of the report (whitespace collapsed).

    A quote without a letter ("3", "3+2+1") is no evidence: it names no field, and a bare
    number is a substring of almost any report.
    """
    report = _collapse(text)
    values: dict[str, int | None] = {}
    kept: list[dict[str, Any]] = []
    for name in FIELDS:
        value = getattr(output, name)
        quotes = [e.quote for e in output.evidence if e.field == name]
        verified = [q for q in quotes if any(ch.isalpha() for ch in q) and _collapse(q) in report]
        values[name] = value if value is not None and verified else None
        kept += [
            {"field": name, "value": value, "quote": q, "verified": q in verified}
            for q in quotes
        ]
    return Extraction(values, tuple(kept))


def llm_extraction(gateway: ModelGateway, report: ReportText, config_hash: str) -> Extraction:
    ctx = DecisionContext(
        case_id=uuid.uuid5(uuid.NAMESPACE_URL, f"tcga-brca-report/{report.patient_id}"),
        stage_execution_id=uuid.uuid4(),
        stage="grading",  # the stage whose ground truth this is; no stage execution runs here
        run_mode=RunMode.EVAL,
        run_id=None,
        config_hash=config_hash,
    )
    result = gateway.invoke(
        Task.LABEL_EXTRACT,
        PRODUCER_ID,
        ModelInputs(prompt_id=PROMPT_ID, untrusted_document=report.text),
        ctx,
        EntityRef(EntityType.REPORT, report.patient_id),
        ReportGradeExtraction,
    )
    return verify_quotes(result.output, report.text)


# --- 4. reconciliation --------------------------------------------------------------------------


def grade_of_total(total: int) -> int:
    return next(grade for grade, band in GRADE_BANDS.items() if total in band)


def inconsistencies(values: Mapping[str, int | None]) -> list[str]:
    """The SPEC-02 §4 step 4 checks that fail for ``values``."""
    problems = []
    components = [values.get(c) for c in COMPONENTS]
    total, grade = values.get("total"), values.get("grade")
    if total is not None and None not in components and total != sum(components):
        problems.append(f"total {total} != tubule + pleo + mitoses = {sum(components)}")
    if grade is not None and total is not None and grade != grade_of_total(total):
        problems.append(f"grade {grade} does not match total {total}")
    return problems


def complete(extraction: Extraction) -> Extraction:
    """Fill ``total`` from all three components and ``grade`` from ``total``, when not stated."""
    values = dict(extraction.values)
    derived = []
    components = [values[c] for c in COMPONENTS]
    if values["total"] is None and None not in components:
        values["total"] = sum(components)
        derived.append("total")
    if values["grade"] is None and values["total"] is not None:
        values["grade"] = grade_of_total(values["total"])
        derived.append("grade")
    return Extraction(values, extraction.evidence, extraction.conflicts, tuple(derived))


@dataclass
class PatientResult:
    patient_id: str
    report: ReportText | None
    excluded_reason: str | None = None
    regex: Extraction | None = None
    llm: Extraction | None = None
    llm_error: str | None = None
    qa_reason: str | None = None          # set when the patient must be reviewed
    label: dict[str, int | None] = field(default_factory=lambda: dict.fromkeys(FIELDS))
    agreed: bool = False


def reconcile(result: PatientResult) -> PatientResult:
    """Decide ``result`` from its two extractions (SPEC-02 §4 step 4)."""
    regex, llm = result.regex, result.llm
    if "grade" in regex.conflicts:
        result.excluded_reason = "multiple_grades_in_report"
        result.qa_reason = "multiple_grades"
        return result
    if llm is None:
        result.excluded_reason = "qa_pending"
        result.qa_reason = "llm_failed"
        return result
    if regex.conflicts:
        result.excluded_reason = "qa_pending"
        result.qa_reason = "regex_conflict"
        return result
    problems = inconsistencies(regex.values) + inconsistencies(llm.values)
    if problems:
        result.excluded_reason = "qa_pending"
        result.qa_reason = "inconsistent"
        return result
    # Agreement is on the grade and on every field both state. A field only one extractor
    # states (e.g. components written only as "NHG3 (3+3+3)", which the LLM cannot quote with
    # words) is not a disagreement, but it is not part of the label either.
    r, m = complete(regex).values, complete(llm).values
    clash = any(r[f] is not None and m[f] is not None and r[f] != m[f] for f in FIELDS)
    if clash or (r["grade"] is None) != (m["grade"] is None):
        result.excluded_reason = "qa_pending"
        result.qa_reason = "disagreement"
        return result
    result.agreed = True
    result.label = {f: r[f] if r[f] == m[f] else None for f in FIELDS}
    if result.label["grade"] is None:
        result.excluded_reason = "no_grade_in_report"
    return result


# --- 5. QA sampling --------------------------------------------------------------------------------


def sample_agreements(results: Iterable[PatientResult], seed: int) -> set[str]:
    """Patients of a stratified random sample (by grade, including no grade) of the agreements."""
    agreed = sorted((r for r in results if r.agreed), key=lambda r: r.patient_id)
    if not agreed:
        return set()
    target = min(len(agreed), max(QA_AGREEMENT_MINIMUM, math.ceil(QA_AGREEMENT_FRACTION * len(agreed))))
    strata: dict[int | None, list[str]] = {}
    for r in agreed:
        strata.setdefault(r.label["grade"], []).append(r.patient_id)
    rng = np.random.default_rng(seed)
    chosen: set[str] = set()
    # Proportional allocation, at least one per stratum, largest remainders first.
    shares = {k: target * len(v) / len(agreed) for k, v in strata.items()}
    counts = {k: max(1, math.floor(s)) for k, s in shares.items()}
    for k in sorted(strata, key=lambda k: shares[k] - math.floor(shares[k]), reverse=True):
        if sum(counts.values()) >= target:
            break
        if counts[k] < len(strata[k]):
            counts[k] += 1
    for k, members in strata.items():
        chosen.update(rng.choice(members, size=min(counts[k], len(members)), replace=False).tolist())
    return chosen


# --- the run ------------------------------------------------------------------------------------------


def run_extraction(
    patients: Iterable[str],
    corpus: Mapping[str, ReportText],
    pdfs: Mapping[str, list[Path]],
    llm: Callable[[ReportText], Extraction],
    *,
    workers: int = 1,
) -> list[PatientResult]:
    """Text, both extractions and reconciliation for every patient, in patient order."""

    def one(patient_id: str) -> PatientResult:
        report = corpus.get(patient_id)
        if report is None and patient_id in pdfs:
            report = report_from_pdfs(patient_id, pdfs[patient_id])
            if report is None:
                return PatientResult(patient_id, None, excluded_reason="needs_ocr")
        if report is None:
            return PatientResult(patient_id, None, excluded_reason="no_report_text")
        result = PatientResult(patient_id, report, regex=regex_extraction(report.text))
        try:
            result.llm = llm(report)
        except GatewayError as error:
            # Recorded by the gateway; the patient goes to QA with the error, never a guessed label.
            result.llm_error = f"{type(error).__name__}: {error.detail}"
        return reconcile(result)

    ordered = sorted(set(patients))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, ordered))


def label_frame(results: Iterable[PatientResult]) -> pd.DataFrame:
    rows = []
    for r in results:
        accepted = r.agreed and r.label["grade"] is not None
        rows.append({
            "patient_id": r.patient_id,
            **{f: r.label[f] if accepted else None for f in FIELDS},
            "label_source": "both" if accepted else None,
            "label_confidence": "high" if accepted else None,
            "report_sha256": None if r.report is None else r.report.sha256,
            "text_source": None if r.report is None else r.report.text_source,
            "excluded_reason": None if accepted else r.excluded_reason,
        })
    frame = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    return frame.astype({f: "Int64" for f in FIELDS})


def qa_queue(results: Iterable[PatientResult], sampled: set[str]) -> pd.DataFrame:
    rows = []
    for r in results:
        reason = r.qa_reason or ("sampled_agreement" if r.patient_id in sampled else None)
        if reason is None:
            continue
        rows.append({
            "patient_id": r.patient_id,
            "qa_reason": reason,
            "report_sha256": r.report.sha256,
            "text_source": r.report.text_source,
            **r.regex.as_row("regex"),
            **(r.llm.as_row("llm") if r.llm else {f"llm_{f}": None for f in FIELDS}),
            "regex_evidence": json.dumps(list(r.regex.evidence)),
            "llm_evidence": json.dumps(list(r.llm.evidence) if r.llm else []),
            "llm_error": r.llm_error,
        })
    frame = pd.DataFrame(rows, columns=QA_COLUMNS)
    return frame.astype({c: "Int64" for c in QA_COLUMNS if c.startswith(("regex_", "llm_")) and c.split("_", 1)[1] in FIELDS})


def text_frame(results: Iterable[PatientResult]) -> pd.DataFrame:
    """The report text each label came from, for the QA view (SPEC-08 §4.5)."""
    rows = [
        {"patient_id": r.patient_id, "report_sha256": r.report.sha256, "text_source": r.report.text_source,
         "source_ref": r.report.source_ref, "text": r.report.text}
        for r in results if r.report is not None
    ]
    return pd.DataFrame(rows, columns=["patient_id", "report_sha256", "text_source", "source_ref", "text"])


# --- 5 (cont.). QA decisions ---------------------------------------------------------------------------


def apply_qa(labels: pd.DataFrame, queue: pd.DataFrame, decisions: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Fold QA decisions into the labels and measure label accuracy.

    ``decisions`` has one row per reviewed patient: ``patient_id, action`` (accept | edit |
    exclude), the five fields (for edit) and ``reason`` (required for edit and exclude).
    ``accept`` is only meaningful for a sampled agreement: a disagreement has no single
    automated label to accept, so it needs ``edit`` or ``exclude``.
    """
    queued = queue.set_index("patient_id")
    unknown = sorted(set(decisions["patient_id"]) - set(queued.index))
    if unknown:
        raise QADecisionError(f"decisions for patients not in the QA queue: {unknown[:5]}")
    if decisions["patient_id"].duplicated().any():
        raise QADecisionError("more than one decision for a patient")

    out = labels.set_index("patient_id").copy()
    agreement_reviews = agreement_accepts = 0
    for _, d in decisions.iterrows():
        pid, action = d["patient_id"], d["action"]
        reason = d.get("reason")
        is_agreement = queued.loc[pid, "qa_reason"] == "sampled_agreement"
        if action not in ("accept", "edit", "exclude"):
            raise QADecisionError(f"{pid}: unknown action {action!r}")
        if action != "accept" and (reason is None or pd.isna(reason) or not str(reason).strip()):
            raise QADecisionError(f"{pid}: {action} needs a reason")
        if is_agreement:
            agreement_reviews += 1
            agreement_accepts += action == "accept"
        if action == "accept":
            if not is_agreement:
                raise QADecisionError(f"{pid}: a {queued.loc[pid, 'qa_reason']} item needs edit or exclude, not accept")
            continue  # the automated label stands (label_source both, confidence high)
        if action == "exclude":
            out.loc[pid, list(FIELDS)] = pd.NA
            out.loc[pid, ["label_source", "label_confidence"]] = None
            out.loc[pid, "excluded_reason"] = f"qa: {str(reason).strip()}"
            continue
        values = {f: None if pd.isna(d.get(f)) else int(d.get(f)) for f in FIELDS}
        if values["grade"] not in (1, 2, 3):
            raise QADecisionError(f"{pid}: an edit must give the grade (1, 2 or 3)")
        problems = inconsistencies(values)
        if problems:
            raise QADecisionError(f"{pid}: {'; '.join(problems)}")
        for f in FIELDS:
            out.loc[pid, f] = pd.NA if values[f] is None else values[f]
        out.loc[pid, ["label_source", "label_confidence", "excluded_reason"]] = ["qa", "medium", None]

    accuracy = agreement_accepts / agreement_reviews if agreement_reviews else None
    report = {
        "agreements_reviewed": agreement_reviews,
        "agreements_accepted": agreement_accepts,
        "label_accuracy": accuracy,
        "gate": LABEL_ACCURACY_GATE,
        "rework_required": accuracy is None or accuracy < LABEL_ACCURACY_GATE,
        "pending": sorted(set(queued.index) - set(decisions["patient_id"])),
    }
    return out.reset_index()[OUTPUT_COLUMNS], report


# --- command line --------------------------------------------------------------------------------------------


def _gateway(work: Path):
    from app.core.pipeline_config import get_config_hash, init_pipeline_config
    from app.inference.adapters.registry import production_adapters
    from app.inference.blobs import LocalBlobStore
    from app.inference.records import DecisionLog

    log = DecisionLog()
    gateway = ModelGateway(init_pipeline_config(), production_adapters(), log, LocalBlobStore(work / "blobs"))
    return gateway, log, get_config_hash()


def _write_records(log, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in log.pending():
            f.write(json.dumps(row, default=str) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.labels.tcga_reports", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("extract", help="extract, reconcile and queue for QA")
    ex.add_argument("--patients", required=True, type=Path, help="parquet with patient_id (tcga_brca_dx discover)")
    ex.add_argument("--corpus", required=True, type=Path, help="TCGA_Reports.csv.zip")
    ex.add_argument("--pdf-dir", type=Path, help="GDC report PDFs for patients the corpus lacks")
    ex.add_argument("--work", required=True, type=Path, help="gateway cache and decision records")
    ex.add_argument("--out-dir", required=True, type=Path)
    ex.add_argument("--workers", type=int, default=4)
    ex.add_argument("--seed", type=int, default=0)
    fp = sub.add_parser("fetch-pdfs", help="download GDC report PDFs for patients the corpus lacks")
    fp.add_argument("--patients", required=True, type=Path, help="parquet with patient_id (tcga_brca_dx discover)")
    fp.add_argument("--corpus", required=True, type=Path, help="TCGA_Reports.csv.zip")
    fp.add_argument("--dest", required=True, type=Path)
    qa = sub.add_parser("apply-qa", help="fold QA decisions into the labels")
    qa.add_argument("--labels", required=True, type=Path)
    qa.add_argument("--queue", required=True, type=Path)
    qa.add_argument("--decisions", required=True, type=Path, help="csv or parquet")
    qa.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.command == "extract":
        patients = pd.read_parquet(args.patients)["patient_id"].astype(str).tolist()
        corpus = load_corpus(args.corpus)
        pdfs = pdfs_by_patient(args.pdf_dir) if args.pdf_dir else {}
        args.work.mkdir(parents=True, exist_ok=True)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        gateway, log, config_hash = _gateway(args.work)
        results = run_extraction(
            patients, corpus, pdfs, lambda report: llm_extraction(gateway, report, config_hash), workers=args.workers
        )
        _write_records(log, args.work / "decision_records.jsonl")
        sampled = sample_agreements(results, args.seed)
        labels = label_frame(results)
        queue = qa_queue(results, sampled)
        labels.to_parquet(args.out_dir / "tcga_grade.parquet", index=False)
        queue.to_parquet(args.out_dir / "tcga_grade_qa_queue.parquet", index=False)
        text_frame(results).to_parquet(args.out_dir / "tcga_report_text.parquet", index=False)
        summary = {
            "corpus": CORPUS_VERSION,
            "producer": PRODUCER_ID,
            "model": gateway.registry.version_of(PRODUCER_ID),
            "prompt": PROMPT_ID,
            "config_hash": config_hash,
            "patients": len(results),
            "accepted": int(labels["label_source"].notna().sum()),
            "excluded": labels["excluded_reason"].value_counts().to_dict(),
            "qa_queue": queue["qa_reason"].value_counts().to_dict(),
            "seed": args.seed,
        }
        (args.out_dir / "tcga_grade_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
        return 0

    if args.command == "fetch-pdfs":
        from eval.datasets.tcga import TCGABRCAAdapter

        patients = set(pd.read_parquet(args.patients)["patient_id"].astype(str))
        missing = patients - set(load_corpus(args.corpus))
        adapter = TCGABRCAAdapter()
        reports = adapter.discover_reports()
        wanted = reports[reports["patient_id"].isin(missing)]
        paths = download_report_pdfs(wanted, args.dest, adapter.client, adapter.api_url)
        without = sorted(missing - set(wanted["patient_id"]))
        print(json.dumps({"patients_without_corpus_text": len(missing), "pdfs": len(paths),
                          "patients_without_any_report": without}, indent=2))
        return 0

    decisions = pd.read_csv(args.decisions) if args.decisions.suffix == ".csv" else pd.read_parquet(args.decisions)
    labels, report = apply_qa(pd.read_parquet(args.labels), pd.read_parquet(args.queue), decisions)
    labels.to_parquet(args.out, index=False)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
