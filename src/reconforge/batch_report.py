"""Immutable semantic model for canonical batch reconciliation reports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from reconforge.batch import BatchResult
from reconforge.model import ReconciliationResult
from reconforge.report import result_to_dict

BATCH_REPORT_VERSION = 1
_EXECUTION_FIELDS = frozenset({"checkpoint", "executed_pairs", "reused_pairs"})


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
            checkpoint=_required_text(value["checkpoint"], "batch execution checkpoint"),
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


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _require_source(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _string_array(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"batch execution {label} must be an array")
    return tuple(_required_text(item, f"batch execution {label} item") for item in value)


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
