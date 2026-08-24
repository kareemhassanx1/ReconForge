"""Immutable data structures used by the reconciliation engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

CellValue = str | None
RecordKey = tuple[CellValue, ...]


@dataclass(frozen=True, slots=True)
class Record:
    """One normalized source row with its original location."""

    source: str
    row_number: int
    values: Mapping[str, CellValue]

    def __post_init__(self) -> None:
        if self.row_number < 1:
            raise ValueError("record row numbers must be positive")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    def value(self, field: str) -> CellValue:
        """Return a field value, raising a useful error for an unknown field."""

        try:
            return self.values[field]
        except KeyError as exc:
            raise KeyError(f"field {field!r} does not exist in {self.source}") from exc


@dataclass(frozen=True, slots=True)
class Dataset:
    """A normalized table loaded from one source."""

    source: str
    headers: tuple[str, ...]
    records: tuple[Record, ...]

    def __post_init__(self) -> None:
        if not self.headers:
            raise ValueError("a dataset requires at least one header")
        if len(self.headers) != len(set(self.headers)):
            raise ValueError("dataset headers must be unique")


@dataclass(frozen=True, slots=True)
class FieldDifference:
    """An observable value mismatch between a matched pair of records."""

    key: RecordKey
    field: str
    left_value: CellValue
    right_value: CellValue
    left_row: int
    right_row: int


@dataclass(frozen=True, slots=True)
class MissingRecord:
    """A unique record whose key is absent from the other dataset."""

    key: RecordKey
    row_number: int


@dataclass(frozen=True, slots=True)
class DuplicateKey:
    """All source rows that share a key which should be unique."""

    key: RecordKey
    row_numbers: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Complete deterministic outcome of one dataset comparison."""

    key_fields: tuple[str, ...]
    compared_fields: tuple[str, ...]
    ignored_fields: tuple[str, ...]
    case_sensitive_keys: bool
    field_normalizers: tuple[tuple[str, str], ...]
    comparison_policies: tuple[tuple[str, str], ...]
    matched_count: int
    differences: tuple[FieldDifference, ...]
    missing_from_left: tuple[MissingRecord, ...]
    missing_from_right: tuple[MissingRecord, ...]
    duplicate_left: tuple[DuplicateKey, ...]
    duplicate_right: tuple[DuplicateKey, ...]
    fields_only_left: tuple[str, ...]
    fields_only_right: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        """Whether the inputs agree on schema, keys, and compared values."""

        return not any(
            (
                self.differences,
                self.missing_from_left,
                self.missing_from_right,
                self.duplicate_left,
                self.duplicate_right,
                self.fields_only_left,
                self.fields_only_right,
            )
        )

    @property
    def difference_count(self) -> int:
        """Count every independently actionable discrepancy."""

        return sum(
            (
                len(self.differences),
                len(self.missing_from_left),
                len(self.missing_from_right),
                len(self.duplicate_left),
                len(self.duplicate_right),
                len(self.fields_only_left),
                len(self.fields_only_right),
            )
        )
