"""Nottingham grammar (SPEC-02 §4 step 2) on report phrasings seen in TCGA-BRCA reports.

The strings are short phrases in the styles of the TCGA-Reports corpus (capitals, ". " at old
line breaks, synoptic "Field: value" lines), not copies of any report.
"""
import pytest

from eval.labels.report_grammar import ConflictingValues, extract


def values(text: str) -> dict[str, int | None]:
    result = extract(text)
    return {f: result.value(f) for f in ("grade", "total", "tubule", "pleo", "mitoses")}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("- NOTTINGHAM GRADE: 2 OF 3 (MODERATELY DIFFERENTIATED). - NOTTINGHAM SCORE: 7 OF 9. (Tubules=3 , Nuclei= 2, Mitoses= 2;",
         {"grade": 2, "total": 7, "tubule": 3, "pleo": 2, "mitoses": 2}),
        ("Histological grade = 2/3 (score: tubules 3 + nucleus 2 + mitoses 1 =. 6/9) by. criteria.",
         {"grade": 2, "total": 6, "tubule": 3, "pleo": 2, "mitoses": 1}),
        ("Histologic grade: 3. Overall grade: 9/9. Architectural score: 3. Nuclear score: 3. Mitotic score: 3.",
         {"grade": 3, "total": 9, "tubule": 3, "pleo": 3, "mitoses": 3}),
        ("Glandular (Acinar)/Tubular Differentiation: Score 3. Nuclear Pleomorphism: Score 3. Mitotic count: Score 1. Overall Grade: Grade 2: Score of 6 or 7.",
         {"grade": 2, "total": None, "tubule": 3, "pleo": 3, "mitoses": 1}),
        ("Histologic Grade (Nottingham Histologic Score): II/III. Tubule Formation: 3. Nuclear Grade: 2. Mitotic Count (40x objective): 1. Total Nottingham Score: 6/9.",
         {"grade": 2, "total": 6, "tubule": 3, "pleo": 2, "mitoses": 1}),
        ("B. Composite histologic (modified SBR) grade: III/III.", {"grade": 3, "total": None, "tubule": None, "pleo": None, "mitoses": None}),
        ("NOTTINGHAM GRADE: POORLY DIFFERENTIATED (G3).", {"grade": 3, "total": None, "tubule": None, "pleo": None, "mitoses": None}),
        ("BLOOM-RICHARDSON GRADING: GRADE III OF III (MINIMAL TUBULE FORMATIONS)", {"grade": 3, "total": None, "tubule": None, "pleo": None, "mitoses": None}),
        ("INVASIVE DUCTAL CARCINOMA, SBR GRADE 1, LARGEST FOCUS 2.1-CM.", {"grade": 1, "total": None, "tubule": None, "pleo": None, "mitoses": None}),
        ("Nottingham grade (1, 2, 3): 3.", {"grade": 3, "total": None, "tubule": None, "pleo": None, "mitoses": None}),
        ("NOTTINGHAM GRADE 3 (TUBULE. FORMATION 3, NUCLEAR PLEOMORPHISM 3, MITOTIC ACTIVITY 3; TOTAL SCORE 9/9).",
         {"grade": 3, "total": 9, "tubule": 3, "pleo": 3, "mitoses": 3}),
    ],
)
def test_statements_are_read(text, expected):
    assert values(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "DUCTAL CARCINOMA IN SITU, SOLID TYPE, NUCLEAR GRADE 3, WITH COMEDO NECROSIS.",   # DCIS grade
        "Grade of in situ carcinoma: 1/3 by SBR criteria.",
        "RESULTS: Recurrence Score: 9. ER Score: 1",                                        # Oncotype
        "HER2/neu overexpression is negative, score of 1+.",
        "Number of Her2 signals/nucleus. 2.3. Number of CEP 17 signals/nucleus. 1.8.",      # FISH counts
        "Mitotic index = <1/hpf (low). Mitotic count: 12 per 10 HPF.",                      # raw counts
        "Total Nottingham Score: Score cannot be determined.",
        "Nottingham score 7-8 (of 9)",                                                      # a range
        "MODIFIED BLACK'S NUCLEAR GRADE 1-2 (WELL TO MODERATELY DIFFERENTIATED)",
        "Invasive ductal carcinoma, moderately differentiated, 2.0 cm.",                    # no grade named
    ],
)
def test_other_numbers_are_not_nottingham_values(text):
    assert values(text) == dict.fromkeys(("grade", "total", "tubule", "pleo", "mitoses"))


