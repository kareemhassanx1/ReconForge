"""Command-line entry point for ReconForge."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from reconforge.errors import ReconForgeError
from reconforge.normalization import FieldNormalizer
from reconforge.policies import ComparisonPolicy
from reconforge.project_config import ProjectConfig, load_project_config
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
        "--config",
        type=Path,
        help="load defaults from a versioned ReconForge JSON configuration",
    )
    compare.add_argument(
        "--key",
        action="append",
        dest="key_fields",
        metavar="FIELD",
        help="key field; repeat for a composite key; overrides configured key fields",
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
        dest="ignore_fields",
        metavar="FIELD",
        help="shared field to omit; repeat as needed; overrides configured ignored fields",
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
    key_case = compare.add_mutually_exclusive_group()
    key_case.add_argument(
        "--ignore-key-case",
        action="store_false",
        dest="case_sensitive_keys",
        help="match textual keys without regard to case; overrides configuration",
    )
    key_case.add_argument(
        "--case-sensitive-keys",
        action="store_true",
        dest="case_sensitive_keys",
        help="match textual keys with case sensitivity; overrides configuration",
    )
    compare.set_defaults(case_sensitive_keys=None)
    compare.add_argument(
        "--normalize",
        action="append",
        metavar="FIELD=TYPE",
        help=(
            "normalize a field as text, integer, decimal, or date; "
            "merges with configuration by field"
        ),
    )
    compare.add_argument(
        "--policy",
        action="append",
        metavar="FIELD=SPEC",
        help=(
            "compare a field using exact, decimal[:tolerance], date, or text; "
            "merges with configuration by field"
        ),
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
        project = load_project_config(arguments.config) if arguments.config else ProjectConfig()
        left_format = _effective_format(
            arguments.left_format,
            arguments.format,
            project.left_format,
            project.format,
        )
        right_format = _effective_format(
            arguments.right_format,
            arguments.format,
            project.right_format,
            project.format,
        )
        delimiter = arguments.delimiter if arguments.delimiter is not None else project.delimiter
        key_fields = (
            tuple(arguments.key_fields) if arguments.key_fields is not None else project.key_fields
        )
        if not key_fields:
            raise ValueError(
                "at least one key field is required; provide --key or "
                "reconciliation.key_fields in --config"
            )
        compare_fields = (
            tuple(arguments.compare_fields)
            if arguments.compare_fields is not None
            else project.compare_fields
        )
        ignore_fields = (
            tuple(arguments.ignore_fields)
            if arguments.ignore_fields is not None
            else project.ignore_fields
        )
        case_sensitive_keys = (
            arguments.case_sensitive_keys
            if arguments.case_sensitive_keys is not None
            else project.case_sensitive_keys
        )
        normalizers = dict(project.field_normalizers)
        normalizers.update(_parse_normalizers(arguments.normalize or ()))
        policies = dict(project.comparison_policies)
        policies.update(_parse_policies(arguments.policy or ()))
        config = ReconciliationConfig(
            key_fields=key_fields,
            compare_fields=compare_fields,
            ignore_fields=ignore_fields,
            case_sensitive_keys=case_sensitive_keys,
            field_normalizers=normalizers,
            comparison_policies=policies,
        )
        left = read_dataset(
            arguments.left,
            DatasetReadOptions(format=left_format, delimiter=delimiter),
        )
        right = read_dataset(
            arguments.right,
            DatasetReadOptions(format=right_format, delimiter=delimiter),
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


def _effective_format(
    side_cli: str | None,
    shared_cli: str | None,
    side_config: DatasetFormat | None,
    shared_config: DatasetFormat | None,
) -> DatasetFormat | None:
    if side_cli is not None:
        return DatasetFormat.named(side_cli)
    if shared_cli is not None:
        return DatasetFormat.named(shared_cli)
    return side_config or shared_config


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


def _parse_policies(assignments: Sequence[str]) -> dict[str, ComparisonPolicy]:
    policies: dict[str, ComparisonPolicy] = {}
    for assignment in assignments:
        if "=" not in assignment:
            raise ValueError(f"invalid comparison policy {assignment!r}; expected FIELD=SPEC")
        field, specification = (part.strip() for part in assignment.split("=", 1))
        if not field or not specification:
            raise ValueError(f"invalid comparison policy {assignment!r}; expected FIELD=SPEC")
        if field in policies:
            raise ValueError(f"comparison policy for field {field!r} was specified more than once")
        policies[field] = ComparisonPolicy.named(specification)
    return policies


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
