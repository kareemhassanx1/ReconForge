"""Stable serialization for reconciliation results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reconforge.model import ReconciliationResult


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
