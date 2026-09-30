"""Deterministic Nottingham-grade grammar for breast pathology reports (SPEC-02 §4 step 2).

The grammar is deliberately narrow: it reads a value only where the report names the
Nottingham system (or one of its aliases), a grade field, or a Nottingham component, and every
capture keeps its character span. What it misses, the LLM extraction finds; a disagreement
between the two goes to QA, so precision matters more here than recall.

Shaped on the TCGA-BRCA report corpus (TCGA-Reports v1), whose OCR text is often in capitals
and has ". " where the PDF had a line break ("TUBULE. FORMATION"), so gaps between the words
of a phrase allow dots as well as spaces. The phrasings covered, by site style:
- "NOTTINGHAM GRADE: 2 OF 3", "SBR GRADE II", "Composite histologic (modified SBR) grade: III",
  "Modified B-R Grade: 2", "Grade, Histologic: 3", "malignancy grade II", "NHG2", "G II", "Grade: 2";
- components as "Tubules=3", "Tubular formation score: Score 3", "Architecture: 3",
  "Nuclear pleomorphism: 2", "Grade, Nuclear: 3", "Mitotic count (40x): 1", and as sums:
  "NHG2 (3+2+1)", "(score: tubules 3 + nucleus 2 + mitoses 1 = 6/9)", "A/N/M = 3/2/1";
- totals as "NOTTINGHAM SCORE: 7 OF 9", "Total points ... = 8 points", "Overall grade: 9/9".
"Well / moderately / poorly differentiated" counts as a grade only after a named system
("Nottingham grade: poorly differentiated"): a bare "Histologic grade: moderately
differentiated" is not necessarily a Nottingham grade.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

FIELDS = ("grade", "total", "tubule", "pleo", "mitoses")
COMPONENTS = ("tubule", "pleo", "mitoses")

_GAP = r"[\s.\-]*"  # between the words of a phrase, including ". " line-break artefacts
_ROMAN = {"I": 1, "II": 2, "III": 3}
_DIFFERENTIATION = {"well": 1, "moderately": 2, "poorly": 3}
_SMALL = r"(?:III|II|I|[1-3])"  # a 1-3 value, arabic or roman (longest roman first)
_END = r"(?![0-9A-Za-z]|\.[0-9])"  # not part of a longer number, word or decimal

_NAMED_SYSTEM = r"nottingham|elston|bloom|richardson|e?m?sbr|b-r"
_GRADE_HEAD = re.compile(
    r"\b(?P<anchor>" + _NAMED_SYSTEM + r"|histologic(?:al)?|overall|composite|combined|tumou?r|malignancy)\b"
    r"[^:=;\n\d]{0,40}?\bgrad(?:e|ing)\b"
    r"|\bgrade,\s*(?:histologic(?:al)?|nottingham|sbr)\b",
    re.IGNORECASE,
)
_TOTAL_HEAD = re.compile(
    r"\b(?:nottingh[ae]m|total|combined|bloom|richardson|e?m?sbr)\b[^:=;\n\d]{0,30}?\bscore\b"
    r"|\btotal\s+points\b[^:=;\n\d]{0,40}",
    re.IGNORECASE,
)
# "(score: tubules 3 + nucleus 2 + mitoses 1 =. 6/9)"
_SUM_TOTAL = re.compile(r"=" + _GAP + r"(?P<v>[3-9])\s*/\s*9\b")
# "NHG2 (3+2+1)", "Nottingham grade 2 (3 +2+1 = 6)", "Nottingham Histologic Score: 3 (3+3+2=8)"
_COMPONENT_SUM = re.compile(
    r"(?:\bNHG\s*" + _SMALL + r"|\bgrade\s*" + _SMALL + r"|\bscore\b[\s:=]*[1-9]?)[^.\n]{0,20}?"
    r"\(?\s*(?P<t>[1-3])\s*\+\s*(?P<p>[1-3])\s*\+\s*(?P<m>[1-3])(?![0-9])\s*(?:=\s*(?P<s>[3-9])(?![0-9]))?",
    re.IGNORECASE,
)
# "A/N/M = 3/2/1": architecture, nuclei, mitoses.
_ANM = re.compile(r"\bA\s*/\s*N\s*/\s*M\s*[:=]?\s*(?P<t>[1-3])\s*/\s*(?P<p>[1-3])\s*/\s*(?P<m>[1-3])(?![0-9])")
_NHG = re.compile(r"\bNHG\s*(?P<v>" + _SMALL + r")" + _END)
# "G2" in a TNM-style line ("pT2, pN0, MX, R0, G2 (L0, V0)") or alone in parentheses
# ("lobular variant (G II)"). Elsewhere "G1", "G2" are usually
# cassette labels ("submitted in G1", "cassettes G2 and G3", "G1-G2: margin").
_EUROPEAN_G = re.compile(r"(?<![A-Za-z0-9/\-])G\s?(?P<v>" + _SMALL + r")(?![0-9A-Za-z/+]|\s*[-:])")
_TNM_CONTEXT = re.compile(r"\bp?[TN](?:[0-4][a-d]?|X|is)\b|\bM[01X]\b|\bR[0-2]\b|\b[LV][0-2]\b")
TNM_CONTEXT_CHARS = 40
_BARE_GRADE = re.compile(
    r"(?<![A-Za-z])grade[\s.:=\-]*(?P<v>" + _SMALL + r")(?:\s*(?:/|of|out\s+of)\s*(?:3|III))?" + _END,
    re.IGNORECASE,
)
_BARE_GRADE_EXCLUDED_BEFORE = re.compile(
    r"nuclear|in[\s.\-]*situ|\bdcis\b|\blcis\b|\blin\b|intraepithelial|intraductal|black|comedo"
    r"|histologic|nottingham|sbr|richardson",
    re.IGNORECASE,
)
BARE_GRADE_LOOKBACK_CHARS = 60

_PARENTHETICAL = re.compile(r"\s*\([^)]{0,40}\)")
_LEAD = r"^[\s.:=\-]*(?:(?:is|of|by)\b[\s.:=\-]*)?"
# A legend row ("Grade II: 6-7 points", "Grade 2: Score of 6 or 7") states no value.
_LEGEND = r"(?!\s*:\s*(?:score\s+of\s+)?\d\s*(?:-|to|or)\s*\d)"
_GRADE_VALUE = re.compile(
    _LEAD
    + r"(?:grade" + _GAP + r")?"
    + r"(?:G\s*(?P<g>[1-3])\b"
    + r"|(?P<num>[1-3])(?![0-9])(?:\s*(?:/|of|out\s+of)\s*(?:9|3|III)\b)?"
    + r"|(?P<roman>III|II|I)(?![A-Za-z])"
    + r"|(?P<word>well|moderately|poorly)" + _GAP + r"differentiated)"
    + r"(?!\s*:\s*\d\s*(?:-|to)\s*\d)",
    re.IGNORECASE,
)
_TOTAL_VALUE = re.compile(
    _LEAD
    + r"(?P<v>[3-9])(?![0-9+%]|\.[0-9]|\s*/\s*(?!9)\d|\s*(?:-|to)\s*\d|\s*\(\s*\d\s*\+)"
    + r"(?:\s*(?:/|of|out\s+of)\s*9\b)?",
    re.IGNORECASE,
)
# "Overall grade: 9/9" is a total, not a grade.
_OVER_NINE = re.compile(_LEAD + r"(?P<v>[3-9])\s*(?:/|of|out\s+of)\s*9\b", re.IGNORECASE)

_COMPONENT_HEADS = {
    "tubule": re.compile(
        # Longer alternatives first: the first alternative that matches wins.
        r"\b(?:tubul(?:e|ar)" + _GAP + r"(?:formation|score|differentiation)|tubule\s*/\s*papilla\s+formation|tubules?"
        r"|glandular" + _GAP + r"(?:\(acinar\))?[\s./]*tubular" + _GAP + r"(?:differentiation|formation)"
        r"|glandular" + _GAP + r"(?:formation|differentiation)|architect(?:ure|ural)(?:" + _GAP + r"score)?)",
        re.IGNORECASE,
    ),
    "pleo": re.compile(
        r"\b(?:nuclei|nucleus|nuclear" + _GAP + r"(?:pleomorphism|score|atypia)|pleomorphism)",
        re.IGNORECASE,
    ),
    "mitoses": re.compile(
        r"\b(?:mitoses|mitosis|mitotic" + _GAP + r"(?:activity|count|rate|figures?)?)",
        re.IGNORECASE,
    ),
}
# Any mix of "(40x objective)", "score", "is", "of" and separators between a component and its value.
_COMPONENT_VALUE = re.compile(
    r"^(?:[\s.:=,\-]*(?:\([^)]{0,30}\)|score\b|is\b|of\b))*[\s.:=,\-]*"
    r"(?P<v>" + _SMALL + r")(?![0-9A-Za-z]|\.[0-9]|\s*/\s*h|\s*per\b|\s*%)",
    re.IGNORECASE,
)
# "Nuclear grade" is the pleomorphism score only next to other Nottingham statements; elsewhere
# it is usually the DCIS nuclear grade, which is not part of the Nottingham grade.
_NUCLEAR_GRADE = re.compile(
    r"\b(?:nuclear" + _GAP + r"grade(?:" + _GAP + r"score)?|grade,\s*nuclear)[\s.:=\-]*(?P<v>" + _SMALL + r")" + _END,
    re.IGNORECASE,
)
_IN_SITU = re.compile(r"in[\s.\-]*situ|\bdcis\b|\blcis\b|intraductal", re.IGNORECASE)
NUCLEAR_GRADE_NEIGHBOUR_CHARS = 150  # a grade, tubule or mitoses capture must be this close
IN_SITU_LOOKBACK_CHARS = 80


@dataclass(frozen=True)
class Capture:
    field: str
    value: int
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class GrammarResult:
    captures: tuple[Capture, ...]

    def values(self, field: str) -> set[int]:
        return {c.value for c in self.captures if c.field == field}

    def value(self, field: str) -> int | None:
        """The field's value, or None when not stated. Raises ConflictingValues for two or more."""
        found = self.values(field)
        if len(found) > 1:
            raise ConflictingValues(field, found)
        return next(iter(found), None)

    def conflicts(self) -> dict[str, set[int]]:
        return {f: self.values(f) for f in FIELDS if len(self.values(f)) > 1}


