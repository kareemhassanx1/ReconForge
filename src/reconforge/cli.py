"""Command-line entry point for ReconForge."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from reconforge.errors import ReconForgeError
from reconforge.normalization import FieldNormalizer
from reconforge.readers import DatasetFormat, DatasetReadOptions, read_dataset
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.report import result_to_dict, write_json_report


def build_parser() -> argparse.ArgumentParser:
    """Build the public CLI parser."""

    parser = argparse.ArgumentParser(
        prog="reconforge",
        description="Compare tabular exports using stable business keys.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    compare = subcommands.add_parser("compare", help="compare two tabular datasets")
    compare.add_argument("left", type=Path, help="left-hand dataset")
    compare.add_argument("right", type=Path, help="right-hand dataset")
    compare.add_argument(
        "--key",
        action="append",
        required=True,
        dest="key_fields",
        metavar="FIELD",
        help="key field; repeat for a composite key",
    )
    compare.add_argument(
        "--compare",
        action="append",
        dest="compare_fields",
        metavar="FIELD",
        help="field to compare; default is every shared non-key field",
    )
    compare.add_argument(
        "--ignore",
        action="append",
        default=[],
        dest="ignore_fields",
        metavar="FIELD",
        help="shared field to omit from comparison",
    )
    compare.add_argument(
        "--delimiter",
        help="one-character CSV delimiter; applies to CSV inputs only",
    )
    compare.add_argument(
        "--format",
        metavar="FORMAT",
        help="input format for both sides: csv, json, or ndjson",
    )
    compare.add_argument(
        "--left-format",
        metavar="FORMAT",
        help="override the detected or shared format for the left input",
    )
    compare.add_argument(
        "--right-format",
        metavar="FORMAT",
        help="override the detected or shared format for the right input",
    )
    compare.add_argument(
        "--ignore-key-case",
        action="store_true",
        help="match textual keys without regard to letter case",
    )
    compare.add_argument(
        "--normalize",
        action="append",
        default=[],
        metavar="FIELD=TYPE",
        help="normalize a field as text, integer, decimal, or date; repeat as needed",
    )
    compare.add_argument(
        "--output",
        type=Path,
        help="write the JSON report to this path instead of stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit status."""

    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.command != "compare":
        parser.error(f"unsupported command: {arguments.command}")

    try:
        shared_format = DatasetFormat.named(arguments.format) if arguments.format else None
        left_format = (
            DatasetFormat.named(arguments.left_format) if arguments.left_format else shared_format
        )
        right_format = (
            DatasetFormat.named(arguments.right_format) if arguments.right_format else shared_format
        )
        left = read_dataset(
            arguments.left,
            DatasetReadOptions(format=left_format, delimiter=arguments.delimiter),
        )
        right = read_dataset(
            arguments.right,
            DatasetReadOptions(format=right_format, delimiter=arguments.delimiter),
        )
        config = ReconciliationConfig(
            key_fields=tuple(arguments.key_fields),
            compare_fields=(tuple(arguments.compare_fields) if arguments.compare_fields else None),
            ignore_fields=tuple(arguments.ignore_fields),
            case_sensitive_keys=not arguments.ignore_key_case,
            field_normalizers=_parse_normalizers(arguments.normalize),
        )
        result = reconcile(left, right, config)
    except (ReconForgeError, ValueError) as exc:
        print(f"reconforge: error: {exc}", file=sys.stderr)
        return 2

    if arguments.output:
        destination = write_json_report(result, arguments.output)
        print(_summary(result_to_dict(result), destination))
    else:
        print(json.dumps(result_to_dict(result), indent=2, ensure_ascii=False))

    return 0 if result.is_clean else 1


def _parse_normalizers(assignments: Sequence[str]) -> dict[str, FieldNormalizer]:
    normalizers: dict[str, FieldNormalizer] = {}
    for assignment in assignments:
        if "=" not in assignment:
            raise ValueError(f"invalid normalizer {assignment!r}; expected FIELD=TYPE")
        field, type_name = (part.strip() for part in assignment.split("=", 1))
        if not field or not type_name:
            raise ValueError(f"invalid normalizer {assignment!r}; expected FIELD=TYPE")
        if field in normalizers:
            raise ValueError(f"normalizer for field {field!r} was specified more than once")
        normalizers[field] = FieldNormalizer.named(type_name)
    return normalizers


def _summary(report: dict[str, object], destination: Path) -> str:
    summary = report["summary"]
    if not isinstance(summary, dict):
        raise TypeError("report summary must be a mapping")
    return (
        f"{report['status']}: {summary['matched_records']} matched record(s), "
        f"{summary['discrepancies']} discrepancy item(s); report: {destination}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
