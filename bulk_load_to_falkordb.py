#!/usr/bin/env python3
"""Bulk-load converted Neptune CSVs into FalkorDB.

This script expects an output directory produced by neptune_to_falkordb_converter.py
that contains:
- bulk_loader_manifest.json
- nodes_*.csv
- edges_*.csv
It can run in one of two modes:
- insert (default): invokes bulk_insert.py with the correct -N / -R arguments.
- update: invokes bulk_update.py for each generated CSV using auto-generated Cypher.
  You can also provide per-file Cypher using --update-queries-csv.

Example:
  python3 bulk_load_to_falkordb.py MYGRAPH --csv-dir ./out --server-url redis://127.0.0.1:6379

Pass-through arguments:
  Any additional arguments after the known flags will be forwarded to the selected
  bulk loader command.
  Example:
    python3 bulk_load_to_falkordb.py MYGRAPH --csv-dir ./out -- --skip-invalid-edges
"""

import argparse
import csv
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set


def _load_manifest(csv_dir: Path) -> Dict[str, Any]:
    manifest_path = csv_dir / "bulk_loader_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _iter_node_labels_from_manifest(manifest: Dict[str, Any]) -> Iterable[str]:
    # Preferred: the converter writes a summary list of all labels.
    labels = manifest.get("summary", {}).get("node_labels")
    if isinstance(labels, list) and labels:
        for l in labels:
            if isinstance(l, str) and l:
                yield l
        return

    # Fallback: collect labels from each node file entry.
    seen: Set[str] = set()
    nodes = manifest.get("output", {}).get("nodes", [])
    for n in nodes:
        for l in n.get("labels", []) or []:
            if isinstance(l, str) and l and l not in seen:
                seen.add(l)
                yield l


def _cypher_quote_identifier(name: str) -> str:
    # Backtick quoting for labels/properties (escape any backticks by doubling them).
    return "`" + name.replace("`", "``") + "`"

def _cypher_label_expression(label_value: str) -> str:
    labels = [segment.strip() for segment in str(label_value).split(":") if segment.strip()]
    return "".join(f":{_cypher_quote_identifier(label)}" for label in labels)


