"""
CLI wrapper to verify splits lock file and assert disjointness across all datasets.
WP-5.3 and SPEC-02 §5.2.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import pandas as pd

from eval.splits import verify_lock, check_disjoint, SplitLeakError, LockMismatchError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check split lock file and leakage.")
    parser.add_argument("--lock", required=True, help="Path to lock file")
    parser.add_argument("--root", default=".", help="Root directory")
    args = parser.parse_args(argv)

    lock_p = Path(args.lock)
    root_p = Path(args.root)

    try:
        verify_lock(lock_p, root_p)
    except LockMismatchError as e:
        print(f"[ERROR] Lock verification failed: {e}", file=sys.stderr)
        return 1

    with open(lock_p, "r", encoding="utf-8") as f:
        lock_data = json.load(f)

    frames: dict[str, pd.DataFrame] = {}
    for rel_posix in lock_data.keys():
        if rel_posix.endswith(".parquet"):
            file_p = root_p / rel_posix
            try:
                frames[rel_posix] = pd.read_parquet(file_p)
            except Exception as e:
                print(f"[ERROR] Failed to read {file_p}: {e}", file=sys.stderr)
                return 1

    try:
        check_disjoint(frames)
    except SplitLeakError as e:
        print(f"[ERROR] Leakage detected: {e}", file=sys.stderr)
        return 1

    print(f"[OK] Verified lock {lock_p} and confirmed disjoint splits across {len(frames)} parquet file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
