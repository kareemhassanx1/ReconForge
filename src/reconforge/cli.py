"""Command-line entry point for ReconForge."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from reconforge.batch import (
    discover_directory_batch,
    load_batch_manifest,
    reconcile_batch,
)
from reconforge.batch_checkpoint import reconcile_batch_resumable
from reconforge.errors import ReconForgeError
from reconforge.exporters import (
    ReportFormat,
    infer_report_format,
    render_report,
    write_report,
)
from reconforge.normalization import FieldNormalizer
from reconforge.policies import ComparisonPolicy
from reconforge.project_config import ProjectConfig, load_project_config
from reconforge.query import (
    IssueKind,
    ResultQuery,
    SourceRow,
    query_result,
    result_page_to_dict,
)
from reconforge.readers import DatasetFormat, DatasetReadOptions, read_dataset
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.report import (
    batch_result_to_dict,
    load_json_report,
    result_to_dict,
)
from reconforge.run_store import RunInput, RunStore, StoredRun, stored_run_to_dict


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
        help="write the report to this path instead of stdout",
    )
    compare.add_argument(
        "--report-format",
        metavar="FORMAT",
        help="report format: json, csv, or html; overrides configuration and output suffix",
    )
    compare.add_argument(
        "--history-db",
        type=Path,
        help="record run metadata and summary in this local SQLite database",
    )

    batch = subcommands.add_parser("batch", help="compare a deterministic batch of datasets")
    batch.add_argument(
        "left_directory",
        nargs="?",
        type=Path,
        help="directory containing left-hand datasets",
    )
    batch.add_argument(
        "right_directory",
        nargs="?",
        type=Path,
        help="directory containing right-hand datasets",
    )
    batch.add_argument(
        "--manifest",
        type=Path,
        help="load explicitly named input pairs from a versioned JSON manifest",
    )
    _add_batch_reconciliation_arguments(batch)

    history = subcommands.add_parser("history", help="inspect locally stored run history")
    history_commands = history.add_subparsers(dest="history_command", required=True)
    history_list = history_commands.add_parser("list", help="list newest stored runs")
    history_list.add_argument("database", type=Path, help="SQLite history database")
    history_list.add_argument("--limit", type=int, default=20, help="runs to return (1-1000)")
    history_list.add_argument("--offset", type=int, default=0, help="newest runs to skip")
    history_show = history_commands.add_parser("show", help="show one stored run")
    history_show.add_argument("database", type=Path, help="SQLite history database")
    history_show.add_argument("run_id", type=int, help="positive run identifier")

    inspect = subcommands.add_parser(
        "inspect",
        help="filter and paginate issues in a saved JSON reconciliation report",
    )
    inspect.add_argument("report", type=Path, help="canonical single-result JSON report")
    inspect.add_argument(
        "--kind",
        action="append",
        metavar="KIND",
        help="issue kind to include; repeat for alternatives",
    )
    inspect.add_argument(
        "--key",
        action="append",
        metavar="JSON_ARRAY",
        help='exact record key as a JSON array, for example: ["INV-100"]',
    )
    inspect.add_argument(
        "--field",
        action="append",
        metavar="FIELD",
        help="field to include; repeat for alternatives",
    )
    inspect.add_argument(
        "--row",
        action="append",
        metavar="SIDE:ROW",
        help="source row to include, such as left:42; repeat for alternatives",
    )
    inspect.add_argument("--offset", type=int, default=0, help="matching issues to skip")
    inspect.add_argument("--limit", type=int, default=50, help="issues to return (1-1000)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit status."""

    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.command == "compare":
        return _run_compare(arguments)
    if arguments.command == "batch":
        return _run_batch(arguments)
    if arguments.command == "history":
        return _run_history(arguments)
    if arguments.command == "inspect":
        return _run_inspect(arguments)
    parser.error(f"unsupported command: {arguments.command}")
    return 2


@dataclass(frozen=True, slots=True)
class _ExecutionSettings:
    config: ReconciliationConfig
    left_options: DatasetReadOptions
    right_options: DatasetReadOptions
    report_format: ReportFormat


def _run_compare(arguments: argparse.Namespace) -> int:
    """Execute the single-pair compare command."""

    try:
        settings = _resolve_execution_settings(arguments)
        left = read_dataset(arguments.left, settings.left_options)
        right = read_dataset(arguments.right, settings.right_options)
        result = reconcile(left, right, settings.config)
        report = result_to_dict(result)
        stored = (
            _record_compare_run(arguments.history_db, report, left.source, right.source)
            if arguments.history_db
            else None
        )
    except (ReconForgeError, ValueError) as exc:
        print(f"reconforge: error: {exc}", file=sys.stderr)
        return 2

    if arguments.output:
        destination = write_report(report, arguments.output, settings.report_format)
        print(_summary(report, destination, stored))
    else:
        print(render_report(report, settings.report_format), end="")

    return 0 if result.is_clean else 1


