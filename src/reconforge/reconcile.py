"""Deterministic exact-value reconciliation for normalized datasets."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class ReconciliationConfig:
    """Public settings for an exact reconciliation run."""

    key_fields: tuple[str, ...]
    compare_fields: tuple[str, ...] | None = None
    ignore_fields: tuple[str, ...] = ()
    case_sensitive_keys: bool = True

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


def reconcile(
    left: Dataset,
    right: Dataset,
    config: ReconciliationConfig,
) -> ReconciliationResult:
    """Compare two datasets and return every observable discrepancy."""

    _validate_required_fields(left, right, config)
    compared_fields = _select_compared_fields(left, right, config)
    left_index = _build_index(left.records, config)
    right_index = _build_index(right.records, config)

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
        differences.extend(_compare_record_pair(key, left_record, right_record, compared_fields))

    left_headers = set(left.headers)
    right_headers = set(right.headers)
    ignored = set(config.ignore_fields)

    return ReconciliationResult(
        key_fields=config.key_fields,
        compared_fields=compared_fields,
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


def _build_index(
    records: Iterable[Record],
    config: ReconciliationConfig,
) -> dict[RecordKey, list[Record]]:
    index: dict[RecordKey, list[Record]] = defaultdict(list)
    for record in records:
        key = tuple(
            _normalize_key_component(record.value(field), config.case_sensitive_keys)
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
) -> list[FieldDifference]:
    differences = []
    for field in fields:
        left_value = left.value(field)
        right_value = right.value(field)
        if left_value != right_value:
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