def _format_cmd_for_print(cmd: List[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)

def _get_command_arg_value(cmd: List[str], *arg_names: str) -> Optional[str]:
    for i, part in enumerate(cmd):
        if part in arg_names and i + 1 < len(cmd):
            return cmd[i + 1]
    return None


def _scan_issue_snippet(value: str, max_len: int = 120) -> str:
    shown = (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    if len(shown) <= max_len:
        return shown
    return shown[: max_len - 3] + "..."


def _extract_update_csv_parsing_settings(passthrough: List[str]) -> tuple[str, bool]:
    separator = ","
    no_header = False

    i = 0
    while i < len(passthrough):
        arg = passthrough[i]

        if arg in ("--separator", "-o"):
            if i + 1 >= len(passthrough):
                raise RuntimeError("Missing value for --separator/-o in passthrough arguments")
            separator = passthrough[i + 1]
            i += 2
            continue

        if arg.startswith("--separator="):
            separator = arg.split("=", 1)[1]
            i += 1
            continue

        if arg in ("--no-header", "-n"):
            no_header = True
            i += 1
            continue

        i += 1

    if separator == "":
        raise RuntimeError("Invalid empty CSV separator for update mode")

    return separator, no_header


def _run_update_preflight_scan(
    *,
    scan_jobs: List[Dict[str, Any]],
    separator: str,
    no_header: bool,
    fail_on_warning: bool,
    max_issues_per_file: int,
) -> None:
    from scan_bulk_update_csv_risks import scan_file

    print(
        "Running pre-flight CSV scan for update mode "
        f"(files={len(scan_jobs)}, separator={separator!r}, no_header={no_header})..."
    )

    total_errors = 0
    total_warnings = 0

    for job in scan_jobs:
        file_path: Path = job["file_path"]
        file_name: str = job["file_name"]
        expected_columns: int = int(job["expected_columns"])

        issues, rows_scanned, expected = scan_file(
            path=file_path,
            separator=separator,
            no_header=no_header,
            expected_columns_override=expected_columns,
        )

        errors = [issue for issue in issues if issue.severity == "ERROR"]
        warnings = [issue for issue in issues if issue.severity == "WARN"]
        total_errors += len(errors)
        total_warnings += len(warnings)

        if not errors and not warnings:
            print(
                f"  ✅ {file_name}: rows={rows_scanned}, expected_columns={expected} "
                "(no issues)"
            )
            continue

        print(
            f"  ⚠️  {file_name}: rows={rows_scanned}, expected_columns={expected}, "
            f"ERROR={len(errors)}, WARN={len(warnings)}"
        )

        report_issues = errors + (warnings if fail_on_warning else [])
        if report_issues:
            for issue in report_issues[:max_issues_per_file]:
                location = f"row {issue.row}"
                if issue.column is not None:
                    location += f", col {issue.column}"
                print(
                    f"      [{issue.severity}] {issue.code} at {location}: {issue.message} "
                    f"(value='{_scan_issue_snippet(issue.value)}')"
                )
            if len(report_issues) > max_issues_per_file:
                print(
                    f"      ... {len(report_issues) - max_issues_per_file} additional "
                    "issue(s) omitted for brevity"
                )

    if total_errors > 0 or (fail_on_warning and total_warnings > 0):
        failure_basis = (
            f"{total_errors} error(s)"
            if total_errors > 0
            else f"{total_warnings} warning(s) with fail-on-warning enabled"
        )
        raise RuntimeError(
            "Update-mode pre-flight CSV scan failed "
            f"({failure_basis}). Fix the reported issues before running update mode, "
            "or bypass with --skip-update-preflight-scan."
        )

    if total_warnings > 0:
        print(
            f"Pre-flight CSV scan completed with warnings only "
            f"(WARN={total_warnings}, ERROR={total_errors}). Proceeding with update mode."
        )
    else:
        print("Pre-flight CSV scan passed with no issues.")


def _load_update_queries_csv(path: Path) -> Dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"Update queries CSV not found: {path}")

    update_queries: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        for line_number, row in enumerate(reader, start=1):
            if not row or all(not str(col).strip() for col in row):
                continue

            file_name = str(row[0]).strip()
            query = ",".join(str(col) for col in row[1:]).strip()
            if (
                line_number == 1
                and file_name.lower() in {"file", "file_name", "filename", "input_file", "input_filename"}
                and query.lower() in {"query", "merge_query", "cypher_query", "cypher", "merge"}
            ):
                continue

            if not file_name:
                raise RuntimeError(
                    f"Invalid update queries CSV row at line {line_number}: missing input file name in column 1"
                )
            if not query:
                raise RuntimeError(
                    f"Invalid update queries CSV row at line {line_number}: missing Cypher query in column 2"
                )
            if file_name in update_queries:
                raise RuntimeError(
                    f"Duplicate input file entry in update queries CSV at line {line_number}: {file_name}"
                )
            update_queries[file_name] = query

    if not update_queries:
        raise RuntimeError(f"Update queries CSV has no usable mappings: {path}")

    return update_queries


def _resolve_custom_update_query(
    *, file_name: str, update_queries: Dict[str, str], used_update_query_keys: Set[str]
) -> Optional[str]:
    if not update_queries:
        return None

    if file_name in update_queries:
        used_update_query_keys.add(file_name)
        return update_queries[file_name]

    base_name = Path(file_name).name
    if base_name in update_queries:
        used_update_query_keys.add(base_name)
        return update_queries[base_name]

    return None


def _build_property_set_clauses(
    *,
    entity_var: str,
    row_var: str,
    start_index: int,
    properties: List[str],
    property_types: Dict[str, str],
) -> List[str]:
    clauses: List[str] = []

    for offset, prop in enumerate(properties):
        row_idx = start_index + offset
        prop_q = _cypher_quote_identifier(prop)
        raw_value_expr = f"{row_var}[{row_idx}]"
        prop_type = (property_types or {}).get(prop)

        if prop_type == "INT":
            value_expr = f"toInteger({raw_value_expr})"
        elif prop_type == "DOUBLE":
            value_expr = f"toFloat({raw_value_expr})"
        elif prop_type == "BOOL":
            value_expr = f"toBoolean({raw_value_expr})"
        else:
            # STRING/ARRAY/unknown fallback: keep as-is.
            value_expr = raw_value_expr

        clauses.append(
            " ".join(
                [
                    "FOREACH (_ IN CASE WHEN",
                    f"{raw_value_expr} <> ''",
                    "THEN [1] ELSE [] END |",
                    f"SET {entity_var}.{prop_q} = {value_expr})",
                ]
            )
        )

    return clauses


def _build_node_update_query(
    *,
    labels: List[str],
    id_property: str,
    properties: List[str],
    property_types: Dict[str, str],
    row_var: str = "row",
) -> str:
    if not labels:
        raise RuntimeError("Node manifest entry has no labels for update mode")

    label_expr = "".join(f":{_cypher_quote_identifier(label)}" for label in labels)
    id_q = _cypher_quote_identifier(id_property)

    query_parts: List[str] = [f"MERGE (n{label_expr} {{{id_q}: {row_var}[0]}})"]
    query_parts.extend(
        _build_property_set_clauses(
            entity_var="n",
            row_var=row_var,
            start_index=1,
            properties=properties,
            property_types=property_types,
        )
    )
    return " ".join(query_parts)

def _find_property_row_index(
    *, properties: List[str], property_name: str, start_index: int
) -> Optional[int]:
    try:
        return start_index + properties.index(property_name)
    except ValueError:
        return None


def _detect_fixed_relation_endpoint_labels(
    *,
    csv_path: Path,
    separator: str,
    no_header: bool,
    source_label_row_index: Optional[int],
    target_label_row_index: Optional[int],
) -> tuple[Optional[str], Optional[str]]:
    if source_label_row_index is None or target_label_row_index is None:
        return None, None

    source_label: Optional[str] = None
    target_label: Optional[str] = None

    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(
            f,
            delimiter=separator,
            skipinitialspace=True,
            quoting=csv.QUOTE_NONE,
            escapechar="\\",
        )

        if not no_header:
            next(reader, None)

        for row in reader:
            max_required_index = max(source_label_row_index, target_label_row_index)
            if len(row) <= max_required_index:
                continue

            row_source_label = str(row[source_label_row_index]).strip()
            row_target_label = str(row[target_label_row_index]).strip()
            if not row_source_label or not row_target_label:
                continue

            if source_label is None:
                source_label = row_source_label
            elif source_label != row_source_label:
                return None, None

            if target_label is None:
                target_label = row_target_label
            elif target_label != row_target_label:
                return None, None

    if source_label and target_label:
        return source_label, target_label
    return None, None



def _build_relation_update_query(
    *,
    relation_type: str,
    id_property: str,
    properties: List[str],
    property_types: Dict[str, str],
    source_label: Optional[str] = None,
    target_label: Optional[str] = None,
    source_label_row_index: Optional[int] = None,
    target_label_row_index: Optional[int] = None,
    row_var: str = "row",
) -> str:
    rel_type_q = _cypher_quote_identifier(relation_type)
    id_q = _cypher_quote_identifier(id_property)
    source_label_expr = _cypher_label_expression(source_label) if source_label else ""
    target_label_expr = _cypher_label_expression(target_label) if target_label else ""

    if source_label_expr and target_label_expr:
        query_parts: List[str] = [
            f"MATCH (src{source_label_expr} {{{id_q}: {row_var}[0]}}), "
            f"(dst{target_label_expr} {{{id_q}: {row_var}[1]}})",
            f"MERGE (src)-[r:{rel_type_q}]->(dst)",
        ]
    else:
        query_parts = [f"MATCH (src {{{id_q}: {row_var}[0]}}), (dst {{{id_q}: {row_var}[1]}})"]
        if source_label_row_index is not None and target_label_row_index is not None:
            query_parts.append(
                f"WHERE {row_var}[{source_label_row_index}] IN labels(src) "
                f"AND {row_var}[{target_label_row_index}] IN labels(dst)"
            )
        query_parts.append(f"MERGE (src)-[r:{rel_type_q}]->(dst)")
    query_parts.extend(
        _build_property_set_clauses(
            entity_var="r",
            row_var=row_var,
            start_index=2,
            properties=properties,
            property_types=property_types,
        )
    )
    return " ".join(query_parts)


def _create_node_id_indexes(
    *,
    graph_name: str,
    server_url: str,
    labels: List[str],
    id_property: str,
) -> None:
    # Import lazily so the wrapper can still be used in --dry-run mode without deps.
    try:
        from redis.exceptions import ResponseError
        from falkordb import FalkorDB
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "Index creation requires the Python packages 'falkordb' and 'redis'. "
            "Install them (e.g. pip install falkordb redis) or re-run without --create-id-indexes."
        ) from e

    if not labels:
        return

    client = FalkorDB.from_url(server_url)
    graph = client.select_graph(graph_name)

    print(f"Creating node ID indexes on property '{id_property}' for {len(labels)} label(s)...")

    # Creating an already-existing index raises a ResponseError; treat that as success.
    for label in labels:
        q = (
            f"CREATE INDEX FOR (n:{_cypher_quote_identifier(label)}) "
            f"ON (n.{_cypher_quote_identifier(id_property)})"
        )
        try:
            graph.query(q)
            print(f"  created :{label}({id_property})")
        except ResponseError as e:
            msg = str(e).lower()
            if any(
                s in msg
                for s in (
                    "already exists",
                    "equivalent index already exists",
                    "already indexed",
                    "index exists",
                )
            ):
                print(f"  exists  :{label}({id_property})")
                continue
            raise


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
        "--mode",
        choices=["insert", "update"],
        default="insert",
        help=(
            "Loading mode: insert uses bulk_insert.py for new graph creation; "
            "update uses bulk_update.py against an existing graph"
        ),
    )
    parser.add_argument(
        "--update-query",
        "--merge-query",
        dest="update_query",
        default=None,
        help=(
            "Custom Cypher query body for --mode update. "
            "The query should reference row values as row[index]. "
            "Example: \"MERGE (n:Device {id: row[0]}) SET n.name = row[1]\". "
            "If provided, it overrides the wrapper's auto-generated update query."
        ),
    )
    parser.add_argument(
        "--update-queries-csv",
        default=None,
        help=(
            "CSV file for --mode update with per-file query overrides. "
            "Column 1: input file name. Column 2: MERGE/Cypher query body. "
            "Mappings here take precedence over --update-query."
        ),
    )
    parser.add_argument(
        "--create-id-indexes",
        action="store_true",
        help=(
            "Create node range indexes on the ID property after load. "
            "This requires the optional Python packages 'falkordb' and 'redis'."
        ),
    )
    parser.add_argument(
        "--id-property",
        default="id",
        help="Node property name to index per label (default: id)",
    )
    parser.add_argument(
        "--skip-update-preflight-scan",
        action="store_true",
        help="Skip CSV risk scan pre-flight in --mode update (not recommended).",
    )
    parser.add_argument(
        "--update-preflight-fail-on-warning",
        action="store_true",
        help=(
            "In --mode update, treat scan warnings as fatal "
            "(default: only scan errors are fatal)."
        ),
    )
    parser.add_argument(
        "--update-preflight-max-issues",
        type=int,
        default=20,
        help="Maximum scan issues to print per file in --mode update (default: 20).",
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
    if args.update_query and args.mode != "update":
        parser.error("--update-query/--merge-query can only be used with --mode update")
    if args.update_queries_csv and args.mode != "update":
        parser.error("--update-queries-csv can only be used with --mode update")
    if args.update_preflight_max_issues <= 0:
        parser.error("--update-preflight-max-issues must be a positive integer")

    csv_dir = Path(args.csv_dir)
    manifest = _load_manifest(csv_dir)
    update_queries = (
        _load_update_queries_csv(Path(args.update_queries_csv)) if args.update_queries_csv else {}
    )
    used_update_query_keys: Set[str] = set()

    bulk_loader_dir = Path(args.bulk_loader_dir)
    loader_script = "bulk_insert.py" if args.mode == "insert" else "bulk_update.py"
    bulk_loader_py = bulk_loader_dir / "falkordb_bulk_loader" / loader_script
    if not bulk_loader_py.exists():
        raise FileNotFoundError(
            f"{loader_script} not found at {bulk_loader_py} (set --bulk-loader-dir accordingly)"
        )

    nodes: List[Dict[str, Any]] = manifest.get("output", {}).get("nodes", [])
    relations: List[Dict[str, Any]] = manifest.get("output", {}).get("relations", [])
    if args.mode == "insert" and not nodes:
        raise RuntimeError("Manifest does not include any node files")
    if args.mode == "update" and not nodes and not relations:
        raise RuntimeError("Manifest does not include any node or relation files")

    commands: List[List[str]] = []
    update_scan_jobs: List[Dict[str, Any]] = []

    manifest_enforce_schema = bool(manifest.get("options", {}).get("enforce_schema", False))
    enforce_schema = (
        args.enforce_schema if args.enforce_schema is not None else manifest_enforce_schema
    )
    # Forward any remaining CLI args to the selected bulk loader command
    # (strip leading '--' if user used it as a separator).
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]
    update_separator = ","
    update_no_header = False
    if args.mode == "update":
        update_separator, update_no_header = _extract_update_csv_parsing_settings(passthrough)

    if args.mode == "insert":
        cmd: List[str] = [sys.executable, str(bulk_loader_py), args.graph, "-u", args.server_url]
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

        cmd.extend(passthrough)
        commands.append(cmd)
    else:
        forbidden_update_passthrough = {"--csv", "-c", "--query", "-q", "--variable-name", "-v"}
        forbidden_seen = sorted(arg for arg in passthrough if arg in forbidden_update_passthrough)
        if forbidden_seen:
            raise RuntimeError(
                "In --mode update, this wrapper manages --csv/--query/--variable-name per file. "
                "Use --update-query (or --merge-query) for a custom Cypher query. "
                f"Do not pass these arguments via passthrough: {', '.join(forbidden_seen)}"
            )

        for n in nodes:
            labels = n.get("labels")
            file_name = n.get("file")
            if not labels or not file_name:
                raise RuntimeError(f"Invalid node entry in manifest: {n}")

            file_path = str(csv_dir / file_name)
            properties = list(n.get("properties", []) or [])
            property_types = dict(n.get("property_types", {}) or {})
            query = _resolve_custom_update_query(
                file_name=file_name,
                update_queries=update_queries,
                used_update_query_keys=used_update_query_keys,
            ) or args.update_query or _build_node_update_query(
                labels=list(labels),
                id_property=args.id_property,
                properties=properties,
                property_types=property_types,
            )
            cmd = [
                sys.executable,
                str(bulk_loader_py),
                args.graph,
                "-u",
                args.server_url,
                "-c",
                file_path,
                "-q",
                query,
            ]
            cmd.extend(passthrough)
            commands.append(cmd)
            update_scan_jobs.append(
                {
                    "file_name": str(file_name),
                    "file_path": csv_dir / str(file_name),
                    "expected_columns": 1 + len(properties),
                }
            )

        for r in relations:
            rel_type = r.get("type")
            file_name = r.get("file")
            if not rel_type or not file_name:
                raise RuntimeError(f"Invalid relation entry in manifest: {r}")

            file_path = str(csv_dir / file_name)
            properties = list(r.get("properties", []) or [])
            property_types = dict(r.get("property_types", {}) or {})
            resolved_query = _resolve_custom_update_query(
                file_name=file_name,
                update_queries=update_queries,
                used_update_query_keys=used_update_query_keys,
            )
            if resolved_query:
                query = resolved_query
            elif args.update_query:
                query = args.update_query
            else:
                source_label_row_index = _find_property_row_index(
                    properties=properties,
                    property_name="source_label",
                    start_index=2,
                )
                target_label_row_index = _find_property_row_index(
                    properties=properties,
                    property_name="target_label",
                    start_index=2,
                )
                fixed_source_label, fixed_target_label = _detect_fixed_relation_endpoint_labels(
                    csv_path=csv_dir / str(file_name),
                    separator=update_separator,
                    no_header=update_no_header,
                    source_label_row_index=source_label_row_index,
                    target_label_row_index=target_label_row_index,
                )
                query = _build_relation_update_query(
                    relation_type=str(rel_type),
                    id_property=args.id_property,
                    properties=properties,
                    property_types=property_types,
                    source_label=fixed_source_label,
                    target_label=fixed_target_label,
                    source_label_row_index=source_label_row_index,
                    target_label_row_index=target_label_row_index,
                )
            cmd = [
                sys.executable,
                str(bulk_loader_py),
                args.graph,
                "-u",
                args.server_url,
                "-c",
                file_path,
                "-q",
                query,
            ]
            cmd.extend(passthrough)
            commands.append(cmd)
            update_scan_jobs.append(
                {
                    "file_name": str(file_name),
                    "file_path": csv_dir / str(file_name),
                    "expected_columns": 2 + len(properties),
                }
            )

    if args.mode == "update" and update_queries:
        unused_update_query_keys = sorted(set(update_queries.keys()) - used_update_query_keys)
        if unused_update_query_keys:
            raise RuntimeError(
                "Some entries in --update-queries-csv did not match any manifest node/relation file: "
                + ", ".join(unused_update_query_keys)
            )
    if args.mode == "update":
        if args.skip_update_preflight_scan:
            print("Skipping pre-flight CSV scan for update mode (--skip-update-preflight-scan).")
        else:
            _run_update_preflight_scan(
                scan_jobs=update_scan_jobs,
                separator=update_separator,
                no_header=update_no_header,
                fail_on_warning=args.update_preflight_fail_on_warning,
                max_issues_per_file=args.update_preflight_max_issues,
            )

    if args.dry_run:
        for cmd in commands:
            print(_format_cmd_for_print(cmd))
        return
    if args.mode == "insert":
        total_input_files = len(nodes) + len(relations)
        if total_input_files > 0:
            print(f"Loading {total_input_files} input file(s) in insert mode:")
            file_index = 1
            for n in nodes:
                file_name = n.get("file")
                print(f"  [{file_index}/{total_input_files}] loading node file: {file_name}")
                file_index += 1
            for r in relations:
                file_name = r.get("file")
                print(f"  [{file_index}/{total_input_files}] loading edge file: {file_name}")
                file_index += 1

    total_commands = len(commands)
    for command_index, cmd in enumerate(commands, start=1):
        if args.mode == "update" and total_commands > 1:
            csv_path = _get_command_arg_value(cmd, "-c", "--csv")
            csv_display = Path(csv_path).name if csv_path else "<unknown>"
            print(f"[{command_index}/{total_commands}] loading input file: {csv_display}")
    for cmd in commands:
        subprocess.run(cmd, check=True)

    if args.create_id_indexes:
        labels = sorted(set(_iter_node_labels_from_manifest(manifest)))
        _create_node_id_indexes(
            graph_name=args.graph,
            server_url=args.server_url,
            labels=labels,
            id_property=args.id_property,
        )


if __name__ == "__main__":
    main()
