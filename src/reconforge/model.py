"""Immutable data structures used by the reconciliation engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

CellValue = str | None
RecordKey = tuple[CellValue, ...]


def record_key_sort_key(key: RecordKey) -> tuple[tuple[bool, str], ...]:
    """Order null before strings without conflating it with an empty string."""

    return tuple((value is not None, value if value is not None else "") for value in key)


@dataclass(frozen=True, slots=True)
class Record:
    """One normalized source row with its original location."""

    source: str
    row_number: int
    values: Mapping[str, CellValue]

    def __post_init__(self) -> None:
        if type(self.row_number) is not int or self.row_number < 1:
            raise ValueError("record row numbers must be positive integers")
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
        object.__setattr__(self, "headers", tuple(self.headers))
        object.__setattr__(self, "records", tuple(self.records))
        if not self.headers:
            raise ValueError("a dataset requires at least one header")
        if len(self.headers) != len(set(self.headers)):
            raise ValueError("dataset headers must be unique")
        if any(not isinstance(record, Record) for record in self.records):
            raise TypeError("dataset records must be Record instances")
        row_numbers = [record.row_number for record in self.records]
        if len(row_numbers) != len(set(row_numbers)):
            raise ValueError("dataset row numbers must be unique")


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
class ValidationViolation:
    """A data-quality rule violation, separate from reconciliation differences."""

    side: str
    source: str
    row_number: int
    field: str
    value: CellValue
    rule: str
    message: str


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Complete deterministic outcome of one dataset comparison."""

    key_fields: tuple[str, ...]
    compared_fields: tuple[str, ...]
    ignored_fields: tuple[str, ...]
    case_sensitive_keys: bool
    field_normalizers: tuple[tuple[str, str], ...]
    comparison_policies: tuple[tuple[str, str], ...]
    validation_rules: tuple[tuple[str, tuple[str, ...]], ...]
    validation_violations: tuple[ValidationViolation, ...]
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
        """Whether inputs agree and satisfy every configured validation rule."""

        return not any(
            (
                self.differences,
                self.missing_from_left,
                self.missing_from_right,
                self.duplicate_left,
                self.duplicate_right,
                self.fields_only_left,
                self.fields_only_right,
                self.validation_violations,
            )
        )

    @property
    def difference_count(self) -> int:
        """Count reconciliation discrepancies without validation violations."""

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

    @property
    def validation_violation_count(self) -> int:
        """Count data-quality rule violations across both inputs."""

        return len(self.validation_violations)

    @property
    def issue_count(self) -> int:
        """Count reconciliation discrepancies and validation violations."""

        return self.difference_count + self.validation_violation_count