def _run_batch(arguments: argparse.Namespace) -> int:
    """Execute the directory- or manifest-driven batch command."""

    try:
        settings = _resolve_execution_settings(arguments)
        if arguments.manifest is not None:
            if arguments.left_directory is not None or arguments.right_directory is not None:
                raise ValueError("--manifest cannot be combined with batch directory arguments")
            plan = load_batch_manifest(arguments.manifest)
        else:
            if arguments.left_directory is None or arguments.right_directory is None:
                raise ValueError("batch requires LEFT_DIRECTORY and RIGHT_DIRECTORY, or --manifest")
            plan = discover_directory_batch(
                arguments.left_directory,
                arguments.right_directory,
            )
        execution: dict[str, object] | None = None
        if arguments.checkpoint:
            resumed = reconcile_batch_resumable(
                plan,
                settings.config,
                arguments.checkpoint,
                left_options=settings.left_options,
                right_options=settings.right_options,
            )
            result = resumed.result
            execution = {
                "checkpoint": str(resumed.checkpoint),
                "reused_pairs": list(resumed.reused_pairs),
                "executed_pairs": list(resumed.executed_pairs),
            }
        else:
            result = reconcile_batch(
                plan,
                settings.config,
                left_options=settings.left_options,
                right_options=settings.right_options,
            )
        report = batch_result_to_dict(result, execution=execution)
        stored = _record_batch_run(arguments.history_db, report) if arguments.history_db else None
    except (ReconForgeError, ValueError) as exc:
        print(f"reconforge: error: {exc}", file=sys.stderr)
        return 2

    if arguments.output:
        destination = write_report(report, arguments.output, settings.report_format)
        print(_batch_summary(report, destination, stored))
    else:
        print(render_report(report, settings.report_format), end="")
    return 0 if result.is_clean else 1


