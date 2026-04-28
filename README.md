# AWS Neptune to FalkorDB (Bulk Loader) CSV Converter

This repository provides a **two-phase migration workflow** for moving Amazon Neptune Export Service data into FalkorDB.

1. **Phase 1 – Conversion**: `neptune_to_falkordb_converter.py` converts Neptune CSV exports into FalkorDB-ready CSV artifacts (separate node/edge files plus a manifest).
2. **Phase 2 – Loading**: Choose one of two loading paths:
   - **Bulk loader path** with `bulk_load_to_falkordb.py` (`falkordb-bulk-loader`)
   - **CSV loader path** with `falkordb_csv_loader.py` (optionally using `prepare_falkordb_csv_loader_input.py`)

## Features

- **Two-phase migration workflow**: Clear split between conversion (phase 1) and loading (phase 2)
- **Flexible phase-2 loading options**: Supports both the bulk loader flow and the CSV loader flow
- **Automatic file detection**: Intelligently finds Neptune export files (vertices.csv, edges.csv, etc.)
- **Label-based file organization**: Creates separate files per node label and edge type for optimized schemas
- **Schema preservation**: Maintains all node labels, edge types, and properties
- **Edge endpoint labels in output**: Writes `source_label` and `target_label` columns in generated edge CSVs
- **Property handling**: Correctly parses JSON-encoded properties and complex data types
- **Flexible input formats**: Handles various Neptune export CSV formats including pipe-delimited and line-numbered formats
- **Smart delimiter detection**: Automatically detects CSV delimiters and line number prefixes
- **Schema documentation**: Generates detailed schema information about the converted data
- **Flexible loading helper**: `bulk_load_to_falkordb.py` supports both insert mode (new graph) and update mode (existing graph)

## Requirements
- Python 3.7+
- Converter script (`neptune_to_falkordb_converter.py`): standard library modules only (no external dependencies)
- Loader helper (`bulk_load_to_falkordb.py`): standard library only unless you enable index creation
  - Optional (only if using `--create-id-indexes`): `pip install falkordb redis`

## Installation

No installation required. Just download the script:

```bash
# Make the script executable
chmod +x neptune_to_falkordb_converter.py
```


## Usage

### Basic Usage

```bash
python3 neptune_to_falkordb_converter.py --input-dir /path/to/neptune/export --output-dir /path/to/falkordb/output
```

### Enforced Schema Output (optional)

If you want the converter to write Neo4j-style typed headers (e.g. `id:ID`, `name:STRING`, `:START_ID`, `:END_ID`, `source_label:STRING`, `target_label:STRING`) for use with the bulk loader's `--enforce-schema` flag:

```bash
python neptune_to_falkordb_converter.py -i /path/to/neptune/export -o /path/to/falkordb/output --enforce-schema
```

### With Verbose Logging

```bash
python3 neptune_to_falkordb_converter.py -i ./twitter_neptune_data -o ./twitter_falkordb_data --verbose
```

## Command Line Options

```
usage: neptune_to_falkordb_converter.py [-h] --input-dir INPUT_DIR --output-dir OUTPUT_DIR [--verbose] [--enforce-schema]

Convert Neptune Export Service CSV to FalkorDB bulk-loader CSV format

optional arguments:
  -h, --help            show this help message and exit
  --input-dir INPUT_DIR, -i INPUT_DIR
                        Directory containing Neptune export CSV files
  --output-dir OUTPUT_DIR, -o OUTPUT_DIR
                        Output directory for FalkorDB CSV files
  --verbose, -v         Enable verbose logging for debugging
  --enforce-schema      Emit typed CSV headers compatible with falkordb-bulk-loader --enforce-schema
```

## Input Format (Neptune Export Service)

The script expects Neptune Export Service CSV files with these typical structures:

### Vertices/Nodes File
```csv
~id,~label,username,followers_count,verified
1,User,@elonmusk,50000000,true
2,User,@twitter,60000000,true
```

### Edges/Relationships File
```csv
~id,~label,~from,~to,created_at,weight
e1,FOLLOWS,1,2,2023-01-15,1.0
e2,MENTIONS,2,1,2023-02-20,0.8
```

## Output Format (FalkorDB Bulk Loader)

The script generates CSVs in the **falkordb-bulk-loader** schemaless format by default.

If you run the converter with `--enforce-schema`, the CSV headers will include explicit types and ID markers compatible with `falkordb-bulk-loader --enforce-schema`.

