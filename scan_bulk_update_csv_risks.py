#!/usr/bin/env python3
"""Scan/fix CSV input for patterns that can confuse falkordb-bulk-loader bulk_update.

The scanner mirrors key assumptions from:
  falkordb-bulk-loader/falkordb_bulk_loader/bulk_update.py

Relevant parser/serialization behavior:
- CSV parsing uses csv.reader(..., quoting=csv.QUOTE_NONE, escapechar="\\")
- Each cell is stripped via .strip()
- Empty cells can fail (bulk_update accesses cell[0]/cell[-1])
- Unquoted strings are wrapped in double quotes without escaping embedded quotes

Use this script before running --mode update to detect risky rows/cells early.
You can also run in --fix mode to rewrite CSV files into a bulk_update-safe
format by escaping separator characters for QUOTE_NONE parsing.
"""

import argparse
import csv
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class Issue:
    severity: str  # ERROR | WARN
    code: str
    message: str
    row: int
    column: Optional[int]
    value: str


def _snippet(value: str, max_len: int = 80) -> str:
    shown = (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    if len(shown) <= max_len:
        return shown
    return shown[: max_len - 3] + "..."


def _add_issue(
    issues: List[Issue],
    *,
    severity: str,
    code: str,
    message: str,
    row: int,
    column: Optional[int],
    value: str,
) -> None:
    issues.append(
        Issue(
            severity=severity,
            code=code,
            message=message,
            row=row,
            column=column,
            value=value,
        )
    )


def _scan_cell(
    *,
    raw_cell: str,
    row_number: int,
    column_number: int,
    separator: str,
    issues: List[Issue],
) -> None:
    stripped = raw_cell.strip()

    if raw_cell != stripped:
        _add_issue(
            issues,
            severity="WARN",
            code="TRIMMED_WHITESPACE",
            message="Leading/trailing whitespace will be removed by bulk_update .strip().",
            row=row_number,
            column=column_number,
            value=raw_cell,
        )

    if stripped == "":
        _add_issue(
            issues,
            severity="ERROR",
            code="EMPTY_CELL",
            message="Empty cell can break bulk_update quote logic (cell[0]/cell[-1]).",
            row=row_number,
            column=column_number,
            value=raw_cell,
        )
        return

    if any(ch in stripped for ch in ("\n", "\r")):
        _add_issue(
            issues,
            severity="ERROR",
            code="NEWLINE_IN_CELL",
            message="Newline/carriage-return in a cell can confuse CSV row parsing.",
            row=row_number,
            column=column_number,
            value=stripped,
        )

    if "\x00" in stripped:
        _add_issue(
            issues,
            severity="ERROR",
            code="NUL_CHAR",
            message="NUL byte found in cell.",
            row=row_number,
            column=column_number,
            value=stripped,
        )

    if "\\" in stripped:
        _add_issue(
            issues,
            severity="WARN",
            code="BACKSLASH_PRESENT",
            message="Backslash is used as CSV escapechar in bulk_update; verify intended parsing.",
            row=row_number,
            column=column_number,
            value=stripped,
        )

    if stripped.startswith("["):
        if not stripped.endswith("]"):
            _add_issue(
                issues,
                severity="ERROR",
                code="RAW_LIST_LITERAL_MALFORMED",
                message="Value starts with '[' and may be treated as raw list literal, but does not end with ']'.",
                row=row_number,
                column=column_number,
                value=stripped,
            )
        else:
            _add_issue(
                issues,
                severity="WARN",
                code="RAW_LIST_LITERAL",
                message="Value starts with '[' and may be treated as raw list literal (not quoted). Ensure valid Cypher list syntax.",
                row=row_number,
                column=column_number,
                value=stripped,
            )

    if stripped.startswith('"') != stripped.endswith('"'):
        _add_issue(
            issues,
            severity="ERROR",
            code="UNBALANCED_DOUBLE_QUOTES",
            message="Value has unbalanced surrounding double quotes.",
            row=row_number,
            column=column_number,
            value=stripped,
        )

    if stripped.startswith("'") != stripped.endswith("'"):
        _add_issue(
            issues,
            severity="ERROR",
            code="UNBALANCED_SINGLE_QUOTES",
            message="Value has unbalanced surrounding single quotes.",
            row=row_number,
            column=column_number,
            value=stripped,
        )

    if '"' in stripped and not (stripped.startswith('"') and stripped.endswith('"')):
        _add_issue(
            issues,
            severity="ERROR",
            code="EMBEDDED_DOUBLE_QUOTE",
            message="Embedded double quote in unquoted value can produce malformed CYPHER rows payload.",
            row=row_number,
            column=column_number,
            value=stripped,
        )

    if separator and separator in stripped:
        _add_issue(
            issues,
            severity="WARN",
            code="SEPARATOR_IN_CELL",
            message="Separator character appears inside parsed value; verify escaping and expected column alignment.",
            row=row_number,
            column=column_number,
            value=stripped,
        )


def _read_header_columns(path: Path, separator: str) -> Optional[int]:
    with open(path, "rt", encoding="utf-8", newline="") as f:
        reader = csv.reader(
            f,
            delimiter=separator,
            skipinitialspace=True,
            quoting=csv.QUOTE_NONE,
            escapechar="\\",
        )
        try:
            header = next(reader)
        except StopIteration:
            return None
    return len(header)


def _default_fixed_output_path(source_path: Path) -> Path:
    suffix = source_path.suffix if source_path.suffix else ".csv"
    return source_path.with_name(f"{source_path.stem}.bulk_update_fixed{suffix}")


def _rewrite_csv_for_bulk_update(
    *,
    source_path: Path,
    output_path: Path,
    separator: str,
) -> int:
    """Rewrite CSV to a format friendly to bulk_update QUOTE_NONE parsing."""
    output_path = output_path.resolve()
    source_path = source_path.resolve()
    same_file = source_path == output_path
    temp_output_path = output_path
    if same_file:
        temp_output_path = output_path.with_suffix(f"{output_path.suffix}.tmp")

    temp_output_path.parent.mkdir(parents=True, exist_ok=True)

    row_count = 0
    reader = None
    try:
        with open(source_path, "rt", encoding="utf-8", newline="") as src, open(
            temp_output_path, "wt", encoding="utf-8", newline=""
        ) as dst:
            reader = csv.reader(src, delimiter=separator)
            writer = csv.writer(
                dst,
                delimiter=separator,
                quoting=csv.QUOTE_NONE,
                escapechar="\\",
                lineterminator="\n",
            )
            for row in reader:
                writer.writerow(row)
                row_count += 1
    except csv.Error as e:
        line_num = reader.line_num if reader is not None else "unknown"
        raise RuntimeError(
            f"CSV rewrite failed near source line {line_num}: {e}"
        ) from e

    if same_file:
        temp_output_path.replace(output_path)

    return row_count


def scan_file(
    *,
    path: Path,
    separator: str,
    no_header: bool,
    expected_columns_override: Optional[int],
) -> tuple[List[Issue], int, Optional[int]]:
    issues: List[Issue] = []
    rows_scanned = 0

    expected_columns = expected_columns_override
    if expected_columns is None and not no_header:
        expected_columns = _read_header_columns(path, separator)

    with open(path, "rt", encoding="utf-8", newline="") as f:
        # bulk_update skips first line blindly when header is expected.
        if not no_header:
            _ = next(f, None)

        reader = csv.reader(
            f,
            delimiter=separator,
            skipinitialspace=True,
            quoting=csv.QUOTE_NONE,
            escapechar="\\",
        )

        # In file line terms: data starts on line 1 with --no-header, else line 2.
        line_offset = 0 if no_header else 1

        for data_index, row in enumerate(reader, start=1):
            rows_scanned += 1
            file_row_number = data_index + line_offset

            if expected_columns is None:
                expected_columns = len(row)

            if len(row) == 0:
                _add_issue(
                    issues,
                    severity="ERROR",
                    code="EMPTY_ROW",
                    message="Blank row produces [] and can break row[index] lookups in update queries.",
                    row=file_row_number,
                    column=None,
                    value="",
                )
                continue

            if expected_columns is not None and len(row) != expected_columns:
                _add_issue(
                    issues,
                    severity="ERROR",
                    code="COLUMN_COUNT_MISMATCH",
                    message=f"Expected {expected_columns} columns, found {len(row)}.",
                    row=file_row_number,
                    column=None,
                    value=",".join(row),
                )

            for col_index, cell in enumerate(row, start=1):
                _scan_cell(
                    raw_cell=cell,
                    row_number=file_row_number,
                    column_number=col_index,
                    separator=separator,
                    issues=issues,
                )

    return issues, rows_scanned, expected_columns


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan CSV for characters/patterns that can confuse falkordb bulk_update parsing."
    )
    parser.add_argument("csv_file", help="Input CSV file to scan.")
    parser.add_argument(
        "--separator",
        "-o",
        default=",",
        help="CSV field separator used by bulk_update (default: ',').",
    )
    parser.add_argument(
        "--no-header",
        "-n",
        action="store_true",
        help="Set if CSV has no header (matches bulk_update --no-header).",
    )
    parser.add_argument(
        "--expected-columns",
        type=int,
        default=None,
        help="Optional explicit expected column count for data rows.",
    )
    parser.add_argument(
        "--max-issues",
        type=int,
        default=100,
        help="Maximum number of issues to print (default: 100).",
    )
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Return non-zero exit code when only WARN issues are found.",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help=(
            "Rewrite the CSV into a bulk_update-safe format by escaping separator "
            "characters for QUOTE_NONE parsing before scanning."
        ),
    )
    parser.add_argument(
        "--fix-output",
        default=None,
        help=(
            "Output path for rewritten CSV when --fix is enabled. "
            "Default: <input>.bulk_update_fixed.csv"
        ),
    )
    parser.add_argument(
        "--fix-in-place",
        action="store_true",
        help="Rewrite the input CSV in place when --fix is enabled.",
    )

    args = parser.parse_args()
    if args.fix_output and not args.fix:
        parser.error("--fix-output requires --fix.")
    if args.fix_in_place and not args.fix:
        parser.error("--fix-in-place requires --fix.")
    if args.fix_in_place and args.fix_output:
        parser.error("--fix-in-place cannot be used together with --fix-output.")
    return args


