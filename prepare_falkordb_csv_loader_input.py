#!/usr/bin/env python3
"""Prepare CSV files for falkordb_csv_loader.py.

This script converts CSV outputs (including `--enforce-schema` style headers) into
the naming/header format expected by `falkordb_csv_loader.py`:

- Node files: `nodes_<Label>.csv` with an `id` column
- Edge files: `edges_<TYPE>.csv` with `source,target,source_label,target_label` columns

It can use `bulk_loader_manifest.json` when available, and can also discover
additional node/edge CSV files automatically.
"""

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple


IGNORE_DISCOVERY_FILES = {"indexes.csv", "constraints.csv", "update_queries.csv"}
SYMBOL_PATTERN = re.compile(r"[^0-9A-Za-z_]+")
UNDERSCORE_PATTERN = re.compile(r"_+")


@dataclass(frozen=True)
class ConversionJob:
    kind: str  # "node" or "edge"
    source_path: Path
    output_name: str


def normalize_header_token(token: str) -> str:
    return (token or "").strip().lstrip("\ufeff")


def header_base(token: str) -> str:
    token = normalize_header_token(token)
    if not token:
        return ""
    if ":" in token and not token.startswith(":"):
        return token.split(":", 1)[0].strip()
    return token


def sanitize_symbol(value: str, default: str) -> str:
    clean = SYMBOL_PATTERN.sub("_", value.strip())
    clean = UNDERSCORE_PATTERN.sub("_", clean).strip("_")
    if not clean:
        clean = default
    if clean[0].isdigit():
        clean = f"_{clean}"
    return clean


def unique_name(name: str, used_names: set[str]) -> str:
    if name not in used_names:
        return name
    suffix = 2
    while f"{name}_{suffix}" in used_names:
        suffix += 1
    return f"{name}_{suffix}"


def classify_node_header(header: str) -> str:
    token = normalize_header_token(header)
    lower = token.lower()
    upper = token.upper()
    base = header_base(token).lstrip("~")
    base_lower = base.lower()

    if lower in {"id", "~id", "vertex_id"} or base_lower in {"id", "vertex_id"}:
        return "id"
    if upper == ":ID" or upper.endswith(":ID"):
        return "id"

    if lower in {"labels", "label", "~labels", "~label"}:
        return "labels"
    if base_lower in {"labels", "label"}:
        return "labels"
    if upper in {":LABEL", ":LABELS"} or upper.endswith(":LABEL") or upper.endswith(":LABELS"):
        return "labels"

    return "property"


def classify_edge_header(header: str) -> str:
    token = normalize_header_token(header)
    lower = token.lower()
    upper = token.upper()
    base = header_base(token).lstrip("~")
    base_lower = base.lower()

    if lower in {"source", "~from", "from", "start_id", "src", "src_id"}:
        return "source"
    if base_lower in {"source", "from", "start_id", "src", "src_id"}:
        return "source"
    if upper == ":START_ID" or upper.endswith(":START_ID"):
        return "source"

    if lower in {"target", "~to", "to", "end_id", "dst", "dst_id"}:
        return "target"
    if base_lower in {"target", "to", "end_id", "dst", "dst_id"}:
        return "target"
    if upper == ":END_ID" or upper.endswith(":END_ID"):
        return "target"

    if lower in {"source_label", "from_label", "src_label"} or upper.endswith(":START_LABEL"):
        return "source_label"
    if lower in {"target_label", "to_label", "dst_label"} or upper.endswith(":END_LABEL"):
        return "target_label"

    if lower in {"type", "~label", "label", "relationship_type", "rel_type"}:
        return "type"
    if upper == ":TYPE" or upper.endswith(":TYPE"):
        return "type"

    return "property"


def build_node_header_mapping(headers: Sequence[str]) -> Tuple[List[str], List[Tuple[int, int]]]:
    output_headers: List[str] = ["id"]
    mappings: List[Tuple[int, int]] = []
    used_names = {"id"}

    seen_id = False
    seen_labels = False

    for index, raw_header in enumerate(headers):
        role = classify_node_header(raw_header)
        if role == "id":
            if not seen_id:
                mappings.append((index, 0))
                seen_id = True
            continue

        if role == "labels":
            if not seen_labels:
                output_headers.append("labels")
                mappings.append((index, len(output_headers) - 1))
                used_names.add("labels")
                seen_labels = True
            continue

        base = header_base(raw_header).lstrip("~")
        name = sanitize_symbol(base, f"property_{index + 1}")
        if name in {"id", "labels"}:
            name = f"{name}_prop"
        name = unique_name(name, used_names)
        output_headers.append(name)
        mappings.append((index, len(output_headers) - 1))
        used_names.add(name)

    if not seen_id:
        raise ValueError("No node ID column found (expected one of: id, id:ID, :ID, ~id).")

    return output_headers, mappings


