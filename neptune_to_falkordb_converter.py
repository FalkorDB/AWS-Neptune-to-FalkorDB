#!/usr/bin/env python3
"""
Neptune Export Service to FalkorDB CSV Converter

This script converts Neptune Export Service CSV files into FalkorDB-compatible
nodes and edges CSV format.

Usage:
    python neptune_to_falkordb_converter.py --input-dir <neptune_export_dir> --output-dir <falkordb_output_dir>

Neptune Export Service typically produces:
- vertices.csv (nodes)
- edges.csv (relationships)
- Additional metadata files

FalkorDB expects:
- nodes.csv with columns: id, labels, properties...
- edges.csv with columns: source, target, type, properties...
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Set, Any, Optional
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class NeptuneToFalkorDBConverter:
    def __init__(self, input_dir: str, output_dir: str):
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Track all property names and labels/types
        self.node_properties: Set[str] = set()
        self.edge_properties: Set[str] = set()
        self.node_labels: Set[str] = set()
        self.edge_types: Set[str] = set()
        
        # Track node ID to labels mapping for edge label resolution
        self.node_id_to_labels: Dict[str, List[str]] = {}
        
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
        """Convert Neptune vertices to FalkorDB nodes format, creating separate files per label."""
        logger.info(f"Converting nodes from {len(node_files)} files: {[f.name for f in node_files]}")
        
        # Track properties per label to optimize CSV structure
        label_properties = {}
        label_data = {}  # Store rows per label
        
        # Process each node file
        for vertices_file in node_files:
            logger.info(f"Processing node file: {vertices_file.name}")
            
            # First pass: collect all properties, labels, and organize data by label
            with open(vertices_file, 'r', encoding='utf-8') as f:
                # Check if file uses pipe delimiter
                first_line = f.readline().strip()
                f.seek(0)
                
                # Detect delimiter - check if it's the line number format (1|header,data,data)
                logger.debug(f"Delimiter detection for {vertices_file.name}: first_line={repr(first_line)}")
                logger.debug(f"Detection conditions: startswith_1_pipe={first_line.startswith('1|')}, has_comma={',' in first_line}, has_pipe={'|' in first_line}")
                
                if first_line.startswith('1|') and ',' in first_line:
                    logger.debug(f"Using line number removal logic for {vertices_file.name}")
                    # Skip the line number and parse as comma-delimited
                    # Read all lines and remove line numbers
                    lines = f.readlines()
                    f.seek(0)
                    # Write cleaned lines back
                    import io
                    cleaned_content = io.StringIO()
                    for line in lines:
                        if '|' in line and line[0].isdigit():
                            # Remove line number prefix (e.g., "1|" -> "")
                            cleaned_line = line[line.find('|')+1:]
                            cleaned_content.write(cleaned_line)
                        else:
                            cleaned_content.write(line)
                    cleaned_content.seek(0)
                    reader = csv.DictReader(cleaned_content)
                elif '|' in first_line and not first_line.startswith('1|'):
                    logger.debug(f"Using pure pipe-delimited logic for {vertices_file.name}")
                    # Pure pipe-delimited (Neptune export format)
                    reader = csv.DictReader(f, delimiter='|')
                else:
                    logger.debug(f"Using standard comma-delimited logic for {vertices_file.name}")
                    # Standard comma-delimited
                    reader = csv.DictReader(f)
                
                logger.debug(f"Headers for {vertices_file.name}: {reader.fieldnames}")
                
                for row in reader:
                    # Extract labels
                    labels = self.extract_labels_from_neptune_row(row)
                    if not labels:
                        labels = ['UnlabeledNode']  # Default label for nodes without explicit labels
                    
                    self.node_labels.update(labels)
                    
                    # Get node ID
                    node_id = row.get('~id') or row.get('id') or row.get('vertex_id')
                    if not node_id:
                        logger.warning(f"No ID found for row: {row}")
                        continue
                    
                    # Extract properties (skip Neptune system columns)
                    node_properties = {}
                    for col, value in row.items():
                        if not col.startswith('~') and col not in ['id', 'label', 'labels'] and value:
                            # Strip type annotations from column names (e.g., "city:string" -> "city")
                            clean_col = col.split(':')[0] if ':' in col else col
                            self.node_properties.add(clean_col)
                            node_properties[clean_col] = value
                    
                    # Store node ID to labels mapping for edge processing
                    self.node_id_to_labels[node_id] = labels
                    
                    # Store data for each label this node has
                    for label in labels:
                        if label not in label_data:
                            label_data[label] = []
                            label_properties[label] = set()
                        
                        # Add this node's data to this label's collection
                        node_data = {
                            'id': node_id,
                            'labels': labels,  # Keep all labels for this node
                            'properties': node_properties
                        }
                        label_data[label].append(node_data)
                        
                        # Track properties used by this label
                        label_properties[label].update(node_properties.keys())
        
        # Create separate CSV files for each label
        created_files = []
        for label in sorted(self.node_labels):
            if label not in label_data:
                logger.warning(f"Label '{label}' found but has no associated data")
                continue
            
            # Create filename safe for filesystem
            safe_label = label.replace('/', '_').replace('\\', '_').replace(':', '_').replace('*', '_').replace('?', '_').replace('"', '_').replace('<', '_').replace('>', '_').replace('|', '_')
            nodes_output = self.output_dir / f'nodes_{safe_label}.csv'
            
            # Prepare headers specific to this label
            label_specific_properties = sorted(label_properties[label])
            headers = ['id', 'labels'] + label_specific_properties
            
            with open(nodes_output, 'w', newline='', encoding='utf-8') as out_f:
                writer = csv.writer(out_f)
                writer.writerow(headers)
                
                for node_data in label_data[label]:
                    # Build output row
                    labels_str = ';'.join(node_data['labels']) if node_data['labels'] else ''
                    output_row = [node_data['id'], labels_str]
                    
                    # Add properties specific to this label
                    for prop in label_specific_properties:
                        value = node_data['properties'].get(prop, '')
                        if value:
                            parsed_value = self.parse_neptune_property_value(value)
                            if isinstance(parsed_value, (dict, list)):
                                output_row.append(json.dumps(parsed_value))
                            else:
                                output_row.append(str(parsed_value))
                        else:
                            output_row.append('')
                    
                    writer.writerow(output_row)
            
            created_files.append(nodes_output.name)
            logger.info(f"Created {nodes_output} with {len(label_data[label])} nodes")
        
        logger.info(f"Created {len(created_files)} node files: {created_files}")
        logger.info(f"Found {len(self.node_labels)} unique node labels: {sorted(self.node_labels)}")
        logger.info(f"Found {len(self.node_properties)} unique node properties: {sorted(self.node_properties)}")
    
    def convert_edges(self, edge_files: List[Path]) -> None:
        """Convert Neptune edges to FalkorDB edges format, creating separate files per edge type."""
        logger.info(f"Converting edges from {len(edge_files)} files: {[f.name for f in edge_files]}")
        
        # Track properties per edge type to optimize CSV structure
        type_properties = {}
        type_data = {}  # Store rows per edge type
        
        # Process each edge file
        for edges_file in edge_files:
            logger.info(f"Processing edge file: {edges_file.name}")
            
            # First pass: collect all properties, edge types, and organize data by type
            with open(edges_file, 'r', encoding='utf-8') as f:
                # Check if file uses pipe delimiter
                first_line = f.readline().strip()
                f.seek(0)
                
                # Detect delimiter - check if it's the line number format (1|header,data,data)
                logger.debug(f"Delimiter detection for {edges_file.name}: first_line={repr(first_line)}")
                logger.debug(f"Detection conditions: startswith_1_pipe={first_line.startswith('1|')}, has_comma={',' in first_line}, has_pipe={'|' in first_line}")
                
                if first_line.startswith('1|') and ',' in first_line:
                    logger.debug(f"Using line number removal logic for {edges_file.name}")
                    # Skip the line number and parse as comma-delimited
                    # Read all lines and remove line numbers
                    lines = f.readlines()
                    f.seek(0)
                    # Write cleaned lines back
                    import io
                    cleaned_content = io.StringIO()
                    for line in lines:
                        if '|' in line and line[0].isdigit():
                            # Remove line number prefix (e.g., "1|" -> "")
                            cleaned_line = line[line.find('|')+1:]
                            cleaned_content.write(cleaned_line)
                        else:
                            cleaned_content.write(line)
                    cleaned_content.seek(0)
                    reader = csv.DictReader(cleaned_content)
                elif '|' in first_line and not first_line.startswith('1|'):
                    logger.debug(f"Using pure pipe-delimited logic for {edges_file.name}")
                    # Pure pipe-delimited (Neptune export format)
                    reader = csv.DictReader(f, delimiter='|')
                else:
                    logger.debug(f"Using standard comma-delimited logic for {edges_file.name}")
                    # Standard comma-delimited
                    reader = csv.DictReader(f)
                
                logger.debug(f"Headers for {edges_file.name}: {reader.fieldnames}")
                
                for row in reader:
                    # Get source and target first (required for valid edge)
                    source = row.get('~from') or row.get('source') or row.get('from')
                    target = row.get('~to') or row.get('target') or row.get('to')
                    
                    if not source or not target:
                        logger.warning(f"Missing source or target for edge: {row}")
                        continue
                    
                    # Extract edge type/label
                    edge_type = row.get('~label') or row.get('label') or row.get('type') or row.get('relationship_type')
                    if not edge_type:
                        edge_type = 'UnlabeledEdge'  # Default type for edges without explicit type
                    
                    self.edge_types.add(edge_type)
                    
                    # Extract properties (skip Neptune system columns)
                    edge_properties = {}
                    for col, value in row.items():
                        if not col.startswith('~') and col not in ['id', 'label', 'type', 'source', 'target', 'from', 'to'] and value:
                            # Strip type annotations from column names (e.g., "dist:int" -> "dist")
                            clean_col = col.split(':')[0] if ':' in col else col
                            self.edge_properties.add(clean_col)
                            edge_properties[clean_col] = value
                    
                    # Store edge data for this type
                    if edge_type not in type_data:
                        type_data[edge_type] = []
                        type_properties[edge_type] = set()
                    
                    # Get source and target labels from node mapping
                    source_labels = self.node_id_to_labels.get(source, ['Unknown'])
                    target_labels = self.node_id_to_labels.get(target, ['Unknown'])
                    
                    # Use primary label (first one) for source_label and target_label
                    source_label = source_labels[0] if source_labels else 'Unknown'
                    target_label = target_labels[0] if target_labels else 'Unknown'
                    
                    edge_data = {
                        'source': source,
                        'target': target,
                        'type': edge_type,
                        'source_label': source_label,
                        'target_label': target_label,
                        'properties': edge_properties
                    }
                    type_data[edge_type].append(edge_data)
                    
                    # Track properties used by this edge type
                    type_properties[edge_type].update(edge_properties.keys())
        
        # Create separate CSV files for each edge type
        created_files = []
        for edge_type in sorted(self.edge_types):
            if edge_type not in type_data:
                logger.warning(f"Edge type '{edge_type}' found but has no associated data")
                continue
            
            # Create filename safe for filesystem
            safe_type = edge_type.replace('/', '_').replace('\\', '_').replace(':', '_').replace('*', '_').replace('?', '_').replace('"', '_').replace('<', '_').replace('>', '_').replace('|', '_')
            edges_output = self.output_dir / f'edges_{safe_type}.csv'
            
            # Prepare headers specific to this edge type
            type_specific_properties = sorted(type_properties[edge_type])
            headers = ['source', 'target', 'type', 'source_label', 'target_label'] + type_specific_properties
            
            with open(edges_output, 'w', newline='', encoding='utf-8') as out_f:
                writer = csv.writer(out_f)
                writer.writerow(headers)
                
                for edge_data in type_data[edge_type]:
                    # Build output row
                    output_row = [edge_data['source'], edge_data['target'], edge_data['type'], 
                                edge_data['source_label'], edge_data['target_label']]
                    
                    # Add properties specific to this edge type
                    for prop in type_specific_properties:
                        value = edge_data['properties'].get(prop, '')
                        if value:
                            parsed_value = self.parse_neptune_property_value(value)
                            if isinstance(parsed_value, (dict, list)):
                                output_row.append(json.dumps(parsed_value))
                            else:
                                output_row.append(str(parsed_value))
                        else:
                            output_row.append('')
                    
                    writer.writerow(output_row)
            
            created_files.append(edges_output.name)
            logger.info(f"Created {edges_output} with {len(type_data[edge_type])} edges")
        
        logger.info(f"Created {len(created_files)} edge files: {created_files}")
        logger.info(f"Found {len(self.edge_types)} unique edge types: {sorted(self.edge_types)}")
        logger.info(f"Found {len(self.edge_properties)} unique edge properties: {sorted(self.edge_properties)}")
    
    def generate_schema_info(self) -> None:
        """Generate a schema information file."""
        # List generated files
        node_files = list(self.output_dir.glob('nodes_*.csv'))
        edge_files = list(self.output_dir.glob('edges_*.csv'))
        
        schema_info = {
            'conversion_info': {
                'source_format': 'Neptune Export Service CSV',
                'target_format': 'FalkorDB CSV (separate files per label/type)',
                'conversion_timestamp': str(Path().cwd()),
                'output_structure': 'separate_files_per_type'
            },
            'files_generated': {
                'node_files': [f.name for f in sorted(node_files)],
                'edge_files': [f.name for f in sorted(edge_files)],
                'total_files': len(node_files) + len(edge_files)
            },
            'nodes': {
                'labels': sorted(self.node_labels),
                'properties': sorted(self.node_properties),
                'count_labels': len(self.node_labels),
                'count_properties': len(self.node_properties),
                'files_per_label': {label: f'nodes_{label.replace("/", "_").replace("\\", "_").replace(":", "_").replace("*", "_").replace("?", "_").replace('"', "_").replace("<", "_").replace(">", "_").replace("|", "_")}.csv' for label in sorted(self.node_labels)}
            },
            'edges': {
                'types': sorted(self.edge_types),
                'properties': sorted(self.edge_properties),
                'count_types': len(self.edge_types),
                'count_properties': len(self.edge_properties),
                'files_per_type': {edge_type: f'edges_{edge_type.replace("/", "_").replace("\\", "_").replace(":", "_").replace("*", "_").replace("?", "_").replace('"', "_").replace("<", "_").replace(">", "_").replace("|", "_")}.csv' for edge_type in sorted(self.edge_types)}
            }
        }
        
        schema_file = self.output_dir / 'schema_info.json'
        with open(schema_file, 'w', encoding='utf-8') as f:
            json.dump(schema_info, f, indent=2)
        
        logger.info(f"Schema information written to {schema_file}")
    
    def convert(self) -> None:
        """Main conversion method."""
        logger.info(f"Starting conversion from {self.input_dir} to {self.output_dir}")
        
        # Find Neptune export files
        files = self.find_neptune_files()
        
        if not files['node_files'] and not files['edge_files']:
            logger.error("No Neptune export files found. Please check the input directory.")
            sys.exit(1)
        
        logger.info(f"Found {len(files['node_files'])} node files and {len(files['edge_files'])} edge files")
        
        # Convert nodes first to build ID-to-label mapping
        if files['node_files']:
            self.convert_nodes(files['node_files'])
        else:
            logger.warning("No node files found - edge label mapping will be incomplete")
        
        # Convert edges (requires node mapping to be available)
        if files['edge_files']:
            if not self.node_id_to_labels:
                logger.warning("No node label mapping available - source_label and target_label will show 'Unknown'")
            self.convert_edges(files['edge_files'])
        else:
            logger.warning("No edge files found")
        
        # Generate schema info
        self.generate_schema_info()
        
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
        print(f"    - schema_info.json")
        
        

def main():
    parser = argparse.ArgumentParser(description='Convert Neptune Export Service CSV to FalkorDB format')
    parser.add_argument('--input-dir', '-i', required=True, help='Directory containing Neptune export CSV files')
    parser.add_argument('--output-dir', '-o', required=True, help='Output directory for FalkorDB CSV files')
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose logging')
    
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
    converter = NeptuneToFalkorDBConverter(args.input_dir, args.output_dir)
    converter.convert()


if __name__ == '__main__':
    main()