# AWS Neptune to FalkorDB CSV Converter

This script converts Amazon Neptune Export Service CSV files into FalkorDB-compatible CSV format for easy data migration. The converter creates **separate CSV files for each node label and edge type**, optimizing the schema for each entity type.

## Features

- **Automatic file detection**: Intelligently finds Neptune export files (vertices.csv, edges.csv, etc.)
- **Label-based file organization**: Creates separate files per node label and edge type for optimized schemas
- **Schema preservation**: Maintains all node labels, edge types, and properties
- **Property handling**: Correctly parses JSON-encoded properties and complex data types
- **Flexible input formats**: Handles various Neptune export CSV formats including pipe-delimited and line-numbered formats
- **Smart delimiter detection**: Automatically detects CSV delimiters and line number prefixes
- **Schema documentation**: Generates detailed schema information about the converted data

## Requirements

- Python 3.7+
- Standard library modules only (no external dependencies)

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

### With Verbose Logging

```bash
python3 neptune_to_falkordb_converter.py -i ./twitter_neptune_data -o ./twitter_falkordb_data --verbose
```

## Command Line Options

```
usage: neptune_to_falkordb_converter.py [-h] --input-dir INPUT_DIR --output-dir OUTPUT_DIR [--verbose]

Convert Neptune Export Service CSV to FalkorDB format

optional arguments:
  -h, --help            show this help message and exit
  --input-dir INPUT_DIR, -i INPUT_DIR
                        Directory containing Neptune export CSV files
  --output-dir OUTPUT_DIR, -o OUTPUT_DIR
                        Output directory for FalkorDB CSV files
  --verbose, -v         Enable verbose logging for debugging
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

## Output Format (FalkorDB)

The script generates **separate FalkorDB-compatible CSV files for each node label and edge type**:

### Node Files (nodes_[LABEL].csv)
Each node label gets its own optimized file:

**nodes_User.csv**:
```csv
id,labels,username,followers_count,verified
1,User,@elonmusk,50000000,true
2,User,@twitter,60000000,true
```

### Edge Files (edges_[TYPE].csv)
Each edge type gets its own optimized file:

**edges_FOLLOWS.csv**:
```csv
source,target,type,source_label,target_label,created_at,weight
1,2,FOLLOWS,User,User,2023-01-15,1.0
```

**edges_MENTIONS.csv**:
```csv
source,target,type,source_label,target_label,created_at,weight
2,1,MENTIONS,User,User,2023-02-20,0.8
```

## File Discovery

The script automatically detects Neptune export files using these patterns:

**Node files**: `vertices.csv`, `nodes.csv`, `vertex.csv`
**Edge files**: `edges.csv`, `relationships.csv`, `edge.csv`
**Schema files**: `schema.json`, `metadata.json`

If standard file names aren't found, the script analyzes CSV headers to identify file types.

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
- **`nodes_[LABEL].csv`**: One file per node label with only relevant properties
- Example: `nodes_User.csv`, `nodes_Tweet.csv`, `nodes_Hashtag.csv`

### Edge Files
- **`edges_[TYPE].csv`**: One file per edge type with only relevant properties
- Includes `source_label` and `target_label` columns for context
- Example: `edges_FOLLOWS.csv`, `edges_MENTIONS.csv`, `edges_RETWEETS.csv`

### Metadata
- **`schema_info.json`**: Comprehensive schema information including:
  - Node labels and their corresponding files
  - Edge types and their corresponding files
  - Property lists per label/type
  - File mapping and conversion metadata

## Example Workflow

1. **Export from Neptune** using Neptune Export Service
2. **Convert to FalkorDB format**:
   ```bash
   python3 neptune_to_falkordb_converter.py -i ./twitter_neptune_export -o ./twitter_falkordb_import
   ```
3. **Review the output**:
   ```bash
   ls twitter_falkordb_import/
   # nodes_User.csv  nodes_Tweet.csv  edges_FOLLOWS.csv  edges_MENTIONS.csv  schema_info.json
   
   # Check node files
   head twitter_falkordb_import/nodes_User.csv
   head twitter_falkordb_import/nodes_Tweet.csv
   
   # Check edge files
   head twitter_falkordb_import/edges_FOLLOWS.csv
   head twitter_falkordb_import/edges_MENTIONS.csv
   
   # Review schema
   cat twitter_falkordb_import/schema_info.json
   ```
4. **Import into FalkorDB** using the FalkorDB Rust loader (see [Loading Data into FalkorDB](#loading-data-into-falkordb) section below)

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
# nodes_User.csv     - Twitter user profiles with properties
# edges_FOLLOWS.csv  - Follow relationships with timestamps
# schema_info.json   - Complete schema metadata
```

**Sample Output Structure:**
- **nodes_User.csv**: `id,labels,username,followers_count,verified`
- **edges_FOLLOWS.csv**: `source,target,type,source_label,target_label,created_at`

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

After converting your Neptune data to CSV format using this tool, you can load it into FalkorDB using the high-performance **FalkorDB Rust Loader**.

### FalkorDB Rust Loader

The [FalkorDB Rust Loader](https://github.com/FalkorDB/FalkorDB-Loader-RS) is a command-line tool specifically designed for loading CSV files into FalkorDB with optimal performance and comprehensive error handling.

#### Installation

```bash
git clone https://github.com/FalkorDB/FalkorDB-Loader-RS
cd FalkorDB-Loader-RS
cargo build --release
```

#### Basic Usage

```bash
# Load your converted CSV files into FalkorDB
./target/release/falkordb-loader my_graph_name --csv-dir ./twitter_falkordb_import
```

#### Advanced Usage

```bash
# Load with custom settings
./target/release/falkordb-loader my_graph_name \
  --host localhost \
  --port 6379 \
  --csv-dir ./twitter_falkordb_import \
  --batch-size 1000 \
  --merge-mode \
  --stats \
  --progress-interval 500
```

#### Key Features

- **High Performance**: Async batch processing with configurable batch sizes
- **Schema Management**: Automatic index and constraint creation
- **Merge Mode**: Support for upsert operations to handle duplicate data
- **Progress Tracking**: Real-time progress reporting during loading
- **Error Handling**: Comprehensive error handling with detailed logging
- **Type Safety**: Automatic type inference for properties

#### Workflow Integration

1. **Convert Neptune data** using this Neptune-to-FalkorDB converter
2. **Load into FalkorDB** using the Rust loader:
   ```bash
   # Convert
   python3 neptune_to_falkordb_converter.py -i ./neptune_export -o ./falkordb_csv
   
   # Load
   ./target/release/falkordb-loader my_social_graph --csv-dir ./falkordb_csv --stats
   ```

For detailed usage instructions and configuration options, visit the [FalkorDB Rust Loader repository](https://github.com/FalkorDB/FalkorDB-Loader-RS).

## Advanced Features

### Delimiter Detection
The converter automatically handles multiple CSV formats:
- **Standard CSV**: Comma-delimited files
- **Neptune pipe format**: Pipe-delimited files (`|`)
- **Line-numbered format**: Files with line number prefixes (e.g., `1|data,data,data`)

### File Organization
- **Label-based optimization**: Each node label gets a file with only its relevant properties
- **Type-based optimization**: Each edge type gets a file with only its relevant properties
- **Safe filename generation**: Special characters in labels/types are safely converted

### Multi-label Support
- Nodes can have multiple labels (stored as semicolon-separated values)
- Primary label used for source_label/target_label in edges
- All labels preserved in the `labels` column

## License

This script is provided as-is for Neptune to FalkorDB migration purposes.
