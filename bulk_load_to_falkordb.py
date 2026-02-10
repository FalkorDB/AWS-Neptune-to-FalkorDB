#!/usr/bin/env python3
"""Bulk-load converted Neptune CSVs into FalkorDB.

This script expects an output directory produced by neptune_to_falkordb_converter.py
that contains:
- bulk_loader_manifest.json
- nodes_*.csv
- edges_*.csv

It then invokes the FalkorDB bulk loader (../falkordb-bulk-loader) with the correct
-N / -R arguments.

Example:
  python3 bulk_load_to_falkordb.py MYGRAPH --csv-dir ./out --server-url redis://127.0.0.1:6379

Pass-through arguments:
  Any additional arguments after the known flags will be forwarded to bulk_insert.py.
  Example:
    python3 bulk_load_to_falkordb.py MYGRAPH --csv-dir ./out -- --skip-invalid-edges
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List


def _load_manifest(csv_dir: Path) -> Dict[str, Any]:
    manifest_path = csv_dir / "bulk_loader_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load converted Neptune CSVs into FalkorDB using falkordb-bulk-loader"
    )
    parser.add_argument("graph", help="Target FalkorDB graph name")
    parser.add_argument(
        "--csv-dir",
        required=True,
        help="Directory containing converted CSVs and bulk_loader_manifest.json",
    )
    parser.add_argument(
        "--server-url",
        "-u",
        default="redis://127.0.0.1:6379",
        help="FalkorDB/Redis URL (default: redis://127.0.0.1:6379)",
    )
    parser.add_argument(
        "--bulk-loader-dir",
        default="../falkordb-bulk-loader",
        help="Path to the falkordb-bulk-loader repository clone (default: ../falkordb-bulk-loader)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the bulk loader command without executing it",
    )
    parser.add_argument(
        "--enforce-schema",
        dest="enforce_schema",
        action="store_true",
        default=None,
        help="Pass --enforce-schema to the bulk loader (overrides manifest)",
    )
    parser.add_argument(
        "--no-enforce-schema",
        dest="enforce_schema",
        action="store_false",
        default=None,
        help="Do not pass --enforce-schema to the bulk loader (overrides manifest)",
    )

    args, passthrough = parser.parse_known_args()

    csv_dir = Path(args.csv_dir)
    manifest = _load_manifest(csv_dir)

    bulk_loader_dir = Path(args.bulk_loader_dir)
    bulk_insert_py = bulk_loader_dir / "falkordb_bulk_loader" / "bulk_insert.py"
    if not bulk_insert_py.exists():
        raise FileNotFoundError(
            f"bulk_insert.py not found at {bulk_insert_py} (set --bulk-loader-dir accordingly)"
        )

    nodes: List[Dict[str, Any]] = manifest.get("output", {}).get("nodes", [])
    relations: List[Dict[str, Any]] = manifest.get("output", {}).get("relations", [])

    if not nodes:
        raise RuntimeError("Manifest does not include any node files")

    cmd: List[str] = [sys.executable, str(bulk_insert_py), args.graph, "-u", args.server_url]

    manifest_enforce_schema = bool(manifest.get("options", {}).get("enforce_schema", False))
    enforce_schema = (
        args.enforce_schema if args.enforce_schema is not None else manifest_enforce_schema
    )
    if enforce_schema:
        cmd.append("--enforce-schema")

    for n in nodes:
        labels = n.get("labels")
        file_name = n.get("file")
        if not labels or not file_name:
            raise RuntimeError(f"Invalid node entry in manifest: {n}")
        label_str = ":".join(labels)
        cmd.extend(["-N", label_str, str(csv_dir / file_name)])

    for r in relations:
        rel_type = r.get("type")
        file_name = r.get("file")
        if not rel_type or not file_name:
            raise RuntimeError(f"Invalid relation entry in manifest: {r}")
        cmd.extend(["-R", str(rel_type), str(csv_dir / file_name)])

    # Forward any remaining CLI args to the bulk loader
    # (strip leading '--' if user used it as a separator).
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]
    cmd.extend(passthrough)

    if args.dry_run:
        print(" ".join(cmd))
        return

    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
