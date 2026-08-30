"""Composable data-quality rules for reconciliation inputs."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from reconforge.model import CellValue, Dataset, ValidationViolation


class ValidationRuleKind(StrEnum):
    """Built-in data-quality checks available for dataset fields."""

    REQUIRED = "required"
    ALLOWED = "allowed"
    NUMERIC_RANGE = "numeric_range"
    PATTERN = "pattern"


@dataclass(frozen=True, slots=True)
class RequiredRule:
    """Require a field to contain a non-null value."""

    @property
    def kind(self) -> ValidationRuleKind:
        return ValidationRuleKind.REQUIRED

    @property
    def descriptor(self) -> str:
        return self.kind.value

    def check(self, value: CellValue) -> str | None:
        return "value is required" if value is None else None


@dataclass(frozen=True, slots=True)
class AllowedValuesRule:
    """Require non-null values to belong to an explicit exact-value set."""

    values: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.values, str):
            raise TypeError("allowed values must be provided as a sequence of strings")
        values = tuple(self.values)
        if not values:
            raise ValueError("allowed-values rule requires at least one value")
        if any(not isinstance(value, str) for value in values):
            raise TypeError("allowed values must be strings")
        duplicates = sorted({value for value in values if values.count(value) > 1})
        if duplicates:
            raise ValueError(f"allowed values must be unique; repeated: {duplicates!r}")
        object.__setattr__(self, "values", values)

    @property
    def kind(self) -> ValidationRuleKind:
        return ValidationRuleKind.ALLOWED

    @property
    def descriptor(self) -> str:
        encoded = json.dumps(self.values, ensure_ascii=False, separators=(",", ":"))
        return f"allowed:{encoded}"

    def check(self, value: CellValue) -> str | None:
        if value is None or value in self.values:
            return None
        choices = ", ".join(repr(item) for item in self.values)
        return f"expected one of: {choices}"


@dataclass(frozen=True, slots=True)
class NumericRangeRule:
    """Require finite decimal values to fall within configured bounds."""

    minimum: Decimal | str | int | None = None
    maximum: Decimal | str | int | None = None
    minimum_inclusive: bool = True
    maximum_inclusive: bool = True

    def __post_init__(self) -> None:
        minimum = _optional_decimal_bound(self.minimum, "minimum")
        maximum = _optional_decimal_bound(self.maximum, "maximum")
        if minimum is None and maximum is None:
            raise ValueError("numeric-range rule requires a minimum or maximum")
        if type(self.minimum_inclusive) is not bool or type(self.maximum_inclusive) is not bool:
            raise TypeError("numeric-range inclusivity settings must be booleans")
        if minimum is not None and maximum is not None:
            if minimum > maximum:
                raise ValueError("numeric-range minimum cannot exceed maximum")
            if minimum == maximum and not (self.minimum_inclusive and self.maximum_inclusive):
                raise ValueError("equal numeric-range bounds must both be inclusive")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)

    @property
    def kind(self) -> ValidationRuleKind:
        return ValidationRuleKind.NUMERIC_RANGE

    @property
    def descriptor(self) -> str:
        left = "(" if self.minimum is None else ("[" if self.minimum_inclusive else "(")
        right = ")" if self.maximum is None else ("]" if self.maximum_inclusive else ")")
        minimum = "-inf" if self.minimum is None else _format_decimal(self.minimum)
        maximum = "+inf" if self.maximum is None else _format_decimal(self.maximum)
        return f"numeric_range:{left}{minimum},{maximum}{right}"

    def check(self, value: CellValue) -> str | None:
        if value is None:
            return None
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return "expected a finite decimal number"
        if not parsed.is_finite():
            return "expected a finite decimal number"
        if self.minimum is not None:
            below_minimum = parsed < self.minimum or (
                parsed == self.minimum and not self.minimum_inclusive
            )
            if below_minimum:
                return f"expected a value within {self._display_range()}"
        if self.maximum is not None:
            above_maximum = parsed > self.maximum or (
                parsed == self.maximum and not self.maximum_inclusive
            )
            if above_maximum:
                return f"expected a value within {self._display_range()}"
        return None

    def _display_range(self) -> str:
        return self.descriptor.removeprefix("numeric_range:")


@dataclass(frozen=True, slots=True)
class TextPatternRule:
    """Require non-null text to fully match a regular-expression pattern."""

    pattern: str
    _compiled: re.Pattern[str] = dataclass_field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.pattern, str) or not self.pattern:
            raise ValueError("text-pattern rule requires a non-empty pattern")
        try:
            compiled = re.compile(self.pattern)
        except re.error as exc:
            raise ValueError(f"invalid text pattern: {exc}") from exc
        object.__setattr__(self, "_compiled", compiled)

    @property
    def kind(self) -> ValidationRuleKind:
        return ValidationRuleKind.PATTERN

    @property
    def descriptor(self) -> str:
        return f"pattern:{self.pattern}"

    def check(self, value: CellValue) -> str | None:
        if value is None or self._compiled.fullmatch(value) is not None:
            return None
        return f"expected text matching full pattern {self.pattern!r}"


ValidationRule = RequiredRule | AllowedValuesRule | NumericRangeRule | TextPatternRule
VALIDATION_RULE_TYPES = (RequiredRule, AllowedValuesRule, NumericRangeRule, TextPatternRule)


def validate_dataset_fields(
    dataset: Dataset,
    rules: Mapping[str, tuple[ValidationRule, ...]],
    normalized: Mapping[int, Mapping[str, CellValue]],
    *,
    side: str,
) -> tuple[ValidationViolation, ...]:
    """Evaluate every configured rule in deterministic row, field, and rule order."""

    violations: list[ValidationViolation] = []
    for record in dataset.records:
        for field in sorted(rules):
            raw_value = record.value(field)
            value = normalized[record.row_number].get(field, raw_value)
            for rule in rules[field]:
                message = rule.check(value)
                if message is not None:
                    violations.append(
                        ValidationViolation(
                            side=side,
                            source=record.source,
                            row_number=record.row_number,
                            field=field,
                            value=raw_value,
                            rule=rule.descriptor,
                            message=message,
                        )
                    )
    return tuple(violations)


def _optional_decimal_bound(
    value: Decimal | str | int | None,
    label: str,
) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Decimal | str | int):
        raise TypeError(f"numeric-range {label} must be a Decimal, string, or integer")
    try:
        parsed = Decimal(value.strip() if isinstance(value, str) else value)
    except InvalidOperation as exc:
        raise ValueError(f"numeric-range {label} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"numeric-range {label} must be a finite decimal")
    return parsed


def _format_decimal(value: Decimal) -> str:
    formatted = format(value, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted or "0"