### Node Files (nodes_*.csv)
Each output node file represents a label-set. The **first column is the node identifier**, and the remaining columns are node properties.

**nodes_User.csv**:
```csv
id,username,followers_count,verified
1,@elonmusk,50000000,true
2,@twitter,60000000,true
```

If Neptune vertices contain multiple labels, nodes are grouped by their *full label set* and written once to a combined file. Example:

**nodes_User__Verified.csv** (labels `User:Verified` at import time):
```csv
id,username,verified
42,@example,true
```

### Edge Files (edges_*.csv)
Each output edge file represents a relationship type. The **first two columns are the start and end node identifiers**. Generated edge files also include `source_label` and `target_label`, followed by relationship properties.

**edges_FOLLOWS.csv**:
```csv
source,target,source_label,target_label,created_at,weight
1,2,User,User,2023-01-15,1.0
```

**edges_MENTIONS.csv**:
```csv
source,target,source_label,target_label,created_at,weight
2,1,User,User,2023-02-20,0.8
```

## File Discovery

The script automatically detects Neptune export files using these patterns:
**Node files**: any CSV filename containing `vertices`, `nodes`, or `vertex`  
**Edge files**: any CSV filename containing `edges` or `relationships`

For remaining unmatched CSV files, the script analyzes CSV headers to identify file types.

## Neptune Column Mapping

The converter handles various Neptune export formats:

### Node Columns
- **ID**: `~id`, `id`, `vertex_id`
- **Labels**: `~label`, `label`, `labels`, `~labels`
- **Properties**: Any other non-system columns

### Edge Columns
- **Source**: `~from`, `source`, `from`
- **Target**: `~to`, `target`, `to`
- **Type**: `~label`, `label`, `type`, `relationship_type`
- **Endpoint labels in output**: `source_label`, `target_label` (inferred from converted node labels by endpoint ID when possible)
- **Properties**: Any other non-system columns

## Data Type Handling

The script intelligently converts Neptune data types:

- **JSON objects/arrays**: Parsed and re-serialized
- **Numbers**: Converted to int/float as appropriate
- **Booleans**: Converted from string representation
- **Strings**: Preserved as-is
- **Empty values**: Converted to empty strings

## Output Files

The converter creates **multiple optimized files**:

### Node Files
- **`nodes_*.csv`**: One file per **unique label-set** (a node appears in exactly one file)
- Example: `nodes_User.csv`, `nodes_Tweet.csv`, `nodes_User__Verified.csv`

### Edge Files
- **`edges_*.csv`**: One file per edge type with endpoint IDs, endpoint label columns (`source_label`, `target_label`), and relevant edge properties
- Example: `edges_FOLLOWS.csv`, `edges_MENTIONS.csv`, `edges_RETWEETS.csv`

### Metadata
- **`bulk_loader_manifest.json`**: Manifest describing the generated CSVs, including:
  - Node files and the label-set that should be applied to each file
  - Relationship files and their relationship type
  - Basic summary information

## Example Workflow

1. **Export from Neptune** using Neptune Export Service
2. **Convert to FalkorDB format**:
   ```bash
   python3 neptune_to_falkordb_converter.py -i ./twitter_neptune_export -o ./twitter_falkordb_import
   ```
3. **Review the output**:
   ```bash
   ls twitter_falkordb_import/
   # nodes_*.csv  edges_*.csv  bulk_loader_manifest.json

   # Check node files
   head twitter_falkordb_import/nodes_User.csv

   # Check edge files
   head twitter_falkordb_import/edges_FOLLOWS.csv

   # Review manifest
   cat twitter_falkordb_import/bulk_loader_manifest.json
   ```
4. **Pre-create `id` indexes for all node labels** (recommended for large datasets):
   ```bash
   python3 - <<'PY' | redis-cli -u redis://127.0.0.1:6379 --raw
   import json
   from pathlib import Path

   manifest = json.loads(Path("./twitter_falkordb_import/bulk_loader_manifest.json").read_text(encoding="utf-8"))
   labels = set(manifest.get("summary", {}).get("node_labels", []) or [])
   if not labels:
       for node in manifest.get("output", {}).get("nodes", []):
           labels.update(node.get("labels", []) or [])

   graph_name = "my_graph_name"

   def quote_ident(name: str) -> str:
       return "`" + name.replace("`", "``") + "`"

   for label in sorted(l for l in labels if isinstance(l, str) and l):
       print(
           f'GRAPH.QUERY {graph_name} "CREATE INDEX FOR (u:{quote_ident(label)}) ON (u.id);"'
       )
   PY
   ```
5. **Import into FalkorDB** using one of the options in [Loading Data into FalkorDB](#loading-data-into-falkordb):
   - **Option A**: `falkordb-bulk-loader` via `bulk_load_to_falkordb.py`
   - **Option B**: `falkordb_csv_loader.py` (after preparing files with `prepare_falkordb_csv_loader_input.py`)

## Real Example: Twitter Dataset

Converting a Twitter social network dataset:

```bash
# Convert Twitter Neptune export to FalkorDB format
python3 neptune_to_falkordb_converter.py -i ./twitter_neptune_export -o ./twitter_falkordb --verbose