def build_edge_header_mapping(headers: Sequence[str]) -> Tuple[List[str], List[Tuple[int, int]]]:
    fixed_source_index: Optional[int] = None
    fixed_target_index: Optional[int] = None
    fixed_source_label_index: Optional[int] = None
    fixed_target_label_index: Optional[int] = None
    fixed_type_index: Optional[int] = None
    property_columns: List[Tuple[int, str]] = []
    used_property_names: set[str] = set()

    for index, raw_header in enumerate(headers):
        role = classify_edge_header(raw_header)
        if role == "source":
            if fixed_source_index is None:
                fixed_source_index = index
            continue
        if role == "target":
            if fixed_target_index is None:
                fixed_target_index = index
            continue
        if role == "source_label":
            if fixed_source_label_index is None:
                fixed_source_label_index = index
            continue
        if role == "target_label":
            if fixed_target_label_index is None:
                fixed_target_label_index = index
            continue
        if role == "type":
            if fixed_type_index is None:
                fixed_type_index = index
            continue

        base = header_base(raw_header).lstrip("~")
        prop_name = sanitize_symbol(base, f"property_{index + 1}")
        if prop_name in {"source", "target", "type", "source_label", "target_label", "id", "labels"}:
            prop_name = f"{prop_name}_prop"
        prop_name = unique_name(prop_name, used_property_names)
        used_property_names.add(prop_name)
        property_columns.append((index, prop_name))

    if fixed_source_index is None or fixed_target_index is None:
        raise ValueError(
            "No edge endpoints found (expected source/target or :START_ID/:END_ID columns)."
        )

    output_headers = ["source", "target", "source_label", "target_label"]
    mappings: List[Tuple[int, int]] = [(fixed_source_index, 0), (fixed_target_index, 1)]

    if fixed_source_label_index is not None:
        mappings.append((fixed_source_label_index, 2))
    if fixed_target_label_index is not None:
        mappings.append((fixed_target_label_index, 3))
    if fixed_type_index is not None:
        output_headers.append("type")
        mappings.append((fixed_type_index, len(output_headers) - 1))

    for source_index, prop_name in property_columns:
        output_headers.append(prop_name)
        mappings.append((source_index, len(output_headers) - 1))

    return output_headers, mappings


def read_csv_headers(csv_path: Path) -> List[str]:
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as in_file:
        reader = csv.reader(in_file)
        try:
            headers = next(reader)
        except StopIteration:
            return []
    return [normalize_header_token(header) for header in headers]


def infer_file_kind(csv_path: Path) -> Optional[str]:
    filename = csv_path.name.lower()
    if "edge" in filename or "relationship" in filename:
        return "edge"
    if "node" in filename or "vertex" in filename:
        return "node"

    headers = read_csv_headers(csv_path)
    if not headers:
        return None

    edge_roles = {classify_edge_header(header) for header in headers}
    if "source" in edge_roles and "target" in edge_roles:
        return "edge"

    node_roles = {classify_node_header(header) for header in headers}
    if "id" in node_roles:
        return "node"

    return None