class ConflictingValues(ValueError):
    def __init__(self, field: str, values: set[int]):
        super().__init__(f"the report states {field} as {sorted(values)}")
        self.field = field
        self.values = values


def _small_int(token: str) -> int:
    return int(token) if token.isdigit() else _ROMAN[token.upper()]


def _skip_parentheticals(text: str, pos: int) -> int:
    while (m := _PARENTHETICAL.match(text, pos)) is not None:
        pos = m.end()
    return pos


def _capture(field: str, value: int, text: str, start: int, end: int) -> Capture:
    return Capture(field, value, start, end, text[start:end])


def _grade_captures(text: str) -> list[Capture]:
    captures = []
    for head in _GRADE_HEAD.finditer(text):
        if re.search(r"nuclear" + _GAP + r"grad", head.group(0), re.IGNORECASE):
            continue
        pos = _skip_parentheticals(text, head.end())
        tail = text[pos:pos + 60]
        over_nine = _OVER_NINE.match(tail)
        if over_nine is not None:
            captures.append(_capture("total", int(over_nine.group("v")), text, head.start(), pos + over_nine.end()))
            continue
        m = _GRADE_VALUE.match(tail)
        if m is None:
            continue
        if m.group("word"):
            named = head.group("anchor") and re.fullmatch(_NAMED_SYSTEM, head.group("anchor"), re.IGNORECASE)
            if not named:
                continue
            value = _DIFFERENTIATION[m.group("word").lower()]
        elif m.group("g"):
            value = int(m.group("g"))
        elif m.group("num"):
            value = int(m.group("num"))
        else:
            value = _ROMAN[m.group("roman").upper()]
        captures.append(_capture("grade", value, text, head.start(), pos + m.end()))

    for m in _NHG.finditer(text):
        captures.append(_capture("grade", _small_int(m.group("v")), text, m.start(), m.end()))
    for m in _EUROPEAN_G.finditer(text):
        window = text[max(0, m.start() - TNM_CONTEXT_CHARS):m.end() + TNM_CONTEXT_CHARS]
        in_parentheses = re.search(r"\(\s*$", text[:m.start()]) and re.match(r"\s*\)", text[m.end():])
        if in_parentheses or _TNM_CONTEXT.search(window) is not None:
            captures.append(_capture("grade", _small_int(m.group("v")), text, m.start(), m.end()))
    for m in _BARE_GRADE.finditer(text):
        before = text[max(0, m.start() - BARE_GRADE_LOOKBACK_CHARS):m.start()]
        legend = re.match(_LEGEND, text[m.end():], re.IGNORECASE) is None
        if _BARE_GRADE_EXCLUDED_BEFORE.search(before) is None and not legend:
            captures.append(_capture("grade", _small_int(m.group("v")), text, m.start(), m.end()))
    return captures


