"""Immutable semantic model for canonical batch reconciliation reports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from reconforge.batch import BatchResult
from reconforge.errors import InputError
from reconforge.json_safety import JsonNestingTooDeepError, validate_json_nesting
from reconforge.model import ReconciliationResult
from reconforge.report import result_fingerprint, result_from_dict, result_to_dict

BATCH_REPORT_VERSION = 1
_EXECUTION_FIELDS = frozenset({"checkpoint", "executed_pairs", "reused_pairs"})
_BATCH_REPORT_FIELDS = frozenset(
    {"batch_report_version", "status", "summary", "unmatched_inputs", "pairs"}
)
_BATCH_SUMMARY_FIELDS = frozenset(
    {
        "paired_inputs",
        "clean_pairs",
        "pairs_with_issues",
        "matched_records",
        "discrepancies",
        "validation_violations",
        "unmatched_left",
        "unmatched_right",
        "issues",
    }
)
_UNMATCHED_FIELDS = frozenset({"left", "right"})
_PAIR_FIELDS = frozenset({"name", "left", "right", "result"})


@dataclass(frozen=True, slots=True)
class BatchReportPair:
    """One named input pair and its strictly typed reconciliation result."""

    name: str
    left: str
    right: str
    result: ReconciliationResult

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_text(self.name, "batch report pair name"))
        _require_source(self.left, "batch report left source")
        _require_source(self.right, "batch report right source")
        if not isinstance(self.result, ReconciliationResult):
            raise TypeError("batch report pair result must be a ReconciliationResult instance")


@dataclass(frozen=True, slots=True)
class BatchExecution:
    """Optional operational provenance for a resumable batch execution."""

    checkpoint: str
    executed_pairs: tuple[str, ...]
    reused_pairs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_source(self.checkpoint, "batch execution checkpoint")
        executed = _pair_names(self.executed_pairs, "executed_pairs")
        reused = _pair_names(self.reused_pairs, "reused_pairs")
        overlap = sorted(set(executed) & set(reused), key=_sort_text)
        if overlap:
            raise ValueError(
                "batch execution pair names cannot be both executed and reused: "
                + ", ".join(repr(name) for name in overlap)
            )
        object.__setattr__(self, "executed_pairs", tuple(sorted(executed, key=_sort_text)))
        object.__setattr__(self, "reused_pairs", tuple(sorted(reused, key=_sort_text)))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BatchExecution:
        """Construct provenance from the canonical report mapping."""

        if not isinstance(value, Mapping):
            raise TypeError("batch execution must be a mapping")
        fields = frozenset(value)
        if fields != _EXECUTION_FIELDS:
            missing = sorted(_EXECUTION_FIELDS - fields)
            unknown = sorted(fields - _EXECUTION_FIELDS)
            details = []
            if missing:
                details.append("missing: " + ", ".join(missing))
            if unknown:
                details.append("unknown: " + ", ".join(unknown))
            raise ValueError("invalid batch execution fields (" + "; ".join(details) + ")")
        return cls(
            checkpoint=_source_text(value["checkpoint"], "batch execution checkpoint"),
            executed_pairs=_string_array(value["executed_pairs"], "executed_pairs"),
            reused_pairs=_string_array(value["reused_pairs"], "reused_pairs"),
        )


@dataclass(frozen=True, slots=True)
class BatchReport:
    """Canonical batch outcome reconstructed without touching archived inputs."""

    pairs: tuple[BatchReportPair, ...]
    unmatched_left: tuple[str, ...] = ()
    unmatched_right: tuple[str, ...] = ()
    execution: BatchExecution | None = None

    def __post_init__(self) -> None:
        pairs = tuple(self.pairs)
        if any(not isinstance(pair, BatchReportPair) for pair in pairs):
            raise TypeError("batch report pairs must be BatchReportPair instances")
        names = [pair.name.casefold() for pair in pairs]
        if len(names) != len(set(names)):
            raise ValueError("batch report pair names must be unique ignoring case")
        left = _sources(self.unmatched_left, "unmatched left inputs")
        right = _sources(self.unmatched_right, "unmatched right inputs")
        _validate_source_scope(pairs, left, right)
        if self.execution is not None:
            if not isinstance(self.execution, BatchExecution):
                raise TypeError("batch report execution must be a BatchExecution or null")
            covered = set(self.execution.executed_pairs) | set(self.execution.reused_pairs)
            expected = {pair.name for pair in pairs}
            if covered != expected:
                missing = sorted(expected - covered, key=_sort_text)
                unknown = sorted(covered - expected, key=_sort_text)
                details = []
                if missing:
                    details.append("missing: " + ", ".join(repr(name) for name in missing))
                if unknown:
                    details.append("unknown: " + ", ".join(repr(name) for name in unknown))
                raise ValueError(
                    "batch execution must cover every report pair exactly once ("
                    + "; ".join(details)
                    + ")"
                )
        object.__setattr__(
            self, "pairs", tuple(sorted(pairs, key=lambda pair: _sort_text(pair.name)))
        )
        object.__setattr__(self, "unmatched_left", tuple(sorted(left, key=_sort_text)))
        object.__setattr__(self, "unmatched_right", tuple(sorted(right, key=_sort_text)))

    @property
    def is_clean(self) -> bool:
        """Whether every pair is clean and every input was paired."""

        return (
            not self.unmatched_left
            and not self.unmatched_right
            and all(pair.result.is_clean for pair in self.pairs)
        )

    @property
    def issue_count(self) -> int:
        """Return aggregate pair issues plus unpaired input issues."""

        return (
            sum(pair.result.issue_count for pair in self.pairs)
            + len(self.unmatched_left)
            + len(self.unmatched_right)
        )


def batch_report_from_result(
    result: BatchResult,
    *,
    execution: Mapping[str, object] | None = None,
) -> BatchReport:
    """Create the report model for a freshly computed batch result."""

    if not isinstance(result, BatchResult):
        raise TypeError("result must be a BatchResult instance")
    provenance = None if execution is None else BatchExecution.from_mapping(execution)
    return BatchReport(
        pairs=tuple(
            BatchReportPair(
                name=item.pair.name,
                left=str(item.pair.left),
                right=str(item.pair.right),
                result=item.result,
            )
            for item in result.pairs
        ),
        unmatched_left=tuple(str(path) for path in result.unmatched_left),
        unmatched_right=tuple(str(path) for path in result.unmatched_right),
        execution=provenance,
    )


def batch_report_from_dict(payload: Mapping[str, object]) -> BatchReport:
    """Parse the supported canonical batch-report structure into its semantic model."""

    raw_report = _object(payload, "batch report")
    version = raw_report.get("batch_report_version")
    if type(version) is not int or version != BATCH_REPORT_VERSION:
        raise ValueError(
            f"unsupported batch report version {version!r}; expected {BATCH_REPORT_VERSION}"
        )
    report = _exact_object(
        raw_report,
        "batch report",
        _BATCH_REPORT_FIELDS,
        optional=frozenset({"execution"}),
    )
    status = _canonical_text(report["status"], "batch report.status")
    _exact_object(report["summary"], "batch report.summary", _BATCH_SUMMARY_FIELDS)
    unmatched = _exact_object(
        report["unmatched_inputs"],
        "batch report.unmatched_inputs",
        _UNMATCHED_FIELDS,
    )
    unmatched_left = _report_string_array(unmatched["left"], "batch report.unmatched_inputs.left")
    unmatched_right = _report_string_array(
        unmatched["right"], "batch report.unmatched_inputs.right"
    )
    raw_pairs = report["pairs"]
    if not isinstance(raw_pairs, list):
        raise ValueError("batch report.pairs must be an array")
    pairs = tuple(_parse_report_pair(value, index) for index, value in enumerate(raw_pairs))
    execution = (
        BatchExecution.from_mapping(_object(report["execution"], "batch report.execution"))
        if "execution" in report
        else None
    )
    parsed = BatchReport(
        pairs=pairs,
        unmatched_left=unmatched_left,
        unmatched_right=unmatched_right,
        execution=execution,
    )
    _validate_summary(report["summary"], parsed)
    _validate_status(status, parsed)
    return parsed


def load_json_batch_report(path: str | Path) -> BatchReport:
    """Load a UTF-8 batch report with duplicate-key and semantic validation."""

    source, payload = _read_report_json(path)
    try:
        return batch_report_from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise InputError(f"{source}: invalid batch reconciliation report: {exc}") from exc


def load_saved_report(path: str | Path) -> ReconciliationResult | BatchReport:
    """Load a canonical single or batch report using unambiguous version markers."""

    source, payload = _read_report_json(path)
    has_single_version = "report_version" in payload
    has_batch_version = "batch_report_version" in payload
    if has_single_version and has_batch_version:
        raise InputError(
            f"{source}: report is ambiguous because it contains both report_version "
            "and batch_report_version"
        )
    try:
        if has_batch_version:
            return batch_report_from_dict(payload)
        if has_single_version:
            return result_from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise InputError(f"{source}: invalid reconciliation report: {exc}") from exc
    raise InputError(f"{source}: report must contain either report_version or batch_report_version")


def batch_report_to_dict(report: BatchReport) -> dict[str, Any]:
    """Serialize an immutable batch report into the version-one JSON model."""

    if not isinstance(report, BatchReport):
        raise TypeError("report must be a BatchReport instance")
    payload: dict[str, Any] = {
        "batch_report_version": BATCH_REPORT_VERSION,
        "status": "clean" if report.is_clean else "issues_found",
        "summary": {
            "paired_inputs": len(report.pairs),
            "clean_pairs": sum(pair.result.is_clean for pair in report.pairs),
            "pairs_with_issues": sum(not pair.result.is_clean for pair in report.pairs),
            "matched_records": sum(pair.result.matched_count for pair in report.pairs),
            "discrepancies": sum(pair.result.difference_count for pair in report.pairs),
            "validation_violations": sum(
                pair.result.validation_violation_count for pair in report.pairs
            ),
            "unmatched_left": len(report.unmatched_left),
            "unmatched_right": len(report.unmatched_right),
            "issues": report.issue_count,
        },
        "unmatched_inputs": {
            "left": list(report.unmatched_left),
            "right": list(report.unmatched_right),
        },
        "pairs": [
            {
                "name": pair.name,
                "left": pair.left,
                "right": pair.right,
                "result": result_to_dict(pair.result),
            }
            for pair in report.pairs
        ],
    }
    if report.execution is not None:
        payload["execution"] = {
            "checkpoint": report.execution.checkpoint,
            "executed_pairs": list(report.execution.executed_pairs),
            "reused_pairs": list(report.execution.reused_pairs),
        }
    return payload


def batch_report_fingerprint(report: BatchReport) -> str:
    """Return a semantic checksum that excludes operational execution provenance."""

    if not isinstance(report, BatchReport):
        raise TypeError("report must be a BatchReport instance")
    canonical_report = batch_report_to_dict(report)
    canonical_report.pop("execution", None)
    canonical = json.dumps(
        canonical_report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def batch_pair_fingerprints(report: BatchReport) -> tuple[tuple[str, str], ...]:
    """Return stable per-pair fingerprints for targeted integrity diagnosis."""

    if not isinstance(report, BatchReport):
        raise TypeError("report must be a BatchReport instance")
    return tuple((pair.name, result_fingerprint(pair.result)) for pair in report.pairs)


def _parse_report_pair(value: object, index: int) -> BatchReportPair:
    location = f"batch report.pairs[{index}]"
    pair = _exact_object(value, location, _PAIR_FIELDS)
    name = _canonical_text(pair["name"], f"{location}.name")
    left = _source_text(pair["left"], f"{location}.left")
    right = _source_text(pair["right"], f"{location}.right")
    try:
        result = result_from_dict(_object(pair["result"], f"{location}.result"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{location}.result: {exc}") from exc
    return BatchReportPair(name, left, right, result)


def _validate_summary(value: object, report: BatchReport) -> None:
    summary = _exact_object(value, "batch report.summary", _BATCH_SUMMARY_FIELDS)
    actual = {
        field: _nonnegative_integer(summary[field], f"batch report.summary.{field}")
        for field in sorted(_BATCH_SUMMARY_FIELDS)
    }
    expected = batch_report_to_dict(report)["summary"]
    for field in sorted(_BATCH_SUMMARY_FIELDS):
        if actual[field] != expected[field]:
            raise ValueError(
                f"batch report.summary.{field} is {actual[field]}; expected {expected[field]}"
            )


def _validate_status(value: str, report: BatchReport) -> None:
    expected = "clean" if report.is_clean else "issues_found"
    if value != expected:
        raise ValueError(f"batch report.status is {value!r}; expected {expected!r}")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _canonical_text(value: object, label: str) -> str:
    text = _required_text(value, label)
    if value != text:
        raise ValueError(f"{label} must not have leading or trailing whitespace")
    return text


def _nonnegative_integer(value: object, location: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{location} must be a non-negative integer")
    return value


def _source_text(value: object, label: str) -> str:
    _require_source(value, label)
    return value


def _require_source(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _string_array(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"batch execution {label} must be an array")
    return tuple(_canonical_text(item, f"batch execution {label} item") for item in value)


def _report_string_array(value: object, location: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{location} must be an array")
    return tuple(_source_text(item, f"{location}[{index}]") for index, item in enumerate(value))


def _pair_names(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    names = tuple(_required_text(value, f"batch execution {label} item") for value in values)
    if len(names) != len(set(names)):
        raise ValueError(f"batch execution {label} must contain unique pair names")
    return names


def _sources(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    sources = tuple(values)
    for value in sources:
        _require_source(value, f"batch report {label} item")
    if len(sources) != len(set(sources)):
        raise ValueError(f"batch report {label} must contain unique sources")
    return sources


def _validate_source_scope(
    pairs: tuple[BatchReportPair, ...],
    unmatched_left: tuple[str, ...],
    unmatched_right: tuple[str, ...],
) -> None:
    for side, paired, unmatched in (
        ("left", [pair.left for pair in pairs], unmatched_left),
        ("right", [pair.right for pair in pairs], unmatched_right),
    ):
        if len(paired) != len(set(paired)):
            raise ValueError(f"batch report {side} sources must be assigned to only one pair")
        overlap = sorted(set(paired) & set(unmatched), key=_sort_text)
        if overlap:
            raise ValueError(
                f"batch report {side} sources cannot be both paired and unmatched: "
                + ", ".join(repr(source) for source in overlap)
            )


def _sort_text(value: str) -> tuple[str, str]:
    return value.casefold(), value


def _read_report_json(path: str | Path) -> tuple[Path, dict[str, object]]:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise InputError(f"reconciliation report does not exist: {source}") from exc
    except UnicodeError as exc:
        raise InputError(f"reconciliation report is not valid UTF-8: {source}") from exc
    except OSError as exc:
        raise InputError(f"cannot read reconciliation report {source}: {exc}") from exc
    try:
        payload = json.loads(text, object_pairs_hook=_unique_object)
        validate_json_nesting(payload)
    except _DuplicateObjectKeyError as exc:
        raise InputError(f"{source}: invalid report JSON: {exc}") from exc
    except (JsonNestingTooDeepError, RecursionError) as exc:
        raise InputError(f"{source}: report JSON exceeds the maximum nesting depth") from exc
    except JSONDecodeError as exc:
        raise InputError(
            f"{source}: invalid report JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise InputError(f"{source}: top-level report JSON value must be an object")
    return source, payload


class _DuplicateObjectKeyError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateObjectKeyError(f"duplicate key {key!r}")
        payload[key] = value
    return payload


def _object(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{location} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{location} keys must be strings")
    return dict(value)


def _exact_object(
    value: object,
    location: str,
    required: frozenset[str],
    *,
    optional: frozenset[str] = frozenset(),
) -> dict[str, object]:
    payload = _object(value, location)
    missing = sorted(required - payload.keys())
    unknown = sorted(payload.keys() - required - optional)
    if missing:
        raise ValueError(
            f"{location} is missing required field(s): "
            + ", ".join(repr(field) for field in missing)
        )
    if unknown:
        raise ValueError(
            f"{location} contains unknown field(s): " + ", ".join(repr(field) for field in unknown)
        )
    return payload