def derive_node_component(file_stem: str) -> str:
    stem = file_stem
    for prefix in ("nodes_", "node_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix) :]
            break
    for suffix in ("_nodes", "_node", "-nodes", "-node"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return sanitize_symbol(stem, "Node")


def derive_edge_component(file_stem: str) -> str:
    stem = file_stem
    for prefix in ("edges_", "edge_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix) :]
            break
    for suffix in ("_edges", "_edge", "-edges", "-edge", "_relationships", "-relationships"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return sanitize_symbol(stem, "REL")


def load_manifest_jobs(input_dir: Path, manifest_path: Path) -> List[ConversionJob]:
    jobs: List[ConversionJob] = []
    if not manifest_path.exists():
        return jobs

    with open(manifest_path, "r", encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)

    output_info = manifest.get("output", {})
    node_entries = output_info.get("nodes", []) or []
    relation_entries = output_info.get("relations", []) or []

    for entry in node_entries:
        source_name = entry.get("file")
        if not source_name:
            continue
        source_path = input_dir / source_name
        if not source_path.exists():
            raise FileNotFoundError(f"Manifest node file not found: {source_path}")

        labels = [str(label) for label in (entry.get("labels") or []) if str(label).strip()]
        if labels:
            component = "__".join(sanitize_symbol(label, "Label") for label in labels)
        else:
            component = derive_node_component(source_path.stem)
        jobs.append(
            ConversionJob(
                kind="node",
                source_path=source_path,
                output_name=f"nodes_{component}.csv",
            )
        )

    for entry in relation_entries:
        source_name = entry.get("file")
        if not source_name:
            continue
        source_path = input_dir / source_name
        if not source_path.exists():
            raise FileNotFoundError(f"Manifest edge file not found: {source_path}")

        rel_type = str(entry.get("type") or "").strip()
        if rel_type:
            component = sanitize_symbol(rel_type, "REL")
        else:
            component = derive_edge_component(source_path.stem)
        jobs.append(
            ConversionJob(
                kind="edge",
                source_path=source_path,
                output_name=f"edges_{component}.csv",
            )
        )

    return jobs


def collect_conversion_jobs(
    input_dir: Path, manifest_path: Path, include_discovered_files: bool
) -> List[ConversionJob]:
    jobs: List[ConversionJob] = []
    seen_sources: set[Path] = set()
    output_to_source: Dict[str, Path] = {}

    def register_job(job: ConversionJob) -> None:
        if job.source_path in seen_sources:
            return
        existing_source = output_to_source.get(job.output_name)
        if existing_source and existing_source != job.source_path:
            raise ValueError(
                f"Output filename collision: {job.output_name} from {existing_source.name} and {job.source_path.name}"
            )
        seen_sources.add(job.source_path)
        output_to_source[job.output_name] = job.source_path
        jobs.append(job)

    for job in load_manifest_jobs(input_dir, manifest_path):
        register_job(job)

    if include_discovered_files:
        for csv_path in sorted(input_dir.glob("*.csv")):
            if csv_path.name in IGNORE_DISCOVERY_FILES:
                continue
            if csv_path in seen_sources:
                continue

            inferred_kind = infer_file_kind(csv_path)
            if inferred_kind == "node":
                component = derive_node_component(csv_path.stem)
                register_job(
                    ConversionJob(
                        kind="node",
                        source_path=csv_path,
                        output_name=f"nodes_{component}.csv",
                    )
                )
            elif inferred_kind == "edge":
                component = derive_edge_component(csv_path.stem)
                register_job(
                    ConversionJob(
                        kind="edge",
                        source_path=csv_path,
                        output_name=f"edges_{component}.csv",
                    )
                )

    return jobs


def node_label_from_output_name(output_name: str) -> str:
    if output_name.startswith("nodes_") and output_name.endswith(".csv"):
        return output_name[len("nodes_") : -len(".csv")]
    return "UnknownLabel"


def infer_label_for_node_id(node_id_to_labels: Dict[str, Set[str]], node_id: str) -> str:
    labels = node_id_to_labels.get(node_id) or set()
    if not labels:
        return ""
    if len(labels) == 1:
        return next(iter(labels))
    return ":".join(sorted(labels))


def convert_csv_file(
    job: ConversionJob, output_path: Path, node_id_to_labels: Optional[Dict[str, Set[str]]] = None
) -> Tuple[int, List[str]]:
    source_headers = read_csv_headers(job.source_path)
    if not source_headers:
        raise ValueError(f"{job.source_path.name} is empty.")

    node_label = ""
    if job.kind == "node":
        output_headers, mappings = build_node_header_mapping(source_headers)
        node_label = node_label_from_output_name(job.output_name)
    elif job.kind == "edge":
        output_headers, mappings = build_edge_header_mapping(source_headers)
    else:
        raise ValueError(f"Unsupported job kind: {job.kind}")

    temp_output_path = output_path
    same_file = output_path.resolve() == job.source_path.resolve()
    if same_file:
        temp_output_path = output_path.with_suffix(f"{output_path.suffix}.tmp")

    row_count = 0
    with open(job.source_path, "r", encoding="utf-8-sig", newline="") as in_file, open(
        temp_output_path, "w", encoding="utf-8", newline=""
    ) as out_file:
        reader = csv.reader(in_file)
        writer = csv.writer(out_file)

        # Skip input header row and write normalized output header row.
        next(reader, None)
        writer.writerow(output_headers)

        expected_columns = len(source_headers)
        source_index = output_headers.index("source") if job.kind == "edge" else -1
        target_index = output_headers.index("target") if job.kind == "edge" else -1
        source_label_index = output_headers.index("source_label") if job.kind == "edge" else -1
        target_label_index = output_headers.index("target_label") if job.kind == "edge" else -1
        for row_number, row in enumerate(reader, start=2):
            if len(row) > expected_columns:
                raise ValueError(
                    f"{job.source_path.name}:{row_number} has {len(row)} columns, expected {expected_columns}."
                )
            if len(row) < expected_columns:
                row = row + [""] * (expected_columns - len(row))

            output_row = [""] * len(output_headers)
            for src_index, dst_index in mappings:
                output_row[dst_index] = row[src_index]

            if job.kind == "node" and node_id_to_labels is not None:
                node_id = output_row[0]
                if node_id:
                    node_id_to_labels.setdefault(node_id, set()).add(node_label)

            if job.kind == "edge" and node_id_to_labels is not None:
                if not output_row[source_label_index]:
                    output_row[source_label_index] = infer_label_for_node_id(
                        node_id_to_labels, output_row[source_index]
                    )
                if not output_row[target_label_index]:
                    output_row[target_label_index] = infer_label_for_node_id(
                        node_id_to_labels, output_row[target_index]
                    )

            writer.writerow(output_row)
            row_count += 1

    if same_file:
        temp_output_path.replace(output_path)

    return row_count, output_headers


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert Neptune CSV outputs into falkordb_csv_loader.py-compatible CSVs."
    )
    parser.add_argument(
        "--input-dir",
        "-i",
        required=True,
        help="Directory containing source CSV files.",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        required=True,
        help="Directory to write normalized CSV files.",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Optional manifest path. Defaults to <input-dir>/bulk_loader_manifest.json if present.",
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="Only convert files listed in the manifest.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)

    if not input_dir.exists() or not input_dir.is_dir():
        print(f"❌ Input directory does not exist or is not a directory: {input_dir}")
        return 1

    manifest_path = Path(args.manifest) if args.manifest else input_dir / "bulk_loader_manifest.json"
    include_discovered_files = not args.manifest_only

    if args.manifest_only and not manifest_path.exists():
        print(f"❌ --manifest-only was provided but manifest was not found: {manifest_path}")
        return 1

    try:
        jobs = collect_conversion_jobs(input_dir, manifest_path, include_discovered_files)
    except Exception as error:
        print(f"❌ Failed to collect conversion jobs: {error}")
        return 1

    if not jobs:
        print("⚠️ No node/edge CSV files found to convert.")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Converting {len(jobs)} file(s)...")
    converted_nodes = 0
    converted_edges = 0

    node_jobs = [job for job in jobs if job.kind == "node"]
    edge_jobs = [job for job in jobs if job.kind == "edge"]
    node_id_to_labels: Dict[str, Set[str]] = {}

    for job in node_jobs:
        output_path = output_dir / job.output_name
        try:
            row_count, output_headers = convert_csv_file(
                job, output_path, node_id_to_labels=node_id_to_labels
            )
        except Exception as error:
            print(f"❌ Failed converting {job.source_path.name}: {error}")
            return 1

        if job.kind == "node":
            converted_nodes += 1
        else:
            converted_edges += 1

        print(
            f"  ✅ {job.source_path.name} -> {job.output_name} ({row_count} rows, header={','.join(output_headers)})"
        )

    for job in edge_jobs:
        output_path = output_dir / job.output_name
        try:
            row_count, output_headers = convert_csv_file(
                job, output_path, node_id_to_labels=node_id_to_labels
            )
        except Exception as error:
            print(f"❌ Failed converting {job.source_path.name}: {error}")
            return 1

        if job.kind == "node":
            converted_nodes += 1
        else:
            converted_edges += 1

        print(
            f"  ✅ {job.source_path.name} -> {job.output_name} ({row_count} rows, header={','.join(output_headers)})"
        )

    print(
        f"Done. Converted {converted_nodes} node file(s) and {converted_edges} edge file(s) into {output_dir}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
