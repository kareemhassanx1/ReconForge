"""Deterministic policy-driven reconciliation for normalized datasets."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from types import MappingProxyType

from reconforge.errors import ConfigurationError
from reconforge.model import (
    CellValue,
    Dataset,
    DuplicateKey,
    FieldDifference,
    MissingRecord,
    ReconciliationResult,
    Record,
    RecordKey,
)
from reconforge.normalization import FieldNormalizer, normalize_dataset_fields
from reconforge.policies import (
    ComparisonPolicy,
    PreparedValue,
    prepare_dataset_fields,
)


@dataclass(frozen=True, slots=True)
class ReconciliationConfig:
    """Public settings for a reconciliation run."""

    key_fields: tuple[str, ...]
    compare_fields: tuple[str, ...] | None = None
    ignore_fields: tuple[str, ...] = ()
    case_sensitive_keys: bool = True
    field_normalizers: Mapping[str, FieldNormalizer] = dataclass_field(default_factory=dict)
    comparison_policies: Mapping[str, ComparisonPolicy] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.key_fields:
            raise ValueError("at least one key field is required")
        _require_unique(self.key_fields, "key fields")
        _require_unique(self.ignore_fields, "ignored fields")
        if self.compare_fields is not None:
            _require_unique(self.compare_fields, "comparison fields")

        overlap = set(self.key_fields) & set(self.ignore_fields)
        if overlap:
            raise ValueError(f"key fields cannot be ignored: {sorted(overlap)!r}")

        normalized = dict(self.field_normalizers)
        if any(not name for name in normalized):
            raise ValueError("normalized field names cannot be empty")
        invalid = [
            name for name, value in normalized.items() if not isinstance(value, FieldNormalizer)
        ]
        if invalid:
            raise TypeError(
                f"field normalizers must be FieldNormalizer instances: {sorted(invalid)!r}"
            )
        ignored_normalizers = set(normalized) & set(self.ignore_fields)
        if ignored_normalizers:
            raise ValueError(
                f"ignored fields cannot have normalizers: {sorted(ignored_normalizers)!r}"
            )
        object.__setattr__(self, "field_normalizers", MappingProxyType(normalized))

        policies = dict(self.comparison_policies)
        if any(not name for name in policies):
            raise ValueError("comparison policy field names cannot be empty")
        invalid_policies = [
            name for name, value in policies.items() if not isinstance(value, ComparisonPolicy)
        ]
        if invalid_policies:
            raise TypeError(
                "comparison policies must be ComparisonPolicy instances: "
                f"{sorted(invalid_policies)!r}"
            )
        ignored_policies = set(policies) & set(self.ignore_fields)
        if ignored_policies:
            raise ValueError(
                f"ignored fields cannot have comparison policies: {sorted(ignored_policies)!r}"
            )
        object.__setattr__(self, "comparison_policies", MappingProxyType(policies))


def reconcile(
    left: Dataset,
    right: Dataset,
    config: ReconciliationConfig,
) -> ReconciliationResult:
    """Compare two datasets and return every observable discrepancy."""

    _validate_required_fields(left, right, config)
    compared_fields = _select_compared_fields(left, right, config)
    _validate_normalizer_scope(config, compared_fields)
    _validate_policy_scope(config, compared_fields)
    normalizers = dict(config.field_normalizers)
    policies = dict(config.comparison_policies)
    left_normalized = normalize_dataset_fields(left, normalizers)
    right_normalized = normalize_dataset_fields(right, normalizers)
    left_prepared = prepare_dataset_fields(left, policies, left_normalized)
    right_prepared = prepare_dataset_fields(right, policies, right_normalized)
    left_index = _build_index(left.records, config, left_normalized)
    right_index = _build_index(right.records, config, right_normalized)

    duplicate_left = _collect_duplicates(left_index)
    duplicate_right = _collect_duplicates(right_index)
    duplicate_keys = {
        *(item.key for item in duplicate_left),
        *(item.key for item in duplicate_right),
    }

    left_keys = set(left_index)
    right_keys = set(right_index)
    unique_left_only = sorted(left_keys - right_keys, key=_sortable_key)
    unique_right_only = sorted(right_keys - left_keys, key=_sortable_key)

    missing_from_right = tuple(
        MissingRecord(key=key, row_number=left_index[key][0].row_number)
        for key in unique_left_only
        if key not in duplicate_keys
    )
    missing_from_left = tuple(
        MissingRecord(key=key, row_number=right_index[key][0].row_number)
        for key in unique_right_only
        if key not in duplicate_keys
    )

    differences: list[FieldDifference] = []
    matched_count = 0
    for key in sorted(left_keys & right_keys, key=_sortable_key):
        if key in duplicate_keys:
            continue
        left_record = left_index[key][0]
        right_record = right_index[key][0]
        matched_count += 1
        differences.extend(
            _compare_record_pair(
                key,
                left_record,
                right_record,
                compared_fields,
                left_normalized,
                right_normalized,
                policies,
                left_prepared,
                right_prepared,
            )
        )

    left_headers = set(left.headers)
    right_headers = set(right.headers)
    ignored = set(config.ignore_fields)

    return ReconciliationResult(
        key_fields=config.key_fields,
        compared_fields=compared_fields,
        field_normalizers=tuple(
            (name, normalizer.kind.value) for name, normalizer in sorted(normalizers.items())
        ),
        comparison_policies=tuple(
            (name, policy.descriptor) for name, policy in sorted(policies.items())
        ),
        matched_count=matched_count,
        differences=tuple(differences),
        missing_from_left=missing_from_left,
        missing_from_right=missing_from_right,
        duplicate_left=duplicate_left,
        duplicate_right=duplicate_right,
        fields_only_left=tuple(sorted((left_headers - right_headers) - ignored)),
        fields_only_right=tuple(sorted((right_headers - left_headers) - ignored)),
    )


def _require_unique(values: tuple[str, ...], label: str) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"{label} must be unique; repeated: {duplicates!r}")


def _validate_required_fields(
    left: Dataset,
    right: Dataset,
    config: ReconciliationConfig,
) -> None:
    for field in config.key_fields:
        _require_field_on_both_sides(field, left, right, "key")

    for field in config.compare_fields or ():
        _require_field_on_both_sides(field, left, right, "comparison")

    for field in config.field_normalizers:
        _require_field_on_both_sides(field, left, right, "normalized")

    for field in config.comparison_policies:
        _require_field_on_both_sides(field, left, right, "comparison policy")


def _require_field_on_both_sides(
    field: str,
    left: Dataset,
    right: Dataset,
    purpose: str,
) -> None:
    missing = []
    if field not in left.headers:
        missing.append(f"left input {left.source}")
    if field not in right.headers:
        missing.append(f"right input {right.source}")
    if missing:
        raise ConfigurationError(
            f"{purpose} field {field!r} is missing from {' and '.join(missing)}"
        )


def _select_compared_fields(
    left: Dataset,
    right: Dataset,
    config: ReconciliationConfig,
) -> tuple[str, ...]:
    if config.compare_fields is not None:
        selected = config.compare_fields
    else:
        right_headers = set(right.headers)
        selected = tuple(field for field in left.headers if field in right_headers)

    excluded = set(config.key_fields) | set(config.ignore_fields)
    return tuple(field for field in selected if field not in excluded)


def _validate_normalizer_scope(
    config: ReconciliationConfig,
    compared_fields: tuple[str, ...],
) -> None:
    usable = set(config.key_fields) | set(compared_fields)
    unused = set(config.field_normalizers) - usable
    if unused:
        raise ConfigurationError(
            f"normalizers apply only to key or comparison fields; unused: {sorted(unused)!r}"
        )


def _validate_policy_scope(
    config: ReconciliationConfig,
    compared_fields: tuple[str, ...],
) -> None:
    unused = set(config.comparison_policies) - set(compared_fields)
    if unused:
        raise ConfigurationError(
            f"comparison policies apply only to compared fields; unused: {sorted(unused)!r}"
        )


def _build_index(
    records: Iterable[Record],
    config: ReconciliationConfig,
    normalized: dict[int, dict[str, CellValue]],
) -> dict[RecordKey, list[Record]]:
    index: dict[RecordKey, list[Record]] = defaultdict(list)
    for record in records:
        key = tuple(
            _normalize_key_component(
                normalized[record.row_number].get(field, record.value(field)),
                config.case_sensitive_keys,
            )
            for field in config.key_fields
        )
        index[key].append(record)
    return dict(index)


def _normalize_key_component(value: CellValue, case_sensitive: bool) -> CellValue:
    if value is None or case_sensitive:
        return value
    return value.casefold()


def _collect_duplicates(index: dict[RecordKey, list[Record]]) -> tuple[DuplicateKey, ...]:
    return tuple(
        DuplicateKey(
            key=key,
            row_numbers=tuple(record.row_number for record in index[key]),
        )
        for key in sorted(index, key=_sortable_key)
        if len(index[key]) > 1
    )


def _compare_record_pair(
    key: RecordKey,
    left: Record,
    right: Record,
    fields: tuple[str, ...],
    left_normalized: dict[int, dict[str, CellValue]],
    right_normalized: dict[int, dict[str, CellValue]],
    policies: Mapping[str, ComparisonPolicy],
    left_prepared: Mapping[int, Mapping[str, PreparedValue]],
    right_prepared: Mapping[int, Mapping[str, PreparedValue]],
) -> list[FieldDifference]:
    differences = []
    for field in fields:
        left_value = left.value(field)
        right_value = right.value(field)
        comparable_left = left_normalized[left.row_number].get(field, left_value)
        comparable_right = right_normalized[right.row_number].get(field, right_value)
        policy = policies.get(field)
        values_match = (
            comparable_left == comparable_right
            if policy is None
            else policy.values_match(
                left_prepared[left.row_number][field],
                right_prepared[right.row_number][field],
            )
        )
        if not values_match:
            differences.append(
                FieldDifference(
                    key=key,
                    field=field,
                    left_value=left_value,
                    right_value=right_value,
                    left_row=left.row_number,
                    right_row=right.row_number,
                )
            )
    return differences


def _sortable_key(key: RecordKey) -> tuple[str, ...]:
    return tuple("" if value is None else value for value in key)