def main() -> int:
    args = parse_args()
    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        print(f"❌ File not found: {csv_path}")
        return 2
    if not csv_path.is_file():
        print(f"❌ Not a file: {csv_path}")
        return 2

    scan_path = csv_path
    if args.fix:
        try:
            fix_output_path = (
                csv_path
                if args.fix_in_place
                else (
                    Path(args.fix_output)
                    if args.fix_output
                    else _default_fixed_output_path(csv_path)
                )
            )
            rewritten_rows = _rewrite_csv_for_bulk_update(
                source_path=csv_path,
                output_path=fix_output_path,
                separator=args.separator,
            )
            scan_path = fix_output_path
            print(f"✅ Rewrote CSV for bulk_update parsing: {scan_path}")
            print(f"   Rows rewritten: {rewritten_rows}")
        except Exception as e:
            print(f"❌ Failed to rewrite CSV in --fix mode: {e}")
            return 2

    issues, rows_scanned, expected_columns = scan_file(
        path=scan_path,
        separator=args.separator,
        no_header=args.no_header,
        expected_columns_override=args.expected_columns,
    )

    error_count = sum(1 for issue in issues if issue.severity == "ERROR")
    warn_count = sum(1 for issue in issues if issue.severity == "WARN")
    by_code = Counter(issue.code for issue in issues)

    print(f"Scanned file: {scan_path}")
    print(f"Rows scanned (data rows): {rows_scanned}")
    if expected_columns is not None:
        print(f"Expected columns: {expected_columns}")
    print(f"Issues found: {len(issues)} (ERROR={error_count}, WARN={warn_count})")

    if issues:
        print("\nIssue summary by type:")
        for code, count in sorted(by_code.items(), key=lambda item: (-item[1], item[0])):
            print(f"  - {code}: {count}")

        print(f"\nFirst {min(args.max_issues, len(issues))} issue(s):")
        for issue in issues[: args.max_issues]:
            loc = f"row {issue.row}"
            if issue.column is not None:
                loc += f", col {issue.column}"
            print(
                f"  [{issue.severity}] {issue.code} at {loc}: {issue.message} "
                f"(value='{_snippet(issue.value)}')"
            )
    else:
        print("✅ No risky patterns detected for the selected checks.")

    if error_count > 0:
        return 2
    if warn_count > 0 and args.fail_on_warning:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
