"""
Acceptance tests for WP-2.5 (SPEC-01 §3.8): hardcoded-literal scanner.
Run from repo root:  python -m pytest tools/tests -q -p no:cacheprovider
Do NOT change assertions; explain disagreements in the PR.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
lint = pytest.importorskip("lint_literals")

WORKER = "backend/worker/x.py"
PIPELINE = "backend/pipeline/x.py"
ROUTERS = "backend/app/routers/x.py"
CORE = "backend/app/core/x.py"


def rules(findings):
    return sorted(f.rule for f in findings)


# ---- L1: `x or <literal>` -------------------------------------------------------

def test_l1_or_literal_number():
    fs = lint.scan_source("w = int(slide.width_px or 20000)\n", WORKER)
    assert rules(fs) == ["L1"]
    assert fs[0].line == 1


def test_l1_or_literal_string():
    assert rules(lint.scan_source("h = cfg_type or 'IDC-NST'\n", PIPELINE)) == ["L1"]


def test_l1_not_triggered_by_names_or_none():
    assert lint.scan_source("x = a or b\ny = a or None\n", WORKER) == []


def test_l1_applies_everywhere_under_backend():
    # L1 is not path-scoped (any backend python file)
    assert rules(lint.scan_source("mpp = s.mpp or 0.25\n", CORE)) == ["L1"]


# ---- L2: .get(key, <literal>) in scoped paths ----------------------------------

@pytest.mark.parametrize("path", [WORKER, PIPELINE, ROUTERS])
def test_l2_get_with_literal_default(path):
    assert rules(lint.scan_source("t = cfg.get('det_threshold', 0.35)\n", path)) == ["L2"]


@pytest.mark.parametrize("src", [
    "t = cfg.get('k')\n",
    "t = cfg.get('k', None)\n",
    "t = cfg.get('k', {})\n",
    "t = cfg.get('k', [])\n",
    "t = cfg.get('k', default)\n",
])
def test_l2_allowed_defaults(src):
    assert lint.scan_source(src, WORKER) == []


def test_l2_not_applied_outside_scope():
    assert lint.scan_source("t = cfg.get('det_threshold', 0.35)\n", CORE) == []


# ---- L3: numeric defaults on clinically named parameters -----------------------

@pytest.mark.parametrize("src", [
    "def f(mpp_x: float = 0.25):\n    pass\n",
    "def f(radius_um=262.0):\n    pass\n",
    "def f(conf_threshold: float = 0.35):\n    pass\n",
    "def f(*, tau_threshold=0.5):\n    pass\n",
])
def test_l3_numeric_default_on_clinical_name(src):
    assert rules(lint.scan_source(src, PIPELINE)) == ["L3"]


def test_l3_other_names_ok():
    assert lint.scan_source("def f(n: int = 3, batch_size=16):\n    pass\n", PIPELINE) == []


# ---- L4: numeric literals in comparisons inside backend/pipeline ---------------

def test_l4_comparison_literal():
    assert rules(lint.scan_source("if ver_c < 0.35:\n    pass\n", PIPELINE)) == ["L4"]


@pytest.mark.parametrize("src", [
    "if x < 0:\n    pass\n",
    "if x > 255:\n    pass\n",
    "if n == 1:\n    pass\n",
    "if n >= 2:\n    pass\n",
    "if d < 1e-6:\n    pass\n",
    "if p > 0.5:\n    pass\n",
])
def test_l4_allowed_constants(src):
    assert lint.scan_source(src, PIPELINE) == []


def test_l4_only_in_pipeline():
    assert lint.scan_source("if ver_c < 0.35:\n    pass\n", WORKER) == []


# ---- findings carry location + snippet ------------------------------------------

def test_finding_fields():
    f = lint.scan_source("a = 1\nw = s.width or 20000\n", WORKER)[0]
    assert (f.rule, f.path, f.line) == ("L1", WORKER, 2)
    assert "20000" in f.snippet
    assert isinstance(f.col, int)


# ---- allowlist and baseline -----------------------------------------------------

def test_allowlist_suppresses_by_line_hash(tmp_path):
    src = "w = s.width or 20000\n"
    allow = [{"path": WORKER, "rule": "L1", "line_hash": lint.line_hash("w = s.width or 20000"),
              "justification": "test"}]
    fs = lint.scan_source(src, WORKER)
    assert lint.filter_allowlisted(fs, allow) == []


def test_allowlist_hash_ignores_whitespace_and_line_number():
    assert lint.line_hash("  w = s.width or 20000  ") == lint.line_hash("w = s.width or 20000")


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_main_exit_codes_with_baseline(tmp_path):
    repo = tmp_path
    _write(repo / "backend/worker/a.py", "w = s.width or 20000\n")
    baseline = repo / "tools/literals_baseline.json"
    allow = repo / "tools/literals_allowlist.yaml"
    _write(allow, "[]\n")

    # 1) without a baseline, the finding fails the run
    assert lint.main(["--root", str(repo), "--allowlist", str(allow)]) == 1

    # 2) --write-baseline records current findings and succeeds
    assert lint.main(["--root", str(repo), "--allowlist", str(allow),
                      "--baseline", str(baseline), "--write-baseline"]) == 0
    data = json.loads(baseline.read_text(encoding="utf-8"))
    assert len(data) == 1 and data[0]["rule"] == "L1"

    # 3) with the baseline, the same finding passes
    assert lint.main(["--root", str(repo), "--allowlist", str(allow), "--baseline", str(baseline)]) == 0

    # 4) a NEW finding fails even with the baseline
    _write(repo / "backend/worker/b.py", "m = s.mpp or 0.25\n")
    assert lint.main(["--root", str(repo), "--allowlist", str(allow), "--baseline", str(baseline)]) == 1


def test_scans_only_backend_python(tmp_path):
    _write(tmp_path / "frontend/x.py", "w = s.width or 20000\n")
    _write(tmp_path / "backend/tests/test_x.py", "w = s.width or 20000\n")   # tests are excluded
    _write(tmp_path / "tools/literals_allowlist.yaml", "[]\n")
    assert lint.main(["--root", str(tmp_path), "--allowlist", str(tmp_path / "tools/literals_allowlist.yaml")]) == 0