def _total_captures(text: str) -> list[Capture]:
    captures = []
    for head in _TOTAL_HEAD.finditer(text):
        if re.search(r"recurrence|her-?2|allred|ki-?67", head.group(0), re.IGNORECASE):
            continue
        pos = _skip_parentheticals(text, head.end())
        m = _TOTAL_VALUE.match(text[pos:pos + 40])
        if m is not None:
            captures.append(_capture("total", int(m.group("v")), text, head.start(), pos + m.end()))
    for m in _SUM_TOTAL.finditer(text):
        captures.append(_capture("total", int(m.group("v")), text, m.start(), m.end()))
    return captures


def _component_captures(text: str) -> list[Capture]:
    captures = []
    for field, head_re in _COMPONENT_HEADS.items():
        for head in head_re.finditer(text):
            m = _COMPONENT_VALUE.match(text[head.end():head.end() + 40])
            if m is not None:
                captures.append(_capture(field, _small_int(m.group("v")), text, head.start(), head.end() + m.end()))
    for pattern in (_COMPONENT_SUM, _ANM):
        for m in pattern.finditer(text):
            parts = {"tubule": int(m.group("t")), "pleo": int(m.group("p")), "mitoses": int(m.group("m"))}
            captures += [_capture(f, v, text, m.start(), m.end()) for f, v in parts.items()]
            stated = m.groupdict().get("s")
            captures.append(_capture("total", int(stated) if stated else sum(parts.values()), text, m.start(), m.end()))
    return captures


def _nuclear_grade_captures(text: str, anchors: list[int]) -> list[Capture]:
    captures = []
    for m in _NUCLEAR_GRADE.finditer(text):
        near = any(abs(m.start() - a) <= NUCLEAR_GRADE_NEIGHBOUR_CHARS for a in anchors)
        in_situ = _IN_SITU.search(text[max(0, m.start() - IN_SITU_LOOKBACK_CHARS):m.start()])
        if near and in_situ is None:
            captures.append(_capture("pleo", _small_int(m.group("v")), text, m.start(), m.end()))
    return captures


def extract(text: str) -> GrammarResult:
    """Every Nottingham statement the grammar recognises, in text order."""
    captures = _grade_captures(text) + _total_captures(text) + _component_captures(text)
    anchors = [c.start for c in captures if c.field in ("grade", "tubule", "mitoses")]
    captures += _nuclear_grade_captures(text, anchors)
    # A span read twice (e.g. a total both as "score" and "= 6/9") is kept once.
    unique = {(c.field, c.value, c.start, c.end): c for c in captures}
    return GrammarResult(tuple(sorted(unique.values(), key=lambda c: (c.start, c.field))))