def _run_history(arguments: argparse.Namespace) -> int:
    """Execute local history lookup and listing commands."""

    try:
        with RunStore(arguments.database) as store:
            if arguments.history_command == "list":
                runs = store.list_runs(limit=arguments.limit, offset=arguments.offset)
                payload = {
                    "schema_version": store.schema_version,
                    "runs": [stored_run_to_dict(run, include_metadata=False) for run in runs],
                }
            elif arguments.history_command == "show":
                payload = {
                    "schema_version": store.schema_version,
                    "run": stored_run_to_dict(store.get_run(arguments.run_id)),
                }
            else:
                raise ValueError(f"unsupported history command: {arguments.history_command}")
    except (ReconForgeError, ValueError) as exc:
        print(f"reconforge: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _run_inspect(arguments: argparse.Namespace) -> int:
    """Query one saved canonical report without changing its reconciliation outcome."""

    try:
        result = load_json_report(arguments.report)
        selected = ResultQuery(
            kinds=frozenset(IssueKind.named(name) for name in arguments.kind or ()),
            keys=frozenset(_parse_query_key(value) for value in arguments.key or ()),
            fields=frozenset(_parse_query_field(value) for value in arguments.field or ()),
            source_rows=frozenset(_parse_source_row(value) for value in arguments.row or ()),
            offset=arguments.offset,
            limit=arguments.limit,
        )
        page = query_result(result, selected)
    except (ReconForgeError, ValueError) as exc:
        print(f"reconforge: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result_page_to_dict(page), indent=2, ensure_ascii=False))
    return 0


def _resolve_execution_settings(arguments: argparse.Namespace) -> _ExecutionSettings:
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
    return _ExecutionSettings(
        config=ReconciliationConfig(
            key_fields=key_fields,
            compare_fields=compare_fields,
            ignore_fields=ignore_fields,
            case_sensitive_keys=case_sensitive_keys,
            field_normalizers=normalizers,
            comparison_policies=policies,
            validation_rules=project.validation_rules,
        ),
        left_options=DatasetReadOptions(format=left_format, delimiter=delimiter),
        right_options=DatasetReadOptions(format=right_format, delimiter=delimiter),
        report_format=_effective_report_format(
            arguments.report_format,
            project.report_format,
            arguments.output,
        ),
    )


def _add_batch_reconciliation_arguments(batch: argparse.ArgumentParser) -> None:
    """Add the shared reconciliation surface to the batch command."""

    batch.add_argument("--config", type=Path, help="load a versioned project configuration")
    batch.add_argument("--key", action="append", dest="key_fields", metavar="FIELD")
    batch.add_argument("--compare", action="append", dest="compare_fields", metavar="FIELD")
    batch.add_argument("--ignore", action="append", dest="ignore_fields", metavar="FIELD")
    batch.add_argument("--delimiter", help="one-character CSV delimiter")
    batch.add_argument("--format", metavar="FORMAT", help="input format for both sides")
    batch.add_argument("--left-format", metavar="FORMAT")
    batch.add_argument("--right-format", metavar="FORMAT")
    key_case = batch.add_mutually_exclusive_group()
    key_case.add_argument("--ignore-key-case", action="store_false", dest="case_sensitive_keys")
    key_case.add_argument("--case-sensitive-keys", action="store_true", dest="case_sensitive_keys")
    batch.set_defaults(case_sensitive_keys=None)
    batch.add_argument("--normalize", action="append", metavar="FIELD=TYPE")
    batch.add_argument("--policy", action="append", metavar="FIELD=SPEC")
    batch.add_argument("--output", type=Path, help="write the batch report to this path")
    batch.add_argument(
        "--report-format",
        metavar="FORMAT",
        help="report format: json, csv, or html; overrides configuration and output suffix",
    )
    batch.add_argument(
        "--checkpoint",
        type=Path,
        help="resume unchanged pairs from this durable content-addressed checkpoint",
    )
    batch.add_argument(
        "--history-db",
        type=Path,
        help="record run metadata and summary in this local SQLite database",
    )


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


def _effective_report_format(
    cli_format: str | None,
    configured_format: ReportFormat | None,
    output: Path | None,
) -> ReportFormat:
    if cli_format is not None:
        return ReportFormat.named(cli_format)
    if configured_format is not None:
        return configured_format
    if output is not None:
        inferred = infer_report_format(output)
        if inferred is not None:
            return inferred
    return ReportFormat.JSON


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


def _parse_query_key(value: str) -> tuple[str | None, ...]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid query key {value!r}; expected a JSON array: {exc.msg}") from exc
    if not isinstance(payload, list) or any(
        component is not None and not isinstance(component, str) for component in payload
    ):
        raise ValueError(f"invalid query key {value!r}; expected a JSON array of strings or null")
    return tuple(payload)


def _parse_query_field(value: str) -> str:
    field = value.strip()
    if not field:
        raise ValueError("query field must be a non-empty string")
    return field


def _parse_source_row(value: str) -> SourceRow:
    side, separator, row_text = value.partition(":")
    if not separator:
        raise ValueError(f"invalid source row {value!r}; expected SIDE:ROW")
    try:
        row_number = int(row_text)
    except ValueError as exc:
        raise ValueError(f"invalid source row {value!r}; ROW must be an integer") from exc
    try:
        return SourceRow(side.strip().lower(), row_number)
    except ValueError as exc:
        raise ValueError(f"invalid source row {value!r}: {exc}") from exc


def _summary(report: dict[str, object], destination: Path, stored: StoredRun | None = None) -> str:
    summary = report["summary"]
    if not isinstance(summary, dict):
        raise TypeError("report summary must be a mapping")
    message = (
        f"{report['status']}: {summary['matched_records']} matched record(s), "
        f"{summary['discrepancies']} reconciliation discrepancy item(s), "
        f"{summary['validation_violations']} validation violation(s); "
        f"report: {destination}"
    )
    return _with_history(message, stored)


def _batch_summary(
    report: dict[str, object], destination: Path, stored: StoredRun | None = None
) -> str:
    summary = report["summary"]
    if not isinstance(summary, dict):
        raise TypeError("batch report summary must be a mapping")
    message = (
        f"{report['status']}: {summary['paired_inputs']} pair(s), "
        f"{summary['issues']} issue(s), "
        f"{summary['unmatched_left']} unmatched left input(s), "
        f"{summary['unmatched_right']} unmatched right input(s); "
        f"report: {destination}"
    )
    return _with_history(message, stored)


def _record_compare_run(
    database: Path,
    report: dict[str, object],
    left_source: str,
    right_source: str,
) -> StoredRun:
    with RunStore(database) as store:
        return store.record_run(
            command="compare",
            status=str(report["status"]),
            summary=_mapping(report["summary"], "report summary"),
            metadata={
                "report_version": report["report_version"],
                "configuration": report["configuration"],
                "schema": report["schema"],
            },
            inputs=(
                RunInput("left", left_source),
                RunInput("right", right_source),
            ),
        )


def _record_batch_run(database: Path, report: dict[str, object]) -> StoredRun:
    pairs = report["pairs"]
    unmatched = _mapping(report["unmatched_inputs"], "unmatched inputs")
    if not isinstance(pairs, list):
        raise TypeError("batch report pairs must be a list")
    inputs: list[RunInput] = []
    pair_metadata: list[dict[str, object]] = []
    for raw_pair in pairs:
        pair = _mapping(raw_pair, "batch pair")
        name = str(pair["name"])
        result = _mapping(pair["result"], "batch pair result")
        inputs.extend(
            (
                RunInput("left", str(pair["left"]), name),
                RunInput("right", str(pair["right"]), name),
            )
        )
        pair_metadata.append({"name": name, "status": result["status"]})
    for side in ("left", "right"):
        sources = unmatched[side]
        if not isinstance(sources, list):
            raise TypeError(f"unmatched {side} inputs must be a list")
        inputs.extend(RunInput(side, str(source)) for source in sources)

    with RunStore(database) as store:
        return store.record_run(
            command="batch",
            status=str(report["status"]),
            summary=_mapping(report["summary"], "batch report summary"),
            metadata={
                "batch_report_version": report["batch_report_version"],
                "pairs": pair_metadata,
                "unmatched_inputs": dict(unmatched),
            },
            inputs=inputs,
        )


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a mapping")
    return value


def _with_history(message: str, stored: StoredRun | None) -> str:
    return message if stored is None else f"{message}; history run: {stored.id}"


if __name__ == "__main__":
    raise SystemExit(main())
