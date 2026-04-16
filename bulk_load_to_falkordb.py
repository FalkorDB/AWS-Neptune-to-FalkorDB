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

Example:
  python3 bulk_load_to_falkordb.py MYGRAPH --csv-dir ./out --server-url redis://127.0.0.1:6379

Pass-through arguments:
  Any additional arguments after the known flags will be forwarded to the selected
  bulk loader command.
  Example:
    python3 bulk_load_to_falkordb.py MYGRAPH --csv-dir ./out -- --skip-invalid-edges
"""

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set


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

def _format_cmd_for_print(cmd: List[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


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


def _build_relation_update_query(
    *,
    relation_type: str,
    id_property: str,
    properties: List[str],
    property_types: Dict[str, str],
    row_var: str = "row",
) -> str:
    rel_type_q = _cypher_quote_identifier(relation_type)
    id_q = _cypher_quote_identifier(id_property)

    query_parts: List[str] = [
        f"MATCH (src {{{id_q}: {row_var}[0]}}), (dst {{{id_q}: {row_var}[1]}})",
        f"MERGE (src)-[r:{rel_type_q}]->(dst)",
    ]
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

    manifest_enforce_schema = bool(manifest.get("options", {}).get("enforce_schema", False))
    enforce_schema = (
        args.enforce_schema if args.enforce_schema is not None else manifest_enforce_schema
    )
    # Forward any remaining CLI args to the selected bulk loader command
    # (strip leading '--' if user used it as a separator).
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]

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
                "In --mode update, this wrapper auto-generates --csv/--query for each file. "
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
            query = _build_node_update_query(
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

        for r in relations:
            rel_type = r.get("type")
            file_name = r.get("file")
            if not rel_type or not file_name:
                raise RuntimeError(f"Invalid relation entry in manifest: {r}")

            file_path = str(csv_dir / file_name)
            properties = list(r.get("properties", []) or [])
            property_types = dict(r.get("property_types", {}) or {})
            query = _build_relation_update_query(
                relation_type=str(rel_type),
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

    if args.dry_run:
        for cmd in commands:
            print(_format_cmd_for_print(cmd))
        return

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
