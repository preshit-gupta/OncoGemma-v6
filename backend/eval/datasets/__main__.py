"""
CLI entry point for dataset discovery and fetching per SPEC-02 §5.5 and WP-5.2.

Usage:
    python -m eval.datasets <key> discover --out <parquet> [--source <dir>]
    python -m eval.datasets <key> fetch --manifest <parquet> --dest <uri> --confirm-bytes <N>
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import pandas as pd

from . import get_adapter
from .storage import storage_from_uri


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.datasets",
        description="Dataset discover and fetch CLI wrapper (SPEC-02 §5.5)",
    )
    parser.add_argument("key", help="Registered dataset key (e.g. tcga_brca_dx, midogpp_breast, bcss, bcnb)")

    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommand to execute")

    # Discover subcommand
    discover_parser = subparsers.add_parser("discover", help="Discover remote dataset and emit parquet listing")
    discover_parser.add_argument("--out", required=True, help="Output parquet path")
    discover_parser.add_argument("--source", help="Local dataset directory, for datasets distributed as files (e.g. bcss masks)")

    # Fetch subcommand
    fetch_parser = subparsers.add_parser("fetch", help="Stream slide payloads to destination storage")
    fetch_parser.add_argument("--manifest", required=True, help="Path to input manifest or discovered parquet")
    fetch_parser.add_argument("--dest", required=True, help="Destination storage URI (file:///... or gs://...)")
    fetch_parser.add_argument("--confirm-bytes", required=True, type=int, help="Confirmed byte threshold guard")

    parsed_args = parser.parse_args(args)
    adapter = get_adapter(parsed_args.key)

    if parsed_args.command == "discover":
        print(f"Discovering records for dataset '{parsed_args.key}'...")
        df = adapter.discover(parsed_args.source) if parsed_args.source else adapter.discover()
        out_path = Path(parsed_args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out_path)
        print(f"Discovered {len(df)} records. Saved to {out_path}")
        if "file_size" in df.columns:
            total_bytes = int(df["file_size"].sum())
            print(f"Total payload size: {total_bytes:,} bytes ({total_bytes / (1024**3):.2f} GiB)")
        return 0

    elif parsed_args.command == "fetch":
        manifest_path = Path(parsed_args.manifest)
        if not manifest_path.exists():
            print(f"Error: manifest file not found: {manifest_path}", file=sys.stderr)
            return 1

        df = pd.read_parquet(manifest_path)
        dest_storage = storage_from_uri(parsed_args.dest)
        print(f"Fetching {len(df)} items for '{parsed_args.key}' into {parsed_args.dest}...")

        if hasattr(adapter, "fetch_many"):
            fetched = adapter.fetch_many(df, dest_storage, confirm_bytes=parsed_args.confirm_bytes)
        else:
            fetched = {}
            for _, row in df.iterrows():
                f_file = adapter.fetch(row, dest_storage)
                fetched[str(f_file.slide_id)] = f_file

        print(f"Successfully fetched {len(fetched)} files.")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
