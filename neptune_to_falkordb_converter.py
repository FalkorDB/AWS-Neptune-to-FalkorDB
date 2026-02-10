#!/usr/bin/env python3
"""Neptune Export Service -> FalkorDB (bulk-loader) CSV converter.

This script converts Amazon Neptune Export Service CSV files into CSVs that can be
loaded directly by the FalkorDB bulk loader (see ../falkordb-bulk-loader).

Bulk loader schemaless CSV expectations:
- Node CSVs: the first column is the unique node identifier, all remaining
  columns are node properties. (Label is supplied by the loader CLI, not the
  file contents.)
- Relation CSVs: the first two columns are start and end node identifiers, all
  remaining columns are relation properties. (Relation type is supplied by the
  loader CLI.)

This converter:
- Produces separate node files and relation files.
- Avoids duplicating nodes across multiple label files by grouping nodes by their
  *full label set* and generating a single file per unique label-set.
- Writes a manifest file (bulk_loader_manifest.json) that an import helper script
  can use to invoke the bulk loader with the correct -N/-R arguments.
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Set, Any, Optional, Tuple
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class NeptuneToFalkorDBConverter:
    MANIFEST_FILENAME = "bulk_loader_manifest.json"

    def __init__(self, input_dir: str, output_dir: str, enforce_schema: bool = False):
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # If True, emit Neo4j-style schema headers (ID/START_ID/END_ID and typed properties)
        # compatible with falkordb-bulk-loader's --enforce-schema mode.
        self.enforce_schema = bool(enforce_schema)

        # Track all property names and labels/types
        self.node_properties: Set[str] = set()
        self.edge_properties: Set[str] = set()
        self.node_labels: Set[str] = set()
        self.edge_types: Set[str] = set()

        # Track node ID -> labels from Neptune (useful for reporting/debugging)
        self.node_id_to_labels: Dict[str, List[str]] = {}

        # Populated during conversion
        self._generated_node_files: List[Dict[str, Any]] = []
        self._generated_edge_files: List[Dict[str, Any]] = []

    @staticmethod
    def _safe_filename_part(s: str) -> str:
        # Keep it simple and cross-platform; also avoids characters with special meaning.
        return (
            s.replace("/", "_")
            .replace("\\", "_")
            .replace(":", "_")
            .replace("*", "_")
            .replace("?", "_")
            .replace('"', "_")
            .replace("<", "_")
            .replace(">", "_")
            .replace("|", "_")
            .strip()
        )

    @classmethod
    def _labelset_filename(cls, labels: Tuple[str, ...]) -> str:
        # We keep the 'nodes_' prefix to avoid collisions with relation type filenames.
        joined = "__".join(cls._safe_filename_part(l) for l in labels)
        return f"nodes_{joined}.csv"

    @classmethod
    def _edgetype_filename(cls, edge_type: str) -> str:
        safe_type = cls._safe_filename_part(edge_type)
        return f"edges_{safe_type}.csv"
    def find_neptune_files(self) -> Dict[str, List[Path]]:
        """Find Neptune export files in the input directory.
        
        Returns a dict with 'node_files' and 'edge_files' lists.
        Neptune export format creates separate CSV files for different node types and edge types.
        """
        files = {'node_files': [], 'edge_files': []}
        
        # First, look for standard Neptune export patterns
        standard_patterns = {
            'vertices': ['vertices.csv', 'nodes.csv', 'vertex.csv'],
            'edges': ['edges.csv', 'relationships.csv', 'edge.csv'],
            'schema': ['schema.json', 'metadata.json']
        }
        
        standard_files = {}
        for file_type, pattern_list in standard_patterns.items():
            for pattern in pattern_list:
                file_path = self.input_dir / pattern
                if file_path.exists():
                    standard_files[file_type] = file_path
                    break
        
        # If we found standard format, use it
        if standard_files.get('vertices'):
            files['node_files'] = [standard_files['vertices']]
        if standard_files.get('edges'):
            files['edge_files'] = [standard_files['edges']]
        
        # If no standard files, look for Neptune CSV export format (separate files)
        if not files['node_files'] and not files['edge_files']:
            csv_files = list(self.input_dir.glob('*.csv'))
            logger.info(f"Found CSV files: {[f.name for f in csv_files]}")
            
            # Classify files based on filename patterns and headers
            for csv_file in csv_files:
                try:
                    with open(csv_file, 'r', encoding='utf-8') as f:
                        # Handle pipe-delimited files (Neptune export format)
                        first_line = f.readline().strip()
                        
                        # Try pipe delimiter first (Neptune export format)
                        if '|' in first_line:
                            headers = [h.strip('"') for h in first_line.split('|')]
                        else:
                            # Fall back to comma delimiter
                            f.seek(0)
                            headers = next(csv.reader(f))
                        
                        # Determine if it's a node file or edge file
                        filename = csv_file.name.lower()
                        
                        # Edge file patterns
                        if ('edge' in filename or '_edges' in filename or 
                            'relationship' in filename or 'follows' in filename or 
                            'likes' in filename or 'mentions' in filename or 
                            'retweets' in filename or 'tweets' in filename):
                            
                            # Verify it has edge-like structure (~from, ~to, or source, target)
                            if (any(h in ['~from', '~to', 'source', 'target', 'from', 'to'] for h in headers) or
                                any('from' in h.lower() or 'to' in h.lower() for h in headers)):
                                files['edge_files'].append(csv_file)
                                logger.debug(f"Classified {csv_file.name} as edge file (headers: {headers[:5]})")
                            else:
                                logger.debug(f"File {csv_file.name} looks like edge file but missing edge headers")
                        
                        # Node file patterns (anything that's not clearly an edge file)
                        elif (any(h in ['~id', 'id', 'vertex_id'] for h in headers) and
                              not any(h in ['~from', '~to', 'source', 'target', 'from', 'to'] for h in headers)):
                            files['node_files'].append(csv_file)
                            logger.debug(f"Classified {csv_file.name} as node file (headers: {headers[:5]})")
                            
                except Exception as e:
                    logger.warning(f"Could not read headers from {csv_file}: {e}")
        
        logger.info(f"Found {len(files['node_files'])} node files: {[f.name for f in files['node_files']]}")
        logger.info(f"Found {len(files['edge_files'])} edge files: {[f.name for f in files['edge_files']]}")
        
        return files
    
    def parse_neptune_property_value(self, value: str) -> Any:
        """Parse Neptune property value which might be JSON encoded."""
        if not value or value == '':
            return None
        
        # Neptune often stores complex values as JSON strings
        if value.startswith('{') or value.startswith('['):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass
        
        # Try to parse as number
        try:
            if '.' in value:
                return float(value)
            return int(value)
        except ValueError:
            pass
        
        # Boolean values
        if value.lower() in ['true', 'false']:
            return value.lower() == 'true'
        
        return value

    @staticmethod
    def _infer_schema_type(values: List[Any]) -> str:
        """Infer a safe bulk-loader schema type for a property column.

        We infer conservatively to avoid --enforce-schema failures:
        - BOOL only if all non-empty values are booleans
        - ARRAY only if all non-empty values are lists
        - DOUBLE if all non-empty values are numeric (int/float) and at least one is float
        - INT if all non-empty values are ints
        - otherwise STRING
        """
        non_null = [v for v in values if v is not None]
        if not non_null:
            return "STRING"

        # bool is a subclass of int in Python; check it first.
        if all(isinstance(v, bool) for v in non_null):
            return "BOOL"

        if all(isinstance(v, list) for v in non_null):
            return "ARRAY"

        numeric_types = (int, float)
        if all(isinstance(v, numeric_types) and not isinstance(v, bool) for v in non_null):
            if any(isinstance(v, float) for v in non_null):
                return "DOUBLE"
            return "INT"

        return "STRING"

    @staticmethod
    def _format_value_for_schema(value: Any, schema_type: str) -> str:
        if value is None:
            return ""

        if schema_type == "BOOL":
            # bulk loader accepts true/false case-insensitively; keep it canonical.
            return "true" if bool(value) else "false"

        if schema_type == "ARRAY":
            if isinstance(value, list):
                return json.dumps(value)
            # If the column was marked ARRAY but we got a non-list value, stringify it.
            # (This is still bracketed? No, but better than crashing during conversion.)
            return str(value)

        if schema_type in ("INT", "DOUBLE"):
            return str(value)

        # STRING
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def extract_labels_from_neptune_row(self, row: Dict[str, str]) -> List[str]:
        """Extract node labels from Neptune row."""
        labels = []
        
        # Common Neptune label columns
        label_columns = ['~label', 'label', 'labels', '~labels']
        
        for col in label_columns:
            if col in row and row[col]:
                value = row[col]
                if value.startswith('[') and value.endswith(']'):
                    # Parse as JSON array
                    try:
                        labels.extend(json.loads(value))
                    except json.JSONDecodeError:
                        # Split by comma if not valid JSON
                        labels.extend([l.strip().strip('"\'') for l in value[1:-1].split(',')])
                else:
                    # Handle semicolon-separated labels (common in Neptune)
                    if ';' in value:
                        labels.extend([l.strip() for l in value.split(';')])
                    # Handle comma-separated labels  
                    elif ',' in value:
                        labels.extend([l.strip() for l in value.split(',')])
                    else:
                        # Single label
                        labels.append(value.strip())
                break
        
        # If no explicit label column, check for ~label columns with specific values
        if not labels:
            for col, value in row.items():
                if col.startswith('~label') and value:
                    labels.append(value)
        
        return [l for l in labels if l]  # Remove empty strings
    
    def convert_nodes(self, node_files: List[Path]) -> None:
        """Convert Neptune vertices to bulk-loader compatible node CSVs.

        We must not emit the same node ID in multiple node files, as the bulk loader
        requires node identifiers to be unique across all node inputs.

        To preserve multi-label semantics, nodes are grouped by their *full label set*
        and a node appears in exactly one output file.
        """
        logger.info(f"Converting nodes from {len(node_files)} files: {[f.name for f in node_files]}")

        # Accumulate a single, merged record per node ID across all Neptune node inputs.
        # This allows multi-label nodes that appear in multiple input files to be grouped
        # into a single output file with the combined label set.
        nodes_by_id: Dict[str, Dict[str, Any]] = {}

        # Process each node file
        for vertices_file in node_files:
            logger.info(f"Processing node file: {vertices_file.name}")

            with open(vertices_file, 'r', encoding='utf-8') as f:
                first_line = f.readline().strip()
                f.seek(0)

                # Detect delimiter - check if it's the line number format (1|header,data,data)
                logger.debug(f"Delimiter detection for {vertices_file.name}: first_line={repr(first_line)}")
                logger.debug(
                    f"Detection conditions: startswith_1_pipe={first_line.startswith('1|')}, has_comma={',' in first_line}, has_pipe={'|' in first_line}"
                )

                if first_line.startswith('1|') and ',' in first_line:
                    logger.debug(f"Using line number removal logic for {vertices_file.name}")
                    lines = f.readlines()
                    f.seek(0)
                    import io

                    cleaned_content = io.StringIO()
                    for line in lines:
                        if '|' in line and line[0].isdigit():
                            cleaned_line = line[line.find('|') + 1 :]
                            cleaned_content.write(cleaned_line)
                        else:
                            cleaned_content.write(line)
                    cleaned_content.seek(0)
                    reader = csv.DictReader(cleaned_content)
                elif '|' in first_line and not first_line.startswith('1|'):
                    logger.debug(f"Using pure pipe-delimited logic for {vertices_file.name}")
                    reader = csv.DictReader(f, delimiter='|')
                else:
                    logger.debug(f"Using standard comma-delimited logic for {vertices_file.name}")
                    reader = csv.DictReader(f)

                logger.debug(f"Headers for {vertices_file.name}: {reader.fieldnames}")

                for row in reader:
                    labels = self.extract_labels_from_neptune_row(row)
                    if not labels:
                        labels = ["UnlabeledNode"]

                    labels_set = {l for l in labels if l}
                    if not labels_set:
                        labels_set = {"UnlabeledNode"}

                    node_id = row.get('~id') or row.get('id') or row.get('vertex_id')
                    if not node_id:
                        logger.warning(f"No ID found for row: {row}")
                        continue

                    node_id = str(node_id)

                    # Initialize merged record if needed
                    if node_id not in nodes_by_id:
                        nodes_by_id[node_id] = {"labels": set(), "properties": {}}

                    # Merge labels
                    nodes_by_id[node_id]["labels"].update(labels_set)
                    self.node_labels.update(labels_set)

                    # Merge properties
                    merged_props: Dict[str, str] = nodes_by_id[node_id]["properties"]
                    for col, value in row.items():
                        if col.startswith('~') or col in ['id', 'label', 'labels']:
                            continue
                        if value is None or value == "":
                            continue

                        clean_col = col.split(':')[0] if ':' in col else col
                        self.node_properties.add(clean_col)

                        # Prefer the first non-empty value; log conflicts at debug level.
                        if clean_col not in merged_props:
                            merged_props[clean_col] = value
                        elif merged_props[clean_col] != value:
                            logger.debug(
                                f"Conflicting values for node '{node_id}' property '{clean_col}': "
                                f"keeping '{merged_props[clean_col]}', ignoring '{value}'"
                            )

        # Group merged nodes by their final label set
        labelset_properties: Dict[Tuple[str, ...], Set[str]] = {}
        labelset_data: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}

        for node_id, node in nodes_by_id.items():
            labels_key = tuple(sorted(node.get("labels") or {"UnlabeledNode"}))
            self.node_id_to_labels[node_id] = list(labels_key)

            if labels_key not in labelset_data:
                labelset_data[labels_key] = []
                labelset_properties[labels_key] = set()

            props: Dict[str, str] = node.get("properties", {})
            labelset_data[labels_key].append({"id": node_id, "properties": props})
            labelset_properties[labels_key].update(props.keys())

        # Write one CSV per unique label set
        created_files: List[str] = []
        self._generated_node_files = []

        for labels_key in sorted(labelset_data.keys(), key=lambda t: "::".join(t)):
            out_name = self._labelset_filename(labels_key)
            nodes_output = self.output_dir / out_name

            props = sorted(labelset_properties.get(labels_key, set()))

            prop_types: Dict[str, str] = {}
            if self.enforce_schema:
                for prop in props:
                    values: List[Any] = []
                    for node_data in labelset_data[labels_key]:
                        raw_val = node_data["properties"].get(prop, "")
                        if raw_val == "":
                            continue
                        values.append(self.parse_neptune_property_value(raw_val))
                    prop_types[prop] = self._infer_schema_type(values)

                headers = ["id:ID"] + [f"{prop}:{prop_types[prop]}" for prop in props]
            else:
                headers = ["id"] + props

            with open(nodes_output, 'w', newline='', encoding='utf-8') as out_f:
                writer = csv.writer(out_f)
                writer.writerow(headers)

                for node_data in labelset_data[labels_key]:
                    output_row: List[str] = [node_data["id"]]
                    for prop in props:
                        raw_val = node_data["properties"].get(prop, "")
                        if raw_val == "":
                            output_row.append("")
                            continue

                        parsed_value = self.parse_neptune_property_value(raw_val)
                        if self.enforce_schema:
                            output_row.append(
                                self._format_value_for_schema(parsed_value, prop_types[prop])
                            )
                        else:
                            if isinstance(parsed_value, (dict, list)):
                                output_row.append(json.dumps(parsed_value))
                            else:
                                output_row.append(str(parsed_value))

                    writer.writerow(output_row)

            created_files.append(out_name)
            node_manifest_entry: Dict[str, Any] = {
                "file": out_name,
                "labels": list(labels_key),
                "count": len(labelset_data[labels_key]),
                "properties": props,
            }
            if self.enforce_schema:
                node_manifest_entry["property_types"] = prop_types
            self._generated_node_files.append(node_manifest_entry)

            logger.info(
                f"Created {nodes_output} with {len(labelset_data[labels_key])} nodes (labels={list(labels_key)})"
            )

        logger.info(f"Created {len(created_files)} node files: {created_files}")
        logger.info(f"Found {len(self.node_labels)} unique node labels: {sorted(self.node_labels)}")
        logger.info(f"Found {len(self.node_properties)} unique node properties: {sorted(self.node_properties)}")
    
    def convert_edges(self, edge_files: List[Path]) -> None:
        """Convert Neptune edges to bulk-loader compatible relation CSVs."""
        logger.info(f"Converting edges from {len(edge_files)} files: {[f.name for f in edge_files]}")

        type_properties: Dict[str, Set[str]] = {}
        type_data: Dict[str, List[Dict[str, Any]]] = {}

        for edges_file in edge_files:
            logger.info(f"Processing edge file: {edges_file.name}")

            with open(edges_file, 'r', encoding='utf-8') as f:
                first_line = f.readline().strip()
                f.seek(0)

                logger.debug(f"Delimiter detection for {edges_file.name}: first_line={repr(first_line)}")
                logger.debug(
                    f"Detection conditions: startswith_1_pipe={first_line.startswith('1|')}, has_comma={',' in first_line}, has_pipe={'|' in first_line}"
                )

                if first_line.startswith('1|') and ',' in first_line:
                    logger.debug(f"Using line number removal logic for {edges_file.name}")
                    lines = f.readlines()
                    f.seek(0)
                    import io

                    cleaned_content = io.StringIO()
                    for line in lines:
                        if '|' in line and line[0].isdigit():
                            cleaned_line = line[line.find('|') + 1 :]
                            cleaned_content.write(cleaned_line)
                        else:
                            cleaned_content.write(line)
                    cleaned_content.seek(0)
                    reader = csv.DictReader(cleaned_content)
                elif '|' in first_line and not first_line.startswith('1|'):
                    logger.debug(f"Using pure pipe-delimited logic for {edges_file.name}")
                    reader = csv.DictReader(f, delimiter='|')
                else:
                    logger.debug(f"Using standard comma-delimited logic for {edges_file.name}")
                    reader = csv.DictReader(f)

                logger.debug(f"Headers for {edges_file.name}: {reader.fieldnames}")

                for row in reader:
                    source = row.get('~from') or row.get('source') or row.get('from')
                    target = row.get('~to') or row.get('target') or row.get('to')
                    if not source or not target:
                        logger.warning(f"Missing source or target for edge: {row}")
                        continue

                    edge_type = (
                        row.get('~label')
                        or row.get('label')
                        or row.get('type')
                        or row.get('relationship_type')
                        or 'UnlabeledEdge'
                    )

                    edge_type = str(edge_type)
                    self.edge_types.add(edge_type)

                    edge_properties: Dict[str, str] = {}
                    for col, value in row.items():
                        if col.startswith('~') or col in ['id', 'label', 'type', 'source', 'target', 'from', 'to']:
                            continue
                        if value is None or value == "":
                            continue

                        clean_col = col.split(':')[0] if ':' in col else col
                        self.edge_properties.add(clean_col)
                        edge_properties[clean_col] = value

                    if edge_type not in type_data:
                        type_data[edge_type] = []
                        type_properties[edge_type] = set()

                    type_data[edge_type].append(
                        {
                            "source": str(source),
                            "target": str(target),
                            "properties": edge_properties,
                        }
                    )
                    type_properties[edge_type].update(edge_properties.keys())

        created_files: List[str] = []
        self._generated_edge_files = []

        for edge_type in sorted(type_data.keys()):
            out_name = self._edgetype_filename(edge_type)
            edges_output = self.output_dir / out_name

            props = sorted(type_properties.get(edge_type, set()))

            prop_types: Dict[str, str] = {}
            if self.enforce_schema:
                for prop in props:
                    values: List[Any] = []
                    for edge_data in type_data[edge_type]:
                        raw_val = edge_data["properties"].get(prop, "")
                        if raw_val == "":
                            continue
                        values.append(self.parse_neptune_property_value(raw_val))
                    prop_types[prop] = self._infer_schema_type(values)

                headers = [":START_ID", ":END_ID"] + [
                    f"{prop}:{prop_types[prop]}" for prop in props
                ]
            else:
                headers = ["source", "target"] + props

            with open(edges_output, 'w', newline='', encoding='utf-8') as out_f:
                writer = csv.writer(out_f)
                writer.writerow(headers)

                for edge_data in type_data[edge_type]:
                    output_row: List[str] = [edge_data["source"], edge_data["target"]]
                    for prop in props:
                        raw_val = edge_data["properties"].get(prop, "")
                        if raw_val == "":
                            output_row.append("")
                            continue

                        parsed_value = self.parse_neptune_property_value(raw_val)
                        if self.enforce_schema:
                            output_row.append(
                                self._format_value_for_schema(parsed_value, prop_types[prop])
                            )
                        else:
                            if isinstance(parsed_value, (dict, list)):
                                output_row.append(json.dumps(parsed_value))
                            else:
                                output_row.append(str(parsed_value))

                    writer.writerow(output_row)

            created_files.append(out_name)
            edge_manifest_entry: Dict[str, Any] = {
                "file": out_name,
                "type": edge_type,
                "count": len(type_data[edge_type]),
                "properties": props,
            }
            if self.enforce_schema:
                edge_manifest_entry["property_types"] = prop_types
            self._generated_edge_files.append(edge_manifest_entry)

            logger.info(f"Created {edges_output} with {len(type_data[edge_type])} edges (type={edge_type})")

        logger.info(f"Created {len(created_files)} edge files: {created_files}")
        logger.info(f"Found {len(self.edge_types)} unique edge types: {sorted(self.edge_types)}")
        logger.info(f"Found {len(self.edge_properties)} unique edge properties: {sorted(self.edge_properties)}")
    
    def generate_bulk_loader_manifest(self) -> Path:
        """Write a manifest describing the generated bulk-loader CSVs."""
        manifest = {
            "format": "falkordb-bulk-loader",
            "source": {
                "source_format": "Neptune Export Service CSV",
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "options": {
                "enforce_schema": self.enforce_schema,
            },
            "output": {
                "nodes": self._generated_node_files,
                "relations": self._generated_edge_files,
            },
            "summary": {
                "node_labels": sorted(self.node_labels),
                "edge_types": sorted(self.edge_types),
                "node_properties": sorted(self.node_properties),
                "edge_properties": sorted(self.edge_properties),
            },
        }

        manifest_path = self.output_dir / self.MANIFEST_FILENAME
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        logger.info(f"Bulk loader manifest written to {manifest_path}")
        return manifest_path
    
    def convert(self) -> None:
        """Main conversion method."""
        logger.info(f"Starting conversion from {self.input_dir} to {self.output_dir}")
        
        # Find Neptune export files
        files = self.find_neptune_files()
        
        if not files['node_files'] and not files['edge_files']:
            logger.error("No Neptune export files found. Please check the input directory.")
            sys.exit(1)
        
        logger.info(f"Found {len(files['node_files'])} node files and {len(files['edge_files'])} edge files")
        
        # Convert nodes
        if files['node_files']:
            self.convert_nodes(files['node_files'])
        else:
            logger.warning("No node files found")

        # Convert edges
        if files['edge_files']:
            self.convert_edges(files['edge_files'])
        else:
            logger.warning("No edge files found")
        
        # Write manifest for bulk-loader import
        manifest_path = self.generate_bulk_loader_manifest()

        logger.info("Conversion completed successfully!")
        
        # Print summary
        node_files = list(self.output_dir.glob('nodes_*.csv'))
        edge_files = list(self.output_dir.glob('edges_*.csv'))
        
        print("\n=== Conversion Summary ===")
        print(f"Input directory: {self.input_dir}")
        print(f"Output directory: {self.output_dir}")
        print(f"Node labels found: {len(self.node_labels)} ({', '.join(sorted(self.node_labels))})")
        print(f"Node properties found: {len(self.node_properties)}")
        print(f"Edge types found: {len(self.edge_types)} ({', '.join(sorted(self.edge_types))})")
        print(f"Edge properties found: {len(self.edge_properties)}")
        
        print(f"\nOutput files created ({len(node_files) + len(edge_files) + 1} total):")
        print(f"\n  Node files ({len(node_files)}):")
        for f in sorted(node_files):
            print(f"    - {f.name}")
        
        print(f"\n  Edge files ({len(edge_files)}):")
        for f in sorted(edge_files):
            print(f"    - {f.name}")
        
        print(f"\n  Metadata:")
        print(f"    - {manifest_path.name}")
        
        

def main():
    parser = argparse.ArgumentParser(
        description='Convert Neptune Export Service CSV to FalkorDB bulk-loader CSV format'
    )
    parser.add_argument('--input-dir', '-i', required=True, help='Directory containing Neptune export CSV files')
    parser.add_argument('--output-dir', '-o', required=True, help='Output directory for FalkorDB CSV files')
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose logging')
    parser.add_argument(
        '--enforce-schema',
        '--enforce-scehma',
        dest='enforce_schema',
        action='store_true',
        help='Emit typed CSV headers compatible with falkordb-bulk-loader --enforce-schema',
    )
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Validate input directory
    input_path = Path(args.input_dir)
    if not input_path.exists():
        print(f"Error: Input directory {input_path} does not exist")
        sys.exit(1)
    
    if not input_path.is_dir():
        print(f"Error: {input_path} is not a directory")
        sys.exit(1)
    
    # Create converter and run
    converter = NeptuneToFalkorDBConverter(
        args.input_dir, args.output_dir, enforce_schema=args.enforce_schema
    )
    converter.convert()


if __name__ == '__main__':
    main()