# Example output:
# Converting nodes from 1 files: ['users.csv']
# Converting edges from 1 files: ['follows.csv']
# 
# Created files:
# nodes_User.csv                - Twitter user profiles with properties
# edges_FOLLOWS.csv             - Follow relationships with timestamps
# bulk_loader_manifest.json     - Bulk loader manifest
```

**Sample Output Structure:**
- **nodes_User.csv**: `id,username,followers_count,verified`
- **edges_FOLLOWS.csv**: `source,target,source_label,target_label,created_at`

## Troubleshooting

### Common Issues

1. **No files found**
   - Check that Neptune export files are in the input directory
   - Verify file naming conventions match expected patterns

2. **Missing node/edge properties**
   - Check the verbose output to see what properties were detected
   - Verify Neptune export includes all required data

3. **Encoding issues**
   - The script uses UTF-8 encoding by default
   - For other encodings, modify the script's file opening parameters

### Debug Mode

Use `--verbose` flag for detailed logging:

```bash
python3 neptune_to_falkordb_converter.py -i input -o output --verbose
```

## Loading Data into FalkorDB

After converting your Neptune data, you can load it into FalkorDB using either:

- the **FalkorDB bulk loader** flow (Options A/C below), or
- the repository's **CSV loader** flow (Option B below).

### Prerequisite for Options A/C: falkordb-bulk-loader

Option B (`falkordb_csv_loader.py`) does not require this clone.  
For Options A/C, clone the bulk loader next to this repository (or point to it with `--bulk-loader-dir`):

```bash
git clone https://github.com/falkordb/falkordb-bulk-loader.git ../falkordb-bulk-loader
```

### Option A (recommended): use the helper script in this repo

```bash
# Convert
python3 neptune_to_falkordb_converter.py -i ./neptune_export -o ./falkordb_csv

# (Optional) generate typed headers for strict loading
# python3 neptune_to_falkordb_converter.py -i ./neptune_export -o ./falkordb_csv --enforce-schema
# Phase 1 (recommended for large datasets): create id indexes on all node labels before load
# This executes one command per label:
#   CREATE INDEX FOR (u:<Label>) ON (u.id);
python3 - <<'PY' | redis-cli -u redis://127.0.0.1:6379 --raw
import json
from pathlib import Path

manifest = json.loads(Path("./falkordb_csv/bulk_loader_manifest.json").read_text(encoding="utf-8"))
labels = set(manifest.get("summary", {}).get("node_labels", []) or [])
if not labels:
    for node in manifest.get("output", {}).get("nodes", []):
        labels.update(node.get("labels", []) or [])

graph_name = "my_graph_name"

def quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"

for label in sorted(l for l in labels if isinstance(l, str) and l):
    print(
        f'GRAPH.QUERY {graph_name} "CREATE INDEX FOR (u:{quote_ident(label)}) ON (u.id);"'
    )
PY

# Phase 2: load (invokes ../falkordb-bulk-loader/falkordb_bulk_loader/bulk_insert.py)
# If the manifest indicates enforce_schema=true, the helper will automatically pass --enforce-schema.
python3 bulk_load_to_falkordb.py my_graph_name --csv-dir ./falkordb_csv --server-url redis://127.0.0.1:6379

# Update mode (invokes bulk_update.py with auto-generated Cypher per CSV file)
# Useful when updating an existing graph.
python3 bulk_load_to_falkordb.py my_graph_name --csv-dir ./falkordb_csv --mode update --server-url redis://127.0.0.1:6379

# Update mode with a custom Cypher query body (applied per update CSV command)
python3 bulk_load_to_falkordb.py my_graph_name --csv-dir ./falkordb_csv --mode update \
  --update-query "MERGE (n:Device {id: row[0]}) SET n.name = row[1], n.status = row[2]" \
  --server-url redis://127.0.0.1:6379
# Update mode with per-file Cypher query bodies from CSV
# CSV format:
#   column 1 = input file name (e.g. nodes_User.csv or edges_FOLLOWS.csv)
#   column 2 = MERGE/Cypher query body
python3 bulk_load_to_falkordb.py my_graph_name --csv-dir ./falkordb_csv --mode update \
  --update-queries-csv ./update_queries.csv \
  --server-url redis://127.0.0.1:6379

# Optional: create :<Label>(id) range indexes after load (requires: pip install falkordb redis)
#   --create-id-indexes
# Optional: if your ID property is not named 'id'
#   --id-property <property_name>   (also used by --mode update to match source/target nodes)
```

#### `bulk_load_to_falkordb.py` key options

- `--mode insert|update` (default: `insert`)
  - `insert`: builds a new graph via `bulk_insert.py` and `-N/-R` manifest mappings
  - `update`: runs `bulk_update.py` per generated CSV using auto-generated Cypher upserts
- `--enforce-schema` / `--no-enforce-schema`
  - Applies to `insert` mode only (passed through to `bulk_insert.py`)
- `--id-property <name>`
  - Property used for post-load index creation, and in `update` mode for endpoint matching
- `--update-query "<cypher query body>"` (alias: `--merge-query`)
  - Applies to `update` mode as a global query fallback for all files
- `--update-queries-csv <path>`
  - Applies to `update` mode and maps individual files to query bodies (column 1 = file name, column 2 = query)
  - Takes precedence over `--update-query` for matched files
- `--dry-run`
  - Prints the command(s) that would run

In `update` mode, this wrapper auto-generates `--csv` / `--query` for each file.  
Do not pass `--csv`, `--query`, or `--variable-name` through passthrough args.

### Option B: use falkordb_csv_loader.py (query-based loader)

This path loads CSVs via Cypher through `falkordb_csv_loader.py`.

Before loading, normalize converted files with `prepare_falkordb_csv_loader_input.py` so:
- filenames follow `nodes_*.csv` / `edges_*.csv`
- headers are compatible with `falkordb_csv_loader.py`
- edge files preserve `source_label` and `target_label` (and can backfill missing values) to improve label-specific matching and index usage

```bash
# Convert Neptune export
python3 neptune_to_falkordb_converter.py -i ./neptune_export -o ./falkordb_csv

# Prepare files for falkordb_csv_loader.py
python3 prepare_falkordb_csv_loader_input.py \
  --input-dir ./falkordb_csv \
  --output-dir ./falkordb_csv_loader_ready

# Load into FalkorDB
python3 falkordb_csv_loader.py my_graph_name --csv-dir ./falkordb_csv_loader_ready

# Optional: upsert behavior
# python3 falkordb_csv_loader.py my_graph_name --csv-dir ./falkordb_csv_loader_ready --merge-mode
```

### Option C: call bulk_insert.py directly

The converter writes `bulk_loader_manifest.json` which tells you which `-N` (nodes-with-label) and `-R` (relations-with-type) arguments to pass.

```bash
python3 ../falkordb-bulk-loader/falkordb_bulk_loader/bulk_insert.py my_graph_name \
  -u redis://127.0.0.1:6379 \
  -N User ./falkordb_csv/nodes_User.csv \
  -R FOLLOWS ./falkordb_csv/edges_FOLLOWS.csv

# If you converted with --enforce-schema, add:
#   --enforce-schema
```

## Advanced Features

### Delimiter Detection
The converter automatically handles multiple CSV formats:
- **Standard CSV**: Comma-delimited files
- **Neptune pipe format**: Pipe-delimited files (`|`)
- **Line-numbered format**: Files with line number prefixes (e.g., `1|data,data,data`)

### File Organization
- **Label-set-based optimization**: Each unique node label-set gets a file with only its relevant properties
- **Type-based optimization**: Each edge type gets a file with only its relevant properties
- **Safe filename generation**: Special characters in labels/types are safely converted

### Multi-label Support
- Nodes with multiple labels are grouped into a combined node file (e.g., `nodes_User__Verified.csv`)
- At load time, the bulk loader is invoked with `-N User:Verified nodes_User__Verified.csv` to apply both labels

## License

This script is provided as-is for Neptune to FalkorDB migration purposes.
