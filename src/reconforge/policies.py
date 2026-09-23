"""Explicit, deterministic policies for comparing field values."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import cast

from reconforge.errors import InputError
from reconforge.model import CellValue, Dataset

PreparedValue = str | Decimal | date | None


class ComparisonPolicyKind(StrEnum):
    """Built-in comparison strategies available to public configuration."""

    EXACT = "exact"
    DECIMAL = "decimal"
    DATE = "date"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class ComparisonPolicy:
    """Rules for deciding whether two values in one column are equivalent."""

    kind: ComparisonPolicyKind | str
    tolerance: Decimal | str | int = Decimal("0")

    def __post_init__(self) -> None:
        kind = _resolve_kind(self.kind)
        tolerance = _resolve_tolerance(self.tolerance)
        if kind is not ComparisonPolicyKind.DECIMAL and tolerance != 0:
            raise ValueError(f"{kind.value} comparison does not accept a tolerance")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "tolerance", tolerance)

    @classmethod
    def named(cls, specification: str) -> ComparisonPolicy:
        """Construct a policy from ``TYPE`` or ``decimal:TOLERANCE`` syntax."""

        name, separator, option = specification.strip().partition(":")
        kind = _resolve_kind(name)
        if kind is ComparisonPolicyKind.DECIMAL:
            if separator and not option.strip():
                raise ValueError("decimal comparison tolerance cannot be empty")
            return cls(kind, option.strip() if separator else Decimal("0"))
        if separator:
            raise ValueError(f"{kind.value} comparison does not accept an option")
        return cls(kind)

    @classmethod
    def exact(cls) -> ComparisonPolicy:
        return cls(ComparisonPolicyKind.EXACT)

    @classmethod
    def decimal(cls, tolerance: Decimal | str | int = Decimal("0")) -> ComparisonPolicy:
        return cls(ComparisonPolicyKind.DECIMAL, tolerance)

    @classmethod
    def date(cls) -> ComparisonPolicy:
        return cls(ComparisonPolicyKind.DATE)

    @classmethod
    def text(cls) -> ComparisonPolicy:
        return cls(ComparisonPolicyKind.TEXT)

    @property
    def descriptor(self) -> str:
        """Return the stable public representation recorded in reports."""

        if self.kind is ComparisonPolicyKind.DECIMAL:
            return f"decimal:tolerance={_format_decimal(cast(Decimal, self.tolerance))}"
        return str(self.kind)

    def prepare(self, value: CellValue) -> PreparedValue:
        """Convert one source value to the form used by this policy."""

        if value is None or self.kind is ComparisonPolicyKind.EXACT:
            return value
        if self.kind is ComparisonPolicyKind.DECIMAL:
            return _parse_decimal(value)
        if self.kind is ComparisonPolicyKind.DATE:
            return _parse_date(value)
        if self.kind is ComparisonPolicyKind.TEXT:
            return _normalize_text(value)
        raise AssertionError(f"unhandled comparison policy kind: {self.kind}")

    def values_match(self, left: PreparedValue, right: PreparedValue) -> bool:
        """Compare two values previously prepared by this policy."""

        if left is None or right is None:
            return left is right
        if self.kind is ComparisonPolicyKind.DECIMAL:
            if not isinstance(left, Decimal) or not isinstance(right, Decimal):
                raise TypeError("decimal comparison requires prepared decimal values")
            return abs(left - right) <= cast(Decimal, self.tolerance)
        return left == right


@dataclass(frozen=True, slots=True)
class ComparisonIssue:
    """One source value rejected by its configured comparison policy."""

    source: str
    row_number: int
    field: str
    value: str
    policy: str
    reason: str

    def describe(self) -> str:
        return (
            f"{self.source} row {self.row_number} field {self.field!r} "
            f"value {self.value!r} for {self.policy}: {self.reason}"
        )


class ComparisonError(InputError):
    """Raised with every policy-invalid value in deterministic order."""

    def __init__(self, issues: tuple[ComparisonIssue, ...]) -> None:
        if not issues:
            raise ValueError("a comparison error requires at least one issue")
        self.issues = issues
        details = "; ".join(issue.describe() for issue in issues)
        super().__init__(f"comparison validation failed for {len(issues)} value(s): {details}")


def prepare_dataset_fields(
    dataset: Dataset,
    policies: Mapping[str, ComparisonPolicy],
    normalized: Mapping[int, Mapping[str, CellValue]],
) -> dict[int, dict[str, PreparedValue]]:
    """Prepare policy fields for every row and collect all invalid values."""

    prepared: dict[int, dict[str, PreparedValue]] = {}
    issues: list[ComparisonIssue] = []

    for record in dataset.records:
        row: dict[str, PreparedValue] = {}
        for field in sorted(policies):
            policy = policies[field]
            raw_value = record.value(field)
            value = normalized[record.row_number].get(field, raw_value)
            try:
                row[field] = policy.prepare(value)
            except ValueError as exc:
                if value is None:
                    raise AssertionError("comparison policies must preserve null values") from exc
                issues.append(
                    ComparisonIssue(
                        source=record.source,
                        row_number=record.row_number,
                        field=field,
                        value=raw_value if raw_value is not None else value,
                        policy=policy.descriptor,
                        reason=str(exc),
                    )
                )
        prepared[record.row_number] = row

    if issues:
        raise ComparisonError(tuple(issues))
    return prepared


def _resolve_kind(kind: ComparisonPolicyKind | str) -> ComparisonPolicyKind:
    if isinstance(kind, ComparisonPolicyKind):
        return kind
    if not isinstance(kind, str):
        raise TypeError("comparison policy kind must be a ComparisonPolicyKind or string")
    try:
        return ComparisonPolicyKind(kind.strip().lower())
    except ValueError as exc:
        choices = ", ".join(item.value for item in ComparisonPolicyKind)
        raise ValueError(f"unknown comparison policy {kind!r}; expected one of: {choices}") from exc


def _resolve_tolerance(value: Decimal | str | int) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal | str | int):
        raise TypeError("decimal tolerance must be a Decimal, string, or integer")
    try:
        tolerance = Decimal(value.strip() if isinstance(value, str) else value)
    except InvalidOperation as exc:
        raise ValueError("decimal tolerance must be a finite non-negative number") from exc
    if not tolerance.is_finite() or tolerance < 0:
        raise ValueError("decimal tolerance must be a finite non-negative number")
    return tolerance


def _parse_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("expected a finite decimal number") from exc
    if not parsed.is_finite():
        raise ValueError("expected a finite decimal number")
    return parsed


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("expected an ISO calendar date") from exc


def _normalize_text(value: str) -> str:
    compatible = unicodedata.normalize("NFKC", value)
    return " ".join(compatible.split()).casefold()


def _format_decimal(value: Decimal) -> str:
    formatted = format(value, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted or "0"
