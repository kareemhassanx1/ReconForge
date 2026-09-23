"""Typed, deterministic normalization for source field values."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from reconforge.errors import InputError
from reconforge.model import CellValue, Dataset

_INTEGER_PATTERN = re.compile(r"[+-]?[0-9]+")


class NormalizerKind(StrEnum):
    """Built-in value types accepted by the normalization layer."""

    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"


@dataclass(frozen=True, slots=True)
class FieldNormalizer:
    """A named normalization policy for one dataset column.

    Normalizers produce canonical strings so the existing public record model
    continues to preserve source values. A null value remains null for every
    type; required-value validation belongs to the validation layer.
    """

    kind: NormalizerKind | str

    def __post_init__(self) -> None:
        if isinstance(self.kind, NormalizerKind):
            return
        if not isinstance(self.kind, str):
            raise TypeError("normalizer kind must be a NormalizerKind or string")
        try:
            resolved = NormalizerKind(self.kind.strip().lower())
        except ValueError as exc:
            choices = ", ".join(kind.value for kind in NormalizerKind)
            raise ValueError(
                f"unknown normalizer type {self.kind!r}; expected one of: {choices}"
            ) from exc
        object.__setattr__(self, "kind", resolved)

    @classmethod
    def named(cls, name: str) -> FieldNormalizer:
        """Construct a built-in normalizer from its public CLI name."""

        try:
            return cls(NormalizerKind(name.strip().lower()))
        except ValueError as exc:
            choices = ", ".join(kind.value for kind in NormalizerKind)
            raise ValueError(
                f"unknown normalizer type {name!r}; expected one of: {choices}"
            ) from exc

    @classmethod
    def text(cls) -> FieldNormalizer:
        return cls(NormalizerKind.TEXT)

    @classmethod
    def integer(cls) -> FieldNormalizer:
        return cls(NormalizerKind.INTEGER)

    @classmethod
    def decimal(cls) -> FieldNormalizer:
        return cls(NormalizerKind.DECIMAL)

    @classmethod
    def date(cls) -> FieldNormalizer:
        return cls(NormalizerKind.DATE)

    def normalize(self, value: CellValue) -> CellValue:
        """Return the canonical representation or raise a stable value error."""

        if value is None:
            return None
        if self.kind is NormalizerKind.TEXT:
            return _normalize_text(value)
        if self.kind is NormalizerKind.INTEGER:
            return _normalize_integer(value)
        if self.kind is NormalizerKind.DECIMAL:
            return _normalize_decimal(value)
        if self.kind is NormalizerKind.DATE:
            return _normalize_date(value)
        raise AssertionError(f"unhandled normalizer kind: {self.kind}")


@dataclass(frozen=True, slots=True)
class NormalizationIssue:
    """One source value that a configured normalizer could not interpret."""

    source: str
    row_number: int
    field: str
    value: str
    kind: NormalizerKind
    reason: str

    def describe(self) -> str:
        return (
            f"{self.source} row {self.row_number} field {self.field!r} "
            f"value {self.value!r} as {self.kind.value}: {self.reason}"
        )


class NormalizationError(InputError):
    """Raised with every invalid configured value in deterministic order."""

    def __init__(self, issues: tuple[NormalizationIssue, ...]) -> None:
        if not issues:
            raise ValueError("a normalization error requires at least one issue")
        self.issues = issues
        details = "; ".join(issue.describe() for issue in issues)
        super().__init__(f"normalization failed for {len(issues)} value(s): {details}")


def normalize_dataset_fields(
    dataset: Dataset,
    normalizers: Mapping[str, FieldNormalizer],
) -> dict[int, dict[str, CellValue]]:
    """Normalize configured fields for every row, collecting all bad values."""

    normalized: dict[int, dict[str, CellValue]] = {}
    issues: list[NormalizationIssue] = []

    for record in dataset.records:
        row: dict[str, CellValue] = {}
        for field in sorted(normalizers):
            normalizer = normalizers[field]
            value = record.value(field)
            try:
                row[field] = normalizer.normalize(value)
            except ValueError as exc:
                if value is None:
                    raise AssertionError("normalizers must preserve null values") from exc
                issues.append(
                    NormalizationIssue(
                        source=record.source,
                        row_number=record.row_number,
                        field=field,
                        value=value,
                        kind=NormalizerKind(normalizer.kind),
                        reason=str(exc),
                    )
                )
        normalized[record.row_number] = row

    if issues:
        raise NormalizationError(tuple(issues))
    return normalized


def _normalize_text(value: str) -> str:
    compatible = unicodedata.normalize("NFKC", value)
    return " ".join(compatible.split()).casefold()


def _normalize_integer(value: str) -> str:
    if _INTEGER_PATTERN.fullmatch(value) is None:
        raise ValueError("expected a signed base-10 integer")
    return str(int(value, 10))


def _normalize_decimal(value: str) -> str:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("expected a finite decimal number") from exc
    if not parsed.is_finite():
        raise ValueError("expected a finite decimal number")
    if parsed.is_zero():
        return "0"
    return str(parsed.normalize())


def _normalize_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError("expected an ISO calendar date in YYYY-MM-DD format") from exc
