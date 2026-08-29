"""Stable serialization for reconciliation results."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from reconforge.batch import BatchResult
from reconforge.model import (
    DuplicateKey,
    FieldDifference,
    MissingRecord,
    ReconciliationResult,
    ValidationViolation,
)


def result_to_dict(result: ReconciliationResult) -> dict[str, Any]:
    """Convert a reconciliation result into a versioned report structure."""

    return {
        "report_version": 1,
        "status": _result_status(result),
        "summary": {
            "matched_records": result.matched_count,
            "discrepancies": result.difference_count,
            "validation_violations": result.validation_violation_count,
            "issues": result.issue_count,
            "field_differences": len(result.differences),
            "missing_from_left": len(result.missing_from_left),
            "missing_from_right": len(result.missing_from_right),
            "duplicate_keys_left": len(result.duplicate_left),
            "duplicate_keys_right": len(result.duplicate_right),
        },
        "configuration": {
            "key_fields": list(result.key_fields),
            "compared_fields": list(result.compared_fields),
            "ignored_fields": list(result.ignored_fields),
            "case_sensitive_keys": result.case_sensitive_keys,
            "field_normalizers": dict(result.field_normalizers),
            "comparison_policies": dict(result.comparison_policies),
            "validation_rules": {field: list(rules) for field, rules in result.validation_rules},
        },
        "schema": {
            "fields_only_left": list(result.fields_only_left),
            "fields_only_right": list(result.fields_only_right),
        },
        "differences": [
            {
                "key": list(item.key),
                "field": item.field,
                "left_value": item.left_value,
                "right_value": item.right_value,
                "left_row": item.left_row,
                "right_row": item.right_row,
            }
            for item in result.differences
        ],
        "validation_violations": [
            {
                "side": item.side,
                "source": item.source,
                "row_number": item.row_number,
                "field": item.field,
                "value": item.value,
                "rule": item.rule,
                "message": item.message,
            }
            for item in result.validation_violations
        ],
        "missing_records": {
            "from_left": [
                {"key": list(item.key), "right_row": item.row_number}
                for item in result.missing_from_left
            ],
            "from_right": [
                {"key": list(item.key), "left_row": item.row_number}
                for item in result.missing_from_right
            ],
        },
        "duplicate_keys": {
            "left": [
                {"key": list(item.key), "row_numbers": list(item.row_numbers)}
                for item in result.duplicate_left
            ],
            "right": [
                {"key": list(item.key), "row_numbers": list(item.row_numbers)}
                for item in result.duplicate_right
            ],
        },
    }


def result_from_dict(payload: Mapping[str, object]) -> ReconciliationResult:
    """Reconstruct a result from ReconForge's canonical version-one report model."""

    report = _object(payload, "report")
    if report.get("report_version") != 1:
        raise ValueError(f"unsupported report version {report.get('report_version')!r}; expected 1")
    summary = _object(report.get("summary"), "report.summary")
    configuration = _object(report.get("configuration"), "report.configuration")
    schema = _object(report.get("schema"), "report.schema")
    missing = _object(report.get("missing_records"), "report.missing_records")
    duplicates = _object(report.get("duplicate_keys"), "report.duplicate_keys")
    normalizers = _string_map(
        configuration.get("field_normalizers"),
        "report.configuration.field_normalizers",
    )
    policies = _string_map(
        configuration.get("comparison_policies"),
        "report.configuration.comparison_policies",
    )
    raw_rules = _object(
        configuration.get("validation_rules"),
        "report.configuration.validation_rules",
    )
    validation_rules = tuple(
        (
            field,
            _string_tuple(raw_rules[field], f"report.configuration.validation_rules.{field}"),
        )
        for field in sorted(raw_rules)
    )

    return ReconciliationResult(
        key_fields=_string_tuple(
            configuration.get("key_fields"), "report.configuration.key_fields"
        ),
        compared_fields=_string_tuple(
            configuration.get("compared_fields"), "report.configuration.compared_fields"
        ),
        ignored_fields=_string_tuple(
            configuration.get("ignored_fields"), "report.configuration.ignored_fields"
        ),
        case_sensitive_keys=_boolean(
            configuration.get("case_sensitive_keys"),
            "report.configuration.case_sensitive_keys",
        ),
        field_normalizers=tuple(sorted(normalizers.items())),
        comparison_policies=tuple(sorted(policies.items())),
        validation_rules=validation_rules,
        validation_violations=tuple(
            _validation_violation(item, index)
            for index, item in enumerate(
                _array(report.get("validation_violations"), "report.validation_violations")
            )
        ),
        matched_count=_integer(summary.get("matched_records"), "report.summary.matched_records"),
        differences=tuple(
            _field_difference(item, index)
            for index, item in enumerate(_array(report.get("differences"), "report.differences"))
        ),
        missing_from_left=tuple(
            _missing_record(item, index, "right_row", "from_left")
            for index, item in enumerate(
                _array(missing.get("from_left"), "report.missing_records.from_left")
            )
        ),
        missing_from_right=tuple(
            _missing_record(item, index, "left_row", "from_right")
            for index, item in enumerate(
                _array(missing.get("from_right"), "report.missing_records.from_right")
            )
        ),
        duplicate_left=tuple(
            _duplicate_key(item, index, "left")
            for index, item in enumerate(
                _array(duplicates.get("left"), "report.duplicate_keys.left")
            )
        ),
        duplicate_right=tuple(
            _duplicate_key(item, index, "right")
            for index, item in enumerate(
                _array(duplicates.get("right"), "report.duplicate_keys.right")
            )
        ),
        fields_only_left=_string_tuple(
            schema.get("fields_only_left"), "report.schema.fields_only_left"
        ),
        fields_only_right=_string_tuple(
            schema.get("fields_only_right"), "report.schema.fields_only_right"
        ),
    )


