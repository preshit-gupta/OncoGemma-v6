"""
AST-based hardcoded literal scanner.
SPEC-01 §3.8 and WP-2.5.
Flags hardcoded clinical and numeric fallbacks in backend code.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import hashlib
import json
import os
import sys
from typing import Any
import yaml


@dataclass(frozen=True)
class Finding:
    rule: str      # "L1".."L4"
    path: str      # repo-relative, forward slashes, e.g. "backend/worker/x.py"
    line: int
    col: int
    snippet: str   # stripped source line


ALLOWED_L4 = {0, 1, -1, 2, 255, 1e-6, 1e-8, 0.5}


def line_hash(line_text: str) -> str:
    """Return SHA-1 hex digest of line_text.strip()."""
    return hashlib.sha1(line_text.strip().encode("utf-8")).hexdigest()


def filter_allowlisted(findings: list[Finding], allowlist: list[dict]) -> list[Finding]:
    """Filter out findings that match an allowlist/baseline entry by (path, rule, line_hash)."""
    allow_set = {
        (item.get("path"), item.get("rule"), item.get("line_hash"))
        for item in allowlist
        if isinstance(item, dict)
    }
    return [
        f for f in findings
        if (f.path, f.rule, line_hash(f.snippet)) not in allow_set
    ]


def _is_num_or_str_constant(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return False
        return isinstance(node.value, (int, float, str))
    return False


def _get_numeric_value(node: ast.AST) -> float | int | None:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        val = _get_numeric_value(node.operand)
        if val is not None:
            return -val if isinstance(node.op, ast.USub) else val
    return None


def _is_clinical_param_name(name: str) -> bool:
    name_lower = name.lower()
    return any(k in name_lower for k in ("mpp", "threshold", "radius", "conf")) or name_lower.endswith("_um")


def _get_snippet(lines: list[str], lineno: int) -> str:
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()
    return ""


class LiteralVisitor(ast.NodeVisitor):
    def __init__(self, path: str, source_lines: list[str]):
        self.path = path.replace("\\", "/")
        self.source_lines = source_lines
        self.findings: list[Finding] = []
        self.in_pipeline = "backend/pipeline/" in self.path or self.path.startswith("backend/pipeline/")
        self.in_worker = "backend/worker/" in self.path or self.path.startswith("backend/worker/")
        self.in_routers = "backend/app/routers/" in self.path or self.path.startswith("backend/app/routers/")
        self.in_l2_scope = self.in_pipeline or self.in_worker or self.in_routers

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        if isinstance(node.op, ast.Or):
            last = node.values[-1]
            if _is_num_or_str_constant(last):
                self.findings.append(Finding(
                    rule="L1",
                    path=self.path,
                    line=last.lineno,
                    col=last.col_offset,
                    snippet=_get_snippet(self.source_lines, last.lineno),
                ))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self.in_l2_scope:
            if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                default_node: ast.AST | None = None
                if len(node.args) >= 2:
                    default_node = node.args[1]
                else:
                    for kw in node.keywords:
                        if kw.arg == "default":
                            default_node = kw.value
                            break
                if default_node and _is_num_or_str_constant(default_node):
                    self.findings.append(Finding(
                        rule="L2",
                        path=self.path,
                        line=default_node.lineno,
                        col=default_node.col_offset,
                        snippet=_get_snippet(self.source_lines, default_node.lineno),
                    ))
        self.generic_visit(node)

    def _check_func_args(self, args: ast.arguments) -> None:
        pos_args = args.posonlyargs + args.args
        num_defaults = len(args.defaults)
        if num_defaults > 0:
            default_args = pos_args[-num_defaults:]
            for arg_obj, def_node in zip(default_args, args.defaults):
                if _is_clinical_param_name(arg_obj.arg):
                    if _get_numeric_value(def_node) is not None:
                        self.findings.append(Finding(
                            rule="L3",
                            path=self.path,
                            line=def_node.lineno,
                            col=def_node.col_offset,
                            snippet=_get_snippet(self.source_lines, def_node.lineno),
                        ))
        for arg_obj, def_node in zip(args.kwonlyargs, args.kw_defaults):
            if def_node is not None and _is_clinical_param_name(arg_obj.arg):
                if _get_numeric_value(def_node) is not None:
                    self.findings.append(Finding(
                        rule="L3",
                        path=self.path,
                        line=def_node.lineno,
                        col=def_node.col_offset,
                        snippet=_get_snippet(self.source_lines, def_node.lineno),
                    ))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_func_args(node.args)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_func_args(node.args)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if self.in_pipeline:
            for op_node in [node.left] + list(node.comparators):
                val = _get_numeric_value(op_node)
                if val is not None and val not in ALLOWED_L4:
                    self.findings.append(Finding(
                        rule="L4",
                        path=self.path,
                        line=op_node.lineno,
                        col=op_node.col_offset,
                        snippet=_get_snippet(self.source_lines, op_node.lineno),
                    ))
        self.generic_visit(node)


def scan_source(source: str, path: str) -> list[Finding]:
    """Scan Python source code and return list of Finding objects."""
    norm_path = path.replace("\\", "/")
    lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=norm_path)
    except SyntaxError:
        return []
    visitor = LiteralVisitor(norm_path, lines)
    visitor.visit(tree)
    # Sort findings by line and column
    visitor.findings.sort(key=lambda f: (f.line, f.col))
    return visitor.findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan backend code for hardcoded literals.")
    parser.add_argument("--root", default=".", help="Root directory (default: current directory)")
    parser.add_argument("--allowlist", default=None, help="Path to literals_allowlist.yaml")
    parser.add_argument("--baseline", default=None, help="Path to literals_baseline.json")
    parser.add_argument("--write-baseline", action="store_true", help="Write findings to baseline file")

    args = parser.parse_args(argv)
    root_dir = os.path.abspath(args.root)

    # 1. Walk backend directory
    backend_dir = os.path.join(root_dir, "backend")
    findings: list[Finding] = []

    if os.path.isdir(backend_dir):
        for dirpath, dirnames, filenames in os.walk(backend_dir):
            rel_dir = os.path.relpath(dirpath, root_dir).replace("\\", "/")
            # Exclude tests, alembic, __pycache__
            parts = rel_dir.split("/")
            if any(p in ("tests", "alembic", "__pycache__") for p in parts):
                continue

            for fname in sorted(filenames):
                if fname.endswith(".py"):
                    full_p = os.path.join(dirpath, fname)
                    rel_p = os.path.relpath(full_p, root_dir).replace("\\", "/")
                    parts_file = rel_p.split("/")
                    if any(p in ("tests", "alembic") for p in parts_file):
                        continue
                    try:
                        with open(full_p, "r", encoding="utf-8-sig") as f:
                            content = f.read()
                        findings.extend(scan_source(content, rel_p))
                    except Exception:
                        pass

    # 2. Filter allowlist
    allowlist_path = args.allowlist or os.path.join(root_dir, "tools", "literals_allowlist.yaml")
    if os.path.isfile(allowlist_path):
        try:
            with open(allowlist_path, "r", encoding="utf-8") as f:
                allow_data = yaml.safe_load(f) or []
            if isinstance(allow_data, list):
                findings = filter_allowlisted(findings, allow_data)
        except Exception:
            pass

    # 3. Handle --write-baseline
    if args.write_baseline:
        baseline_path = args.baseline or os.path.join(root_dir, "tools", "literals_baseline.json")
        os.makedirs(os.path.dirname(os.path.abspath(baseline_path)), exist_ok=True)
        records = [
            {"path": f.path, "rule": f.rule, "line_hash": line_hash(f.snippet)}
            for f in findings
        ]
        records.sort(key=lambda x: (x["path"], x["rule"], x["line_hash"]))
        with open(baseline_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)
        return 0

    # 4. Filter baseline if provided or exists
    baseline_path = args.baseline or os.path.join(root_dir, "tools", "literals_baseline.json")
    if os.path.isfile(baseline_path):
        try:
            with open(baseline_path, "r", encoding="utf-8") as f:
                baseline_data = json.load(f) or []
            if isinstance(baseline_data, list):
                findings = filter_allowlisted(findings, baseline_data)
        except Exception:
            pass

    # 5. Report remaining findings
    if not findings:
        return 0

    for f in findings:
        print(f"{f.path}:{f.line}:{f.col}: {f.rule} {f.snippet}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
