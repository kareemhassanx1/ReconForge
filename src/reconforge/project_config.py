"""Versioned project configuration loading for repeatable reconciliations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from json import JSONDecodeError
from pathlib import Path
from types import MappingProxyType

from reconforge.errors import ConfigurationError
from reconforge.exporters import ReportFormat
from reconforge.json_safety import JsonNestingTooDeepError, validate_json_nesting
from reconforge.normalization import FieldNormalizer
from reconforge.policies import ComparisonPolicy
from reconforge.readers import DatasetFormat
from reconforge.validation import (
    AllowedValuesRule,
    NumericRangeRule,
    RequiredRule,
    TextPatternRule,
    ValidationRule,
    find_duplicate_validation_descriptors,
)

PROJECT_CONFIG_VERSION = 1

_TOP_LEVEL_KEYS = frozenset({"version", "inputs", "reconciliation", "report"})
_INPUT_KEYS = frozenset({"format", "left_format", "right_format", "delimiter"})
_REPORT_KEYS = frozenset({"format"})
_RECONCILIATION_KEYS = frozenset(
    {
        "key_fields",
        "compare_fields",
        "ignore_fields",
        "case_sensitive_keys",
        "field_normalizers",
        "comparison_policies",
        "validation_rules",
    }
)


class ProjectConfigError(ConfigurationError):
    """Raised when a project configuration file is invalid or unsupported."""


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """Validated settings loaded from a versioned project configuration."""

    version: int = PROJECT_CONFIG_VERSION
    format: DatasetFormat | None = None
    left_format: DatasetFormat | None = None
    right_format: DatasetFormat | None = None
    delimiter: str | None = None
    report_format: ReportFormat | None = None
    key_fields: tuple[str, ...] | None = None
    compare_fields: tuple[str, ...] | None = None
    ignore_fields: tuple[str, ...] = ()
    case_sensitive_keys: bool = True
    field_normalizers: Mapping[str, FieldNormalizer] = field(default_factory=dict)
    comparison_policies: Mapping[str, ComparisonPolicy] = field(default_factory=dict)
    validation_rules: Mapping[str, tuple[ValidationRule, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.version != PROJECT_CONFIG_VERSION:
            raise ValueError(
                f"unsupported project configuration version {self.version!r}; "
                f"expected {PROJECT_CONFIG_VERSION}"
            )
        if isinstance(self.report_format, str) and not isinstance(self.report_format, ReportFormat):
            object.__setattr__(self, "report_format", ReportFormat.named(self.report_format))
        elif self.report_format is not None and not isinstance(self.report_format, ReportFormat):
            raise TypeError("report format must be a ReportFormat, string, or null")
        object.__setattr__(
            self,
            "field_normalizers",
            MappingProxyType(dict(self.field_normalizers)),
        )
        object.__setattr__(
            self,
            "comparison_policies",
            MappingProxyType(dict(self.comparison_policies)),
        )
        object.__setattr__(
            self,
            "validation_rules",
            MappingProxyType({name: tuple(rules) for name, rules in self.validation_rules.items()}),
        )


def load_project_config(path: str | Path) -> ProjectConfig:
    """Load and strictly validate a ReconForge JSON project configuration."""

    source = Path(path)
    payload = _read_json_object(source)
    _reject_unknown_keys(payload, _TOP_LEVEL_KEYS, source, "top level")

    if "version" not in payload:
        raise ProjectConfigError(f"{source}: missing required top-level key 'version'")
    version = payload["version"]
    if type(version) is not int:
        raise ProjectConfigError(f"{source}: 'version' must be an integer")
    if version != PROJECT_CONFIG_VERSION:
        raise ProjectConfigError(
            f"{source}: unsupported project configuration version {version}; "
            f"expected {PROJECT_CONFIG_VERSION}"
        )

    inputs = _optional_object(payload, "inputs", source)
    reconciliation = _optional_object(payload, "reconciliation", source)
    report = _optional_object(payload, "report", source)
    _reject_unknown_keys(inputs, _INPUT_KEYS, source, "inputs")
    _reject_unknown_keys(report, _REPORT_KEYS, source, "report")
    _reject_unknown_keys(
        reconciliation,
        _RECONCILIATION_KEYS,
        source,
        "reconciliation",
    )

    return ProjectConfig(
        version=version,
        format=_optional_format(inputs, "format", source),
        left_format=_optional_format(inputs, "left_format", source),
        right_format=_optional_format(inputs, "right_format", source),
        delimiter=_optional_delimiter(inputs, source),
        report_format=_optional_report_format(report, source),
        key_fields=_optional_field_list(
            reconciliation,
            "key_fields",
            source,
            allow_empty=False,
        ),
        compare_fields=_optional_field_list(
            reconciliation,
            "compare_fields",
            source,
            allow_empty=True,
        ),
        ignore_fields=_optional_field_list(
            reconciliation,
            "ignore_fields",
            source,
            allow_empty=True,
        )
        or (),
        case_sensitive_keys=_optional_boolean(
            reconciliation,
            "case_sensitive_keys",
            source,
            default=True,
        ),
        field_normalizers=_normalizer_map(reconciliation, source),
        comparison_policies=_policy_map(reconciliation, source),
        validation_rules=_validation_rule_map(reconciliation, source),
    )


class _DuplicateObjectKey(ValueError):
    pass


def _read_json_object(source: Path) -> dict[str, object]:
    try:
        text = source.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ProjectConfigError(f"project configuration does not exist: {source}") from exc
    except UnicodeError as exc:
        raise ProjectConfigError(f"project configuration is not valid UTF-8: {source}") from exc
    except OSError as exc:
        raise ProjectConfigError(f"cannot read project configuration {source}: {exc}") from exc

    try:
        payload = json.loads(text, object_pairs_hook=_unique_object)
        validate_json_nesting(payload)
    except _DuplicateObjectKey as exc:
        raise ProjectConfigError(f"{source}: {exc}") from exc
    except (JsonNestingTooDeepError, RecursionError) as exc:
        raise ProjectConfigError(
            f"{source}: project configuration JSON exceeds the maximum nesting depth"
        ) from exc
    except JSONDecodeError as exc:
        raise ProjectConfigError(
            f"{source}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(payload, dict):
        raise ProjectConfigError(f"{source}: top-level JSON value must be an object")
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateObjectKey(f"duplicate key {key!r}")
        result[key] = value
    return result


def _optional_object(
    payload: Mapping[str, object],
    key: str,
    source: Path,
) -> dict[str, object]:
    value = payload.get(key, {})
    if not isinstance(value, dict):
        raise ProjectConfigError(f"{source}: {key!r} must be an object")
    return value


def _reject_unknown_keys(
    payload: Mapping[str, object],
    allowed: frozenset[str],
    source: Path,
    location: str,
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ProjectConfigError(
            f"{source}: unknown key(s) in {location}: {', '.join(repr(key) for key in unknown)}"
        )


def _optional_format(
    inputs: Mapping[str, object],
    key: str,
    source: Path,
) -> DatasetFormat | None:
    if key not in inputs:
        return None
    value = inputs[key]
    if not isinstance(value, str):
        raise ProjectConfigError(f"{source}: inputs.{key} must be a string")
    try:
        return DatasetFormat.named(value)
    except ValueError as exc:
        raise ProjectConfigError(f"{source}: invalid inputs.{key}: {exc}") from exc


def _optional_delimiter(inputs: Mapping[str, object], source: Path) -> str | None:
    if "delimiter" not in inputs:
        return None
    value = inputs["delimiter"]
    if not isinstance(value, str) or len(value) != 1:
        raise ProjectConfigError(f"{source}: inputs.delimiter must be exactly one character")
    return value


def _optional_report_format(report: Mapping[str, object], source: Path) -> ReportFormat | None:
    if "format" not in report:
        return None
    value = report["format"]
    if not isinstance(value, str):
        raise ProjectConfigError(f"{source}: report.format must be a string")
    try:
        return ReportFormat.named(value)
    except ValueError as exc:
        raise ProjectConfigError(f"{source}: invalid report.format: {exc}") from exc


def _optional_field_list(
    reconciliation: Mapping[str, object],
    key: str,
    source: Path,
    *,
    allow_empty: bool,
) -> tuple[str, ...] | None:
    if key not in reconciliation:
        return None
    value = reconciliation[key]
    if not isinstance(value, list):
        raise ProjectConfigError(f"{source}: reconciliation.{key} must be an array")
    if not allow_empty and not value:
        raise ProjectConfigError(f"{source}: reconciliation.{key} must contain at least one field")

    fields: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ProjectConfigError(
                f"{source}: reconciliation.{key}[{index}] must be a non-empty string"
            )
        fields.append(item.strip())

    duplicates = sorted({name for name in fields if fields.count(name) > 1})
    if duplicates:
        raise ProjectConfigError(
            f"{source}: reconciliation.{key} contains duplicate fields: {duplicates!r}"
        )
    return tuple(fields)


def _optional_boolean(
    reconciliation: Mapping[str, object],
    key: str,
    source: Path,
    *,
    default: bool,
) -> bool:
    if key not in reconciliation:
        return default
    value = reconciliation[key]
    if type(value) is not bool:
        raise ProjectConfigError(f"{source}: reconciliation.{key} must be a boolean")
    return value


def _normalizer_map(
    reconciliation: Mapping[str, object],
    source: Path,
) -> dict[str, FieldNormalizer]:
    assignments = _string_map(reconciliation, "field_normalizers", source)
    normalizers: dict[str, FieldNormalizer] = {}
    for field_name, type_name in assignments.items():
        try:
            normalizers[field_name] = FieldNormalizer.named(type_name)
        except ValueError as exc:
            raise ProjectConfigError(
                f"{source}: invalid reconciliation.field_normalizers[{field_name!r}]: {exc}"
            ) from exc
    return normalizers


def _policy_map(
    reconciliation: Mapping[str, object],
    source: Path,
) -> dict[str, ComparisonPolicy]:
    assignments = _string_map(reconciliation, "comparison_policies", source)
    policies: dict[str, ComparisonPolicy] = {}
    for field_name, specification in assignments.items():
        try:
            policies[field_name] = ComparisonPolicy.named(specification)
        except ValueError as exc:
            raise ProjectConfigError(
                f"{source}: invalid reconciliation.comparison_policies[{field_name!r}]: {exc}"
            ) from exc
    return policies


def _string_map(
    reconciliation: Mapping[str, object],
    key: str,
    source: Path,
) -> dict[str, str]:
    value = reconciliation.get(key, {})
    if not isinstance(value, dict):
        raise ProjectConfigError(f"{source}: reconciliation.{key} must be an object")

    assignments: dict[str, str] = {}
    for raw_field, raw_specification in value.items():
        field_name = raw_field.strip()
        if not field_name:
            raise ProjectConfigError(
                f"{source}: reconciliation.{key} field names must be non-empty"
            )
        if field_name in assignments:
            raise ProjectConfigError(
                f"{source}: reconciliation.{key} contains duplicate field {field_name!r}"
            )
        if not isinstance(raw_specification, str) or not raw_specification.strip():
            raise ProjectConfigError(
                f"{source}: reconciliation.{key}[{field_name!r}] must be a non-empty string"
            )
        assignments[field_name] = raw_specification.strip()
    return assignments


def _validation_rule_map(
    reconciliation: Mapping[str, object],
    source: Path,
) -> dict[str, tuple[ValidationRule, ...]]:
    value = reconciliation.get("validation_rules", {})
    if not isinstance(value, dict):
        raise ProjectConfigError(f"{source}: reconciliation.validation_rules must be an object")

    configured: dict[str, tuple[ValidationRule, ...]] = {}
    for raw_field, raw_rules in value.items():
        field_name = raw_field.strip()
        if not field_name:
            raise ProjectConfigError(
                f"{source}: reconciliation.validation_rules field names must be non-empty"
            )
        if field_name in configured:
            raise ProjectConfigError(
                f"{source}: reconciliation.validation_rules contains duplicate field {field_name!r}"
            )
        if not isinstance(raw_rules, list) or not raw_rules:
            raise ProjectConfigError(
                f"{source}: reconciliation.validation_rules[{field_name!r}] "
                "must be a non-empty array"
            )

        rules = tuple(
            _parse_validation_rule(rule, source, field_name, index)
            for index, rule in enumerate(raw_rules)
        )
        duplicate_descriptors = find_duplicate_validation_descriptors(
            rule.descriptor for rule in rules
        )
        if duplicate_descriptors:
            raise ProjectConfigError(
                f"{source}: reconciliation.validation_rules[{field_name!r}] must have "
                "unique rule descriptors; "
                f"repeated: {list(duplicate_descriptors)!r}"
            )
        configured[field_name] = rules
    return configured


def _parse_validation_rule(
    raw_rule: object,
    source: Path,
    field_name: str,
    index: int,
) -> ValidationRule:
    location = f"reconciliation.validation_rules[{field_name!r}][{index}]"
    if not isinstance(raw_rule, dict):
        raise ProjectConfigError(f"{source}: {location} must be an object")
    type_name = raw_rule.get("type")
    if not isinstance(type_name, str) or not type_name.strip():
        raise ProjectConfigError(f"{source}: {location}.type must be a non-empty string")
    normalized_type = type_name.strip().lower()

    try:
        if normalized_type == "required":
            _reject_unknown_keys(raw_rule, frozenset({"type"}), source, location)
            return RequiredRule()
        if normalized_type == "allowed":
            _reject_unknown_keys(
                raw_rule,
                frozenset({"type", "values"}),
                source,
                location,
            )
            values = raw_rule.get("values")
            if not isinstance(values, list):
                raise ProjectConfigError(f"{source}: {location}.values must be an array")
            return AllowedValuesRule(tuple(values))
        if normalized_type == "numeric_range":
            _reject_unknown_keys(
                raw_rule,
                frozenset(
                    {
                        "type",
                        "minimum",
                        "maximum",
                        "minimum_inclusive",
                        "maximum_inclusive",
                    }
                ),
                source,
                location,
            )
            return NumericRangeRule(
                minimum=raw_rule.get("minimum"),
                maximum=raw_rule.get("maximum"),
                minimum_inclusive=raw_rule.get("minimum_inclusive", True),
                maximum_inclusive=raw_rule.get("maximum_inclusive", True),
            )
        if normalized_type == "pattern":
            _reject_unknown_keys(
                raw_rule,
                frozenset({"type", "pattern"}),
                source,
                location,
            )
            pattern = raw_rule.get("pattern")
            if not isinstance(pattern, str):
                raise ProjectConfigError(f"{source}: {location}.pattern must be a string")
            return TextPatternRule(pattern)
    except (TypeError, ValueError) as exc:
        raise ProjectConfigError(f"{source}: invalid {location}: {exc}") from exc

    choices = "required, allowed, numeric_range, pattern"
    raise ProjectConfigError(
        f"{source}: unknown validation rule type {type_name!r} at {location}; "
        f"expected one of: {choices}"
    )
