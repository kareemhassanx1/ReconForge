"""Stable serialization for reconciliation results."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from reconforge.atomic_io import atomic_write_text
from reconforge.batch import BatchResult
from reconforge.errors import InputError
from reconforge.model import (
    DuplicateKey,
    FieldDifference,
    MissingRecord,
    ReconciliationResult,
    ValidationViolation,
)
from reconforge.normalization import FieldNormalizer
from reconforge.policies import ComparisonPolicy
from reconforge.validation import (
    AllowedValuesRule,
    NumericRangeRule,
    RequiredRule,
    TextPatternRule,
    find_duplicate_validation_descriptors,
)

_REPORT_FIELDS = frozenset(
    {
        "report_version",
        "status",
        "summary",
        "configuration",
        "schema",
        "differences",
        "validation_violations",
        "missing_records",
        "duplicate_keys",
    }
)
_SUMMARY_FIELDS = frozenset(
    {
        "matched_records",
        "discrepancies",
        "validation_violations",
        "issues",
        "field_differences",
        "missing_from_left",
        "missing_from_right",
        "duplicate_keys_left",
        "duplicate_keys_right",
    }
)
_CONFIGURATION_FIELDS = frozenset(
    {
        "key_fields",
        "compared_fields",
        "ignored_fields",
        "case_sensitive_keys",
        "field_normalizers",
        "comparison_policies",
        "validation_rules",
    }
)
_SCHEMA_FIELDS = frozenset({"fields_only_left", "fields_only_right"})
_MISSING_FIELDS = frozenset({"from_left", "from_right"})
_DUPLICATE_FIELDS = frozenset({"left", "right"})
_DIFFERENCE_FIELDS = frozenset(
    {"key", "field", "left_value", "right_value", "left_row", "right_row"}
)
_VIOLATION_FIELDS = frozenset({"side", "source", "row_number", "field", "value", "rule", "message"})
_NUMERIC_RANGE_DESCRIPTOR = re.compile(
    r"numeric_range:(?P<left>[\[(])(?P<minimum>[^,]+),(?P<maximum>[^\])]+)(?P<right>[\])])"
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


def result_fingerprint(result: ReconciliationResult) -> str:
    """Return a deterministic semantic checksum for one reconciliation result."""

    if not isinstance(result, ReconciliationResult):
        raise TypeError("result must be a ReconciliationResult instance")
    canonical = json.dumps(
        result_to_dict(result),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def result_from_dict(payload: Mapping[str, object]) -> ReconciliationResult:
    """Reconstruct a result from ReconForge's canonical version-one report model."""

    report = _object(payload, "report")
    version = report.get("report_version")
    if type(version) is not int or version != 1:
        raise ValueError(f"unsupported report version {version!r}; expected 1")
    report = _exact_object(report, "report", _REPORT_FIELDS)
    summary = _exact_object(report.get("summary"), "report.summary", _SUMMARY_FIELDS)
    configuration = _exact_object(
        report.get("configuration"), "report.configuration", _CONFIGURATION_FIELDS
    )
    schema = _exact_object(report.get("schema"), "report.schema", _SCHEMA_FIELDS)
    missing = _exact_object(
        report.get("missing_records"), "report.missing_records", _MISSING_FIELDS
    )
    duplicates = _exact_object(
        report.get("duplicate_keys"), "report.duplicate_keys", _DUPLICATE_FIELDS
    )
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
    key_fields = _string_tuple(configuration.get("key_fields"), "report.configuration.key_fields")
    compared_fields = _string_tuple(
        configuration.get("compared_fields"), "report.configuration.compared_fields"
    )
    ignored_fields = _string_tuple(
        configuration.get("ignored_fields"), "report.configuration.ignored_fields"
    )
    _validate_configuration_scope(
        key_fields=key_fields,
        compared_fields=compared_fields,
        ignored_fields=ignored_fields,
        normalizers=normalizers,
        policies=policies,
        validation_rules=validation_rules,
    )
    _validate_recorded_descriptors(normalizers, policies, validation_rules)

    result = ReconciliationResult(
        key_fields=key_fields,
        compared_fields=compared_fields,
        ignored_fields=ignored_fields,
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
    _validate_issue_semantics(result)
    _validate_issue_consistency(result)
    _validate_summary(summary, result)
    _validate_status(report.get("status"), result)
    return result


def load_json_report(path: str | Path) -> ReconciliationResult:
    """Load a canonical single-result JSON report for read-only investigation."""

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
    except _DuplicateObjectKeyError as exc:
        raise InputError(f"{source}: invalid report JSON: {exc}") from exc
    except RecursionError as exc:
        raise InputError(f"{source}: report JSON exceeds the maximum nesting depth") from exc
    except json.JSONDecodeError as exc:
        raise InputError(
            f"{source}: invalid report JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise InputError(f"{source}: top-level report JSON value must be an object")
    try:
        return result_from_dict(payload)
    except ValueError as exc:
        raise InputError(f"{source}: invalid reconciliation report: {exc}") from exc


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

    payload = json.dumps(result_to_dict(result), indent=2, ensure_ascii=False) + "\n"
    return atomic_write_text(destination, payload)


def batch_result_to_dict(
    result: BatchResult,
    *,
    execution: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Convert a batch result into a stable aggregate report."""

    from reconforge.batch_report import batch_report_from_result, batch_report_to_dict

    return batch_report_to_dict(batch_report_from_result(result, execution=execution))


def write_json_batch_report(
    result: BatchResult,
    destination: str | Path,
    *,
    execution: Mapping[str, object] | None = None,
) -> Path:
    """Write a batch report atomically and return its resolved destination."""

    payload = (
        json.dumps(
            batch_result_to_dict(result, execution=execution),
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    return atomic_write_text(destination, payload)


def _object(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{location} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{location} keys must be strings")
    return dict(value)


def _exact_object(
    value: object,
    location: str,
    fields: frozenset[str],
) -> dict[str, object]:
    payload = _object(value, location)
    missing = sorted(fields - payload.keys())
    if missing:
        raise ValueError(f"{location} is missing required field(s): {missing!r}")
    unknown = sorted(payload.keys() - fields)
    if unknown:
        raise ValueError(f"{location} contains unknown field(s): {unknown!r}")
    return payload


class _DuplicateObjectKeyError(ValueError):
    """Internal signal for ambiguous JSON objects."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateObjectKeyError(f"duplicate object key {key!r}")
        payload[key] = value
    return payload


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


def _validate_configuration_scope(
    *,
    key_fields: tuple[str, ...],
    compared_fields: tuple[str, ...],
    ignored_fields: tuple[str, ...],
    normalizers: Mapping[str, str],
    policies: Mapping[str, str],
    validation_rules: tuple[tuple[str, tuple[str, ...]], ...],
) -> None:
    if not key_fields:
        raise ValueError("report.configuration.key_fields must contain at least one field")
    for fields, location in (
        (key_fields, "report.configuration.key_fields"),
        (compared_fields, "report.configuration.compared_fields"),
        (ignored_fields, "report.configuration.ignored_fields"),
    ):
        duplicates = sorted({field for field in fields if fields.count(field) > 1})
        if duplicates:
            raise ValueError(f"{location} must contain unique fields; repeated: {duplicates!r}")

    groups = {
        "key_fields": set(key_fields),
        "compared_fields": set(compared_fields),
        "ignored_fields": set(ignored_fields),
    }
    for left, right in (
        ("key_fields", "compared_fields"),
        ("key_fields", "ignored_fields"),
        ("compared_fields", "ignored_fields"),
    ):
        overlap = sorted(groups[left] & groups[right])
        if overlap:
            raise ValueError(
                f"report.configuration.{left} and {right} must not overlap: {overlap!r}"
            )

    usable_normalizers = groups["key_fields"] | groups["compared_fields"]
    unused_normalizers = sorted(set(normalizers) - usable_normalizers)
    if unused_normalizers:
        raise ValueError(
            "report.configuration.field_normalizers contains fields outside key_fields or "
            f"compared_fields: {unused_normalizers!r}"
        )
    unused_policies = sorted(set(policies) - groups["compared_fields"])
    if unused_policies:
        raise ValueError(
            "report.configuration.comparison_policies contains fields outside "
            f"compared_fields: {unused_policies!r}"
        )
    empty_rules = sorted(field for field, rules in validation_rules if not rules)
    if empty_rules:
        raise ValueError(
            f"report.configuration.validation_rules entries must not be empty: {empty_rules!r}"
        )
    for field, rules in validation_rules:
        duplicate_descriptors = find_duplicate_validation_descriptors(rules)
        if duplicate_descriptors:
            raise ValueError(
                "report.configuration.validation_rules"
                f"[{field!r}] must contain unique descriptors; "
                f"repeated: {list(duplicate_descriptors)!r}"
            )


def _validate_recorded_descriptors(
    normalizers: Mapping[str, str],
    policies: Mapping[str, str],
    validation_rules: tuple[tuple[str, tuple[str, ...]], ...],
) -> None:
    for field, descriptor in normalizers.items():
        location = f"report.configuration.field_normalizers[{field!r}]"
        try:
            parsed = FieldNormalizer.named(descriptor)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{location} is invalid: {exc}") from exc
        if parsed.kind.value != descriptor:
            raise ValueError(f"{location} must use canonical descriptor {parsed.kind.value!r}")

    for field, descriptor in policies.items():
        location = f"report.configuration.comparison_policies[{field!r}]"
        try:
            parsed = _parse_policy_descriptor(descriptor)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{location} is invalid: {exc}") from exc
        if parsed.descriptor != descriptor:
            raise ValueError(f"{location} must use canonical descriptor {parsed.descriptor!r}")

    for field, descriptors in validation_rules:
        for index, descriptor in enumerate(descriptors):
            location = f"report.configuration.validation_rules[{field!r}][{index}]"
            try:
                canonical = _parse_validation_descriptor(descriptor)
            except (TypeError, ValueError, json.JSONDecodeError, re.error) as exc:
                raise ValueError(f"{location} is invalid: {exc}") from exc
            if canonical != descriptor:
                raise ValueError(f"{location} must use canonical descriptor {canonical!r}")


def _parse_policy_descriptor(descriptor: str) -> ComparisonPolicy:
    prefix = "decimal:tolerance="
    if descriptor.startswith(prefix):
        return ComparisonPolicy.decimal(descriptor.removeprefix(prefix))
    return ComparisonPolicy.named(descriptor)


def _parse_validation_descriptor(descriptor: str) -> str:
    if descriptor == "required":
        return RequiredRule().descriptor
    if descriptor.startswith("allowed:"):
        values = json.loads(descriptor.removeprefix("allowed:"))
        if not isinstance(values, list):
            raise ValueError("allowed descriptor values must be a JSON array")
        return AllowedValuesRule(tuple(values)).descriptor
    if descriptor.startswith("numeric_range:"):
        match = _NUMERIC_RANGE_DESCRIPTOR.fullmatch(descriptor)
        if match is None:
            raise ValueError("invalid numeric-range descriptor")
        minimum_text = match.group("minimum")
        maximum_text = match.group("maximum")
        if minimum_text == "-inf" and match.group("left") != "(":
            raise ValueError("an unbounded minimum must be exclusive")
        if maximum_text == "+inf" and match.group("right") != ")":
            raise ValueError("an unbounded maximum must be exclusive")
        return NumericRangeRule(
            minimum=None if minimum_text == "-inf" else minimum_text,
            maximum=None if maximum_text == "+inf" else maximum_text,
            minimum_inclusive=match.group("left") == "[",
            maximum_inclusive=match.group("right") == "]",
        ).descriptor
    if descriptor.startswith("pattern:"):
        return TextPatternRule(descriptor.removeprefix("pattern:")).descriptor
    raise ValueError(f"unknown validation rule descriptor {descriptor!r}")


def _validate_issue_semantics(result: ReconciliationResult) -> None:
    key_arity = len(result.key_fields)
    compared = set(result.compared_fields)
    declared_rules = dict(result.validation_rules)
    for index, difference in enumerate(result.differences):
        _validate_key_arity(difference.key, key_arity, f"report.differences[{index}].key")
        if difference.field not in compared:
            raise ValueError(
                f"report.differences[{index}].field {difference.field!r} is not a compared field"
            )
    for section, records in (
        ("report.missing_records.from_left", result.missing_from_left),
        ("report.missing_records.from_right", result.missing_from_right),
    ):
        for index, record in enumerate(records):
            _validate_key_arity(record.key, key_arity, f"{section}[{index}].key")
    for section, duplicates in (
        ("report.duplicate_keys.left", result.duplicate_left),
        ("report.duplicate_keys.right", result.duplicate_right),
    ):
        for index, duplicate in enumerate(duplicates):
            location = f"{section}[{index}]"
            _validate_key_arity(duplicate.key, key_arity, f"{location}.key")
            if len(duplicate.row_numbers) < 2:
                raise ValueError(f"{location}.row_numbers must contain at least two rows")
            if len(duplicate.row_numbers) != len(set(duplicate.row_numbers)):
                raise ValueError(f"{location}.row_numbers must contain unique rows")

    for index, violation in enumerate(result.validation_violations):
        location = f"report.validation_violations[{index}]"
        if violation.side not in {"left", "right"}:
            raise ValueError(f"{location}.side must be 'left' or 'right'")
        rules = declared_rules.get(violation.field)
        if rules is None:
            raise ValueError(
                f"{location}.field {violation.field!r} has no configured validation rules"
            )
        if violation.rule not in rules:
            raise ValueError(
                f"{location}.rule {violation.rule!r} is not configured for field "
                f"{violation.field!r}"
            )

    configured_fields = {
        *result.key_fields,
        *result.compared_fields,
        *result.ignored_fields,
        *(field for field, _ in result.field_normalizers),
        *(field for field, _ in result.comparison_policies),
        *(field for field, _ in result.validation_rules),
    }
    for side, fields in (
        ("left", result.fields_only_left),
        ("right", result.fields_only_right),
    ):
        overlap = sorted(set(fields) & configured_fields)
        if overlap:
            raise ValueError(
                f"report.schema.fields_only_{side} contains configured fields: {overlap!r}"
            )


def _validate_key_arity(key: tuple[str | None, ...], expected: int, location: str) -> None:
    if len(key) != expected:
        raise ValueError(f"{location} must contain {expected} component(s); received {len(key)}")


def _validate_issue_consistency(result: ReconciliationResult) -> None:
    _require_unique_issue_values(
        result.validation_violations,
        "report.validation_violations",
    )
    _require_unique_issue_values(
        tuple((item.key, item.field) for item in result.differences),
        "report.differences",
    )
    _require_unique_issue_values(
        tuple(item.key for item in result.missing_from_left),
        "report.missing_records.from_left",
    )
    _require_unique_issue_values(
        tuple(item.key for item in result.missing_from_right),
        "report.missing_records.from_right",
    )
    _require_unique_issue_values(
        tuple(item.key for item in result.duplicate_left),
        "report.duplicate_keys.left",
    )
    _require_unique_issue_values(
        tuple(item.key for item in result.duplicate_right),
        "report.duplicate_keys.right",
    )
    _require_unique_issue_values(result.fields_only_left, "report.schema.fields_only_left")
    _require_unique_issue_values(result.fields_only_right, "report.schema.fields_only_right")

    schema_overlap = sorted(set(result.fields_only_left) & set(result.fields_only_right))
    if schema_overlap:
        raise ValueError(
            f"report.schema fields cannot be exclusive to both sides: {schema_overlap!r}"
        )

    classifications: dict[tuple[str | None, ...], set[str]] = {}
    for label, keys in (
        ("field_difference", {item.key for item in result.differences}),
        ("missing_from_left", {item.key for item in result.missing_from_left}),
        ("missing_from_right", {item.key for item in result.missing_from_right}),
        ("duplicate_key", {item.key for item in (*result.duplicate_left, *result.duplicate_right)}),
    ):
        for key in keys:
            classifications.setdefault(key, set()).add(label)
    for key, labels in classifications.items():
        if len(labels) > 1:
            raise ValueError(
                f"report contains contradictory classifications for key {list(key)!r}: "
                f"{sorted(labels)!r}"
            )


def _require_unique_issue_values(values: tuple[object, ...], location: str) -> None:
    seen: dict[object, int] = {}
    for index, value in enumerate(values):
        previous = seen.get(value)
        if previous is not None:
            raise ValueError(
                f"{location}[{index}] duplicates the identity at {location}[{previous}]"
            )
        seen[value] = index


def _validate_summary(summary: Mapping[str, object], result: ReconciliationResult) -> None:
    recorded = {
        field: _integer(summary.get(field), f"report.summary.{field}")
        for field in sorted(_SUMMARY_FIELDS)
    }
    expected = result_to_dict(result)["summary"]
    for field in sorted(_SUMMARY_FIELDS - {"matched_records"}):
        if recorded[field] != expected[field]:
            raise ValueError(
                f"report.summary.{field} is {recorded[field]}; expected {expected[field]} "
                "from report content"
            )


def _validate_status(value: object, result: ReconciliationResult) -> None:
    if not isinstance(value, str):
        raise ValueError("report.status must be a string")
    expected = _result_status(result)
    if value != expected:
        raise ValueError(f"report.status is {value!r}; expected {expected!r} from report content")


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
    item = _exact_object(value, location, _DIFFERENCE_FIELDS)
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
    item = _exact_object(value, location, _VIOLATION_FIELDS)
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
    item = _exact_object(value, location, frozenset({"key", row_key}))
    return MissingRecord(
        key=_record_key(item.get("key"), f"{location}.key"),
        row_number=_positive_integer(item.get(row_key), f"{location}.{row_key}"),
    )


def _duplicate_key(value: object, index: int, side: str) -> DuplicateKey:
    location = f"report.duplicate_keys.{side}[{index}]"
    item = _exact_object(value, location, frozenset({"key", "row_numbers"}))
    rows = _array(item.get("row_numbers"), f"{location}.row_numbers")
    return DuplicateKey(
        key=_record_key(item.get("key"), f"{location}.key"),
        row_numbers=tuple(
            _positive_integer(row, f"{location}.row_numbers[{row_index}]")
            for row_index, row in enumerate(rows)
        ),
    )