def test_nuclear_grade_counts_as_pleomorphism_only_among_the_components():
    among = "NOTTINGHAM SCORE: Nuclear grade: 2. Tubule formation: 3. Mitotic activity score: 1. Total Nottingham score: 6."
    assert values(among)["pleo"] == 2
    alone = "Invasive ductal carcinoma, nuclear grade 2, measuring 1.2 cm."
    assert values(alone)["pleo"] is None


def test_a_legend_line_is_not_a_grade():
    assert values("Total Nottingham Score - Grade II: 6-7 points.")["grade"] is None


def test_captures_keep_their_spans():
    text = "Right breast. NOTTINGHAM GRADE: 2 OF 3."
    (capture,) = extract(text).captures
    assert text[capture.start:capture.end] == capture.text == "NOTTINGHAM GRADE: 2 OF 3"


def test_two_different_grades_conflict():
    result = extract("A. SBR GRADE 3 IN LARGE TUMOR FOCUS. B. This focus of tumor is SBR grade I, with tubular formation.")
    assert result.conflicts() == {"grade": {1, 3}}
    with pytest.raises(ConflictingValues, match="grade"):
        result.value("grade")


def test_the_same_value_repeated_is_not_a_conflict():
    result = extract("SBR GRADE 2, MEASURING 1.5-CM. Modified Scarff Bloom Richardson Grade: 2.")
    assert result.conflicts() == {} and result.value("grade") == 2


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Carcinoma ductale invasivum - NHG2 (3+2+1).", {"grade": 2, "total": 6, "tubule": 3, "pleo": 2, "mitoses": 1}),
        ("Tumor grade: 2 (Elston SBR grade, A/N/M = 3/2/1).", {"grade": 2, "total": 6, "tubule": 3, "pleo": 2, "mitoses": 1}),
        ("Tubular formation score: Score 3. NUCLEAR PLEOMORPHISM SCORE: Score 2. MITOTIC RATE SCORE: Score 1.",
         {"grade": None, "total": None, "tubule": 3, "pleo": 2, "mitoses": 1}),
        ("Composite histologic (modified SBR) grade II. - Architecture: 3. - Nuclear grade: 2. - Mitotic count: 1.",
         {"grade": 2, "total": None, "tubule": 3, "pleo": 2, "mitoses": 1}),
        ("Grade, Histologic: 3. Grade, Nuclear: 3. Grade, Mitotic: III.", {"grade": 3, "total": None, "tubule": None, "pleo": 3, "mitoses": 3}),
        ("invasive lobular breast carcinoma (malignancy grade II), pT2, pN0, MX, R0, G2 (L0, V0).",
         {"grade": 2, "total": None, "tubule": None, "pleo": None, "mitoses": None}),
        ("Modified B-R Grade: 2.", {"grade": 2, "total": None, "tubule": None, "pleo": None, "mitoses": None}),
        ("Nottingham Histologic Score: 3 (3+3+2=8).", {"grade": None, "total": 8, "tubule": 3, "pleo": 3, "mitoses": 2}),
        ("Diagnosis: Infiltrating ductal carcinoma. Grade: 2.", {"grade": 2, "total": None, "tubule": None, "pleo": None, "mitoses": None}),
    ],
)
def test_site_specific_phrasings_are_read(text, expected):
    assert values(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Sections are submitted as follows: G1: superior tip. G2: slice 3. G3-G4: slice 5.",  # cassette labels
        "The remaining fatty tissue is entirely submitted in cassettes G2 and G3.",
        "Histologic grade: Moderately differentiated.",                                       # no system named
        "multicentric, in parts confluent LIN formations (grade I).",                         # lobular neoplasia
        "Grade of DCIS: 2.",
    ],
)
def test_labels_and_non_nottingham_grades_are_not_read(text):
    assert values(text)["grade"] is None


def test_a_european_grade_alone_in_parentheses_is_read():
    assert values("Invasive lobular carcinoma, pleomorphic variant (G II).")["grade"] == 2