def _result_status(result: ReconciliationResult) -> str:
    has_differences = result.difference_count > 0
    has_violations = result.validation_violation_count > 0
    if has_differences and has_violations:
        return "differences_and_validation_violations_found"
    if has_differences:
        return "differences_found"
    if has_violations:
        return "validation_violations_found"
    return "clean"


def write_json_report(result: ReconciliationResult, destination: str | Path) -> Path:
    """Write a report atomically and return its resolved destination."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    payload = json.dumps(result_to_dict(result), indent=2, ensure_ascii=False) + "\n"
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
    return path.resolve()


def batch_result_to_dict(
    result: BatchResult,
    *,
    execution: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Convert a batch result into a stable aggregate report."""

    pair_reports = [
        {
            "name": item.pair.name,
            "left": str(item.pair.left),
            "right": str(item.pair.right),
            "result": result_to_dict(item.result),
        }
        for item in result.pairs
    ]
    report: dict[str, Any] = {
        "batch_report_version": 1,
        "status": "clean" if result.is_clean else "issues_found",
        "summary": {
            "paired_inputs": len(result.pairs),
            "clean_pairs": sum(item.result.is_clean for item in result.pairs),
            "pairs_with_issues": sum(not item.result.is_clean for item in result.pairs),
            "matched_records": sum(item.result.matched_count for item in result.pairs),
            "discrepancies": sum(item.result.difference_count for item in result.pairs),
            "validation_violations": sum(
                item.result.validation_violation_count for item in result.pairs
            ),
            "unmatched_left": len(result.unmatched_left),
            "unmatched_right": len(result.unmatched_right),
            "issues": result.issue_count,
        },
        "unmatched_inputs": {
            "left": [str(path) for path in result.unmatched_left],
            "right": [str(path) for path in result.unmatched_right],
        },
        "pairs": pair_reports,
    }
    if execution is not None:
        report["execution"] = dict(execution)
    return report


def write_json_batch_report(
    result: BatchResult,
    destination: str | Path,
    *,
    execution: Mapping[str, object] | None = None,
) -> Path:
    """Write a batch report atomically and return its resolved destination."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    payload = (
        json.dumps(
            batch_result_to_dict(result, execution=execution),
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
    return path.resolve()


def _object(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{location} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{location} keys must be strings")
    return dict(value)


def _array(value: object, location: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{location} must be an array")
    return value


def _string_tuple(value: object, location: str) -> tuple[str, ...]:
    items = _array(value, location)
    if any(not isinstance(item, str) for item in items):
        raise ValueError(f"{location} must contain only strings")
    return tuple(items)  # type: ignore[arg-type]


def _string_map(value: object, location: str) -> dict[str, str]:
    payload = _object(value, location)
    if any(not isinstance(item, str) for item in payload.values()):
        raise ValueError(f"{location} values must be strings")
    return payload  # type: ignore[return-value]


def _integer(value: object, location: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{location} must be a non-negative integer")
    return value


def _positive_integer(value: object, location: str) -> int:
    result = _integer(value, location)
    if result < 1:
        raise ValueError(f"{location} must be a positive integer")
    return result


def _boolean(value: object, location: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{location} must be a boolean")
    return value


def _cell(value: object, location: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{location} must be a string or null")
    return value


def _record_key(value: object, location: str) -> tuple[str | None, ...]:
    return tuple(
        _cell(item, f"{location}[{index}]") for index, item in enumerate(_array(value, location))
    )


def _field_difference(value: object, index: int) -> FieldDifference:
    location = f"report.differences[{index}]"
    item = _object(value, location)
    field = item.get("field")
    if not isinstance(field, str):
        raise ValueError(f"{location}.field must be a string")
    return FieldDifference(
        key=_record_key(item.get("key"), f"{location}.key"),
        field=field,
        left_value=_cell(item.get("left_value"), f"{location}.left_value"),
        right_value=_cell(item.get("right_value"), f"{location}.right_value"),
        left_row=_positive_integer(item.get("left_row"), f"{location}.left_row"),
        right_row=_positive_integer(item.get("right_row"), f"{location}.right_row"),
    )


def _validation_violation(value: object, index: int) -> ValidationViolation:
    location = f"report.validation_violations[{index}]"
    item = _object(value, location)
    strings: dict[str, str] = {}
    for name in ("side", "source", "field", "rule", "message"):
        field_value = item.get(name)
        if not isinstance(field_value, str):
            raise ValueError(f"{location}.{name} must be a string")
        strings[name] = field_value
    return ValidationViolation(
        side=strings["side"],
        source=strings["source"],
        row_number=_positive_integer(item.get("row_number"), f"{location}.row_number"),
        field=strings["field"],
        value=_cell(item.get("value"), f"{location}.value"),
        rule=strings["rule"],
        message=strings["message"],
    )


def _missing_record(value: object, index: int, row_key: str, side: str) -> MissingRecord:
    location = f"report.missing_records.{side}[{index}]"
    item = _object(value, location)
    return MissingRecord(
        key=_record_key(item.get("key"), f"{location}.key"),
        row_number=_positive_integer(item.get(row_key), f"{location}.{row_key}"),
    )


def _duplicate_key(value: object, index: int, side: str) -> DuplicateKey:
    location = f"report.duplicate_keys.{side}[{index}]"
    item = _object(value, location)
    rows = _array(item.get("row_numbers"), f"{location}.row_numbers")
    return DuplicateKey(
        key=_record_key(item.get("key"), f"{location}.key"),
        row_numbers=tuple(
            _positive_integer(row, f"{location}.row_numbers[{row_index}]")
            for row_index, row in enumerate(rows)
        ),
    )
