"""Immutable dataset contracts and deterministic contract validation."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType

from reconforge.model import CellValue, Dataset, Record
from reconforge.profiling import ValueKind, classify_value


class ContractValueType(StrEnum):
    """Lexical value categories supported by dataset contracts."""

    BOOLEAN = "boolean"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"
    TEXT = "text"

    @classmethod
    def named(cls, name: str) -> ContractValueType:
        """Resolve a user-facing type name without accepting ambiguous aliases."""

        if not isinstance(name, str):
            raise TypeError("contract value type name must be a string")
        normalized = name.strip().lower()
        try:
            return cls(normalized)
        except ValueError as exc:
            choices = ", ".join(item.value for item in cls)
            raise ValueError(
                f"unknown contract value type {name!r}; expected one of: {choices}"
            ) from exc

    def accepts(self, value: str) -> bool:
        """Return whether ``value`` has this type's canonical lexical form."""

        if not isinstance(value, str):
            return False
        return classify_value(value).value == self.value


class ContractViolationCode(StrEnum):
    """Stable machine-readable reasons for row-level contract failures."""

    FIELD_MISSING = "field_missing"
    NULL_NOT_ALLOWED = "null_not_allowed"
    TYPE_MISMATCH = "type_mismatch"
    VALUE_NOT_ALLOWED = "value_not_allowed"
    PATTERN_MISMATCH = "pattern_mismatch"
    LENGTH_BELOW_MINIMUM = "length_below_minimum"
    LENGTH_ABOVE_MAXIMUM = "length_above_maximum"
    VALUE_BELOW_MINIMUM = "value_below_minimum"
    VALUE_ABOVE_MAXIMUM = "value_above_maximum"
    DUPLICATE_VALUE = "duplicate_value"


@dataclass(frozen=True, slots=True)
class FieldContract:
    """Constraints applied to one named field in a :class:`DatasetContract`.

    Values in :class:`~reconforge.model.Dataset` are strings or ``None``.  The
    allowed types therefore describe canonical lexical forms rather than
    converting values.  Validation is observational and never mutates the
    source dataset.
    """

    required: bool = True
    nullable: bool = True
    unique: bool = False
    allowed_types: tuple[ContractValueType, ...] = (ContractValueType.TEXT,)
    allowed_values: tuple[str, ...] | None = None
    pattern: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    minimum: Decimal | str | int | None = None
    maximum: Decimal | str | int | None = None
    _compiled_pattern: re.Pattern[str] | None = field(
        init=False,
        repr=False,
        compare=False,
        default=None,
    )

    def __post_init__(self) -> None:
        _require_boolean(self.required, "field contract required")
        _require_boolean(self.nullable, "field contract nullable")
        _require_boolean(self.unique, "field contract unique")

        value_types = _normalize_value_types(self.allowed_types)
        allowed_values = _normalize_allowed_values(self.allowed_values)
        minimum = _optional_decimal(self.minimum, "field contract minimum")
        maximum = _optional_decimal(self.maximum, "field contract maximum")
        min_length = _optional_length(self.min_length, "field contract min_length")
        max_length = _optional_length(self.max_length, "field contract max_length")
        compiled = _optional_pattern(self.pattern)

        if min_length is not None and max_length is not None and min_length > max_length:
            raise ValueError("field contract min_length cannot exceed max_length")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("field contract minimum cannot exceed maximum")

        text_constraints = compiled is not None or min_length is not None or max_length is not None
        if text_constraints and ContractValueType.TEXT not in value_types:
            raise ValueError("pattern and length constraints require the text value type")
        if minimum is not None or maximum is not None:
            numeric = {ContractValueType.INTEGER, ContractValueType.DECIMAL}
            if not set(value_types).issubset(numeric):
                raise ValueError("decimal bounds require only integer or decimal value types")

        object.__setattr__(self, "allowed_types", value_types)
        object.__setattr__(self, "allowed_values", allowed_values)
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)
        object.__setattr__(self, "min_length", min_length)
        object.__setattr__(self, "max_length", max_length)
        object.__setattr__(self, "_compiled_pattern", compiled)
        self._validate_allowed_values()

    def _validate_allowed_values(self) -> None:
        if self.allowed_values is None:
            return
        for value in self.allowed_values:
            if not any(value_type.accepts(value) for value_type in self.allowed_types):
                names = ", ".join(item.value for item in self.allowed_types)
                raise ValueError(f"allowed value {value!r} does not match an allowed type: {names}")
            kind = classify_value(value)
            if (
                self._compiled_pattern is not None
                and kind is ValueKind.TEXT
                and self._compiled_pattern.fullmatch(value) is None
            ):
                raise ValueError(f"allowed value {value!r} does not match pattern {self.pattern!r}")
            if (
                self.min_length is not None
                and kind is ValueKind.TEXT
                and len(value) < self.min_length
            ):
                raise ValueError(
                    f"allowed value {value!r} is shorter than min_length {self.min_length}"
                )
            if (
                self.max_length is not None
                and kind is ValueKind.TEXT
                and len(value) > self.max_length
            ):
                raise ValueError(
                    f"allowed value {value!r} is longer than max_length {self.max_length}"
                )
            parsed = _finite_decimal(value)
            if self.minimum is not None and (parsed is None or parsed < self.minimum):
                raise ValueError(
                    f"allowed value {value!r} is below minimum {format_decimal(self.minimum)}"
                )
            if self.maximum is not None and (parsed is None or parsed > self.maximum):
                raise ValueError(
                    f"allowed value {value!r} is above maximum {format_decimal(self.maximum)}"
                )


@dataclass(frozen=True, slots=True)
class DatasetContract:
    """An exact, immutable mapping of field names to their constraints."""

    fields: Mapping[str, FieldContract]
    allow_extra_fields: bool = False

    def __post_init__(self) -> None:
        _require_boolean(self.allow_extra_fields, "dataset contract allow_extra_fields")
        if not isinstance(self.fields, Mapping):
            raise TypeError("dataset contract fields must be a mapping")
        if not self.fields:
            raise ValueError("dataset contract requires at least one field")
        copied: dict[str, FieldContract] = {}
        for name, contract in self.fields.items():
            canonical_name = _field_name(name, "dataset contract field name")
            if canonical_name in copied:
                raise ValueError(f"dataset contract contains duplicate field {canonical_name!r}")
            if not isinstance(contract, FieldContract):
                raise TypeError(
                    f"dataset contract field {canonical_name!r} must be a FieldContract"
                )
            copied[canonical_name] = contract
        object.__setattr__(
            self,
            "fields",
            MappingProxyType(dict(sorted(copied.items(), key=lambda item: item[0]))),
        )

    def field(self, name: str) -> FieldContract:
        """Return one declared field or raise an explanatory ``KeyError``."""

        try:
            return self.fields[name]
        except KeyError as exc:
            raise KeyError(f"field {name!r} is not declared by the dataset contract") from exc


@dataclass(frozen=True, slots=True)
class ContractViolation:
    """One deterministic row-level contract violation."""

    source: str
    row_number: int
    field: str
    value: CellValue
    code: ContractViolationCode
    message: str

    def __post_init__(self) -> None:
        _source_text(self.source, "contract violation source")
        if type(self.row_number) is not int or self.row_number < 1:
            raise ValueError("contract violation row_number must be a positive integer")
        _field_name(self.field, "contract violation field")
        if self.value is not None and not isinstance(self.value, str):
            raise TypeError("contract violation value must be a string or null")
        if not isinstance(self.code, ContractViolationCode):
            raise TypeError("contract violation code must be a ContractViolationCode")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("contract violation message must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ContractResult:
    """Complete immutable outcome of validating one dataset contract."""

    source: str
    row_count: int
    checked_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]
    extra_fields: tuple[str, ...]
    violations: tuple[ContractViolation, ...]

    def __post_init__(self) -> None:
        _source_text(self.source, "contract result source")
        if type(self.row_count) is not int or self.row_count < 0:
            raise ValueError("contract result row_count must be a non-negative integer")
        checked = _canonical_field_names(self.checked_fields, "checked_fields")
        missing = _canonical_field_names(self.missing_fields, "missing_fields")
        extra = _canonical_field_names(self.extra_fields, "extra_fields")
        if set(checked) & set(missing):
            raise ValueError("contract result fields cannot be both checked and missing")
        if (set(checked) | set(missing)) & set(extra):
            raise ValueError("contract result extra fields cannot be contracted fields")
        violations = tuple(self.violations)
        if any(not isinstance(item, ContractViolation) for item in violations):
            raise TypeError("contract result violations must be ContractViolation instances")
        for violation in violations:
            if violation.field not in checked:
                raise ValueError(
                    f"contract violation field {violation.field!r} is not a checked field"
                )
        if len(violations) != len(set(violations)):
            raise ValueError("contract result violations must be unique")
        object.__setattr__(self, "checked_fields", checked)
        object.__setattr__(self, "missing_fields", missing)
        object.__setattr__(self, "extra_fields", extra)
        object.__setattr__(self, "violations", tuple(sorted(violations, key=_violation_sort_key)))

    @property
    def issue_count(self) -> int:
        """Return schema and row issues represented by this result."""

        return len(self.missing_fields) + len(self.extra_fields) + len(self.violations)

    @property
    def is_valid(self) -> bool:
        """Whether the dataset conforms completely to its contract."""

        return self.issue_count == 0


def validate_dataset_contract(dataset: Dataset, contract: DatasetContract) -> ContractResult:
    """Validate all applicable constraints without changing ``dataset``.

    Schema differences and every row-level issue are collected.  Output order
    depends only on row numbers, field names, and stable violation codes, not
    on mapping insertion order.
    """

    if not isinstance(dataset, Dataset):
        raise TypeError("dataset must be a Dataset instance")
    if not isinstance(contract, DatasetContract):
        raise TypeError("contract must be a DatasetContract instance")
    _validate_dataset_shape(dataset)

    headers = set(dataset.headers)
    declared = set(contract.fields)
    missing_fields = tuple(
        sorted(name for name in declared - headers if contract.fields[name].required)
    )
    extra_fields = () if contract.allow_extra_fields else tuple(sorted(headers - declared))
    checked_fields = tuple(sorted(headers & declared))
    violations: list[ContractViolation] = []

    for record in sorted(dataset.records, key=lambda item: (item.row_number, item.source)):
        for name in checked_fields:
            field_contract = contract.fields[name]
            if name not in record.values:
                violations.append(
                    ContractViolation(
                        source=record.source,
                        row_number=record.row_number,
                        field=name,
                        value=None,
                        code=ContractViolationCode.FIELD_MISSING,
                        message="field is absent from the record",
                    )
                )
                continue
            value = record.values[name]
            violations.extend(
                _value_violations(
                    record.source,
                    record.row_number,
                    name,
                    value,
                    field_contract,
                )
            )

    for name in checked_fields:
        field_contract = contract.fields[name]
        if not field_contract.unique:
            continue
        occurrences: dict[str, list[tuple[str, int]]] = {}
        for record in dataset.records:
            value = record.values.get(name)
            if value is not None:
                occurrences.setdefault(value, []).append((record.source, record.row_number))
        for value, locations in occurrences.items():
            if len(locations) < 2:
                continue
            rows = ", ".join(str(row_number) for row_number in sorted(row for _, row in locations))
            for source, row_number in locations:
                violations.append(
                    ContractViolation(
                        source=source,
                        row_number=row_number,
                        field=name,
                        value=value,
                        code=ContractViolationCode.DUPLICATE_VALUE,
                        message=f"non-null value is duplicated at rows {rows}",
                    )
                )

    return ContractResult(
        source=dataset.source,
        row_count=len(dataset.records),
        checked_fields=checked_fields,
        missing_fields=missing_fields,
        extra_fields=extra_fields,
        violations=tuple(violations),
    )


def format_decimal(value: Decimal) -> str:
    """Return a bounded canonical representation for one finite decimal.

    Ordinary magnitudes use a readable fixed-point form.  Extreme exponents
    use canonical scientific notation so a tiny input cannot force allocation
    of an enormous zero-filled string.
    """

    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("value must be a finite Decimal")
    if value.is_zero():
        return "0"
    sign, raw_digits, raw_exponent = value.as_tuple()
    if not isinstance(raw_exponent, int):
        raise ValueError("value must be a finite Decimal")
    digits = list(raw_digits)
    exponent = raw_exponent
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits)
    prefix = "-" if sign else ""
    point = len(coefficient) + exponent
    if 0 < point <= len(coefficient):
        whole = coefficient[:point]
        fraction = coefficient[point:]
        return prefix + whole + (f".{fraction}" if fraction else "")
    if point > len(coefficient) and point <= 100:
        return prefix + coefficient + ("0" * (point - len(coefficient)))
    if -6 <= point <= 0:
        return prefix + "0." + ("0" * -point) + coefficient
    adjusted = point - 1
    mantissa = coefficient[0]
    if len(coefficient) > 1:
        mantissa += "." + coefficient[1:]
    exponent_text = f"+{adjusted}" if adjusted >= 0 else str(adjusted)
    return f"{prefix}{mantissa}E{exponent_text}"


def _value_violations(
    source: str,
    row_number: int,
    name: str,
    value: CellValue,
    contract: FieldContract,
) -> list[ContractViolation]:
    violations: list[ContractViolation] = []

    def add(code: ContractViolationCode, message: str) -> None:
        violations.append(
            ContractViolation(
                source=source,
                row_number=row_number,
                field=name,
                value=value,
                code=code,
                message=message,
            )
        )

    if value is None:
        if not contract.nullable:
            add(ContractViolationCode.NULL_NOT_ALLOWED, "null value is not allowed")
        return violations

    if not any(value_type.accepts(value) for value_type in contract.allowed_types):
        expected = ", ".join(item.value for item in contract.allowed_types)
        add(ContractViolationCode.TYPE_MISMATCH, f"expected value type: {expected}")
    if contract.allowed_values is not None and value not in contract.allowed_values:
        expected = ", ".join(repr(item) for item in contract.allowed_values)
        add(ContractViolationCode.VALUE_NOT_ALLOWED, f"expected one of: {expected}")
    kind = classify_value(value)
    if (
        contract._compiled_pattern is not None
        and kind is ValueKind.TEXT
        and contract._compiled_pattern.fullmatch(value) is None
    ):
        add(
            ContractViolationCode.PATTERN_MISMATCH,
            f"expected full match for pattern {contract.pattern!r}",
        )
    if (
        contract.min_length is not None
        and kind is ValueKind.TEXT
        and len(value) < contract.min_length
    ):
        add(
            ContractViolationCode.LENGTH_BELOW_MINIMUM,
            f"length {len(value)} is below minimum {contract.min_length}",
        )
    if (
        contract.max_length is not None
        and kind is ValueKind.TEXT
        and len(value) > contract.max_length
    ):
        add(
            ContractViolationCode.LENGTH_ABOVE_MAXIMUM,
            f"length {len(value)} exceeds maximum {contract.max_length}",
        )

    parsed = _finite_decimal(value)
    if parsed is not None:
        if contract.minimum is not None and parsed < contract.minimum:
            add(
                ContractViolationCode.VALUE_BELOW_MINIMUM,
                f"value is below inclusive minimum {format_decimal(contract.minimum)}",
            )
        if contract.maximum is not None and parsed > contract.maximum:
            add(
                ContractViolationCode.VALUE_ABOVE_MAXIMUM,
                f"value exceeds inclusive maximum {format_decimal(contract.maximum)}",
            )
    return violations


def _normalize_value_types(value: object) -> tuple[ContractValueType, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError("field contract allowed_types must be a sequence of ContractValueType")
    types = tuple(value)
    if not types:
        raise ValueError("field contract requires at least one allowed type")
    if any(not isinstance(item, ContractValueType) for item in types):
        raise TypeError("field contract allowed_types must contain ContractValueType values")
    counts = Counter(types)
    duplicates = sorted(item.value for item, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"field contract allowed_types must be unique; repeated: {duplicates!r}")
    order = {item: index for index, item in enumerate(ContractValueType)}
    return tuple(sorted(types, key=order.__getitem__))


def _normalize_allowed_values(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError("field contract allowed_values must be a sequence of strings or null")
    values = tuple(value)
    if not values:
        raise ValueError("field contract allowed_values cannot be empty")
    if any(not isinstance(item, str) for item in values):
        raise TypeError("field contract allowed_values must contain only strings")
    if len(values) != len(set(values)):
        repeated = sorted(item for item, count in Counter(values).items() if count > 1)
        raise ValueError(f"field contract allowed_values must be unique; repeated: {repeated!r}")
    return tuple(sorted(values))


def _optional_pattern(value: object) -> re.Pattern[str] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("field contract pattern must be a non-empty string or null")
    try:
        return re.compile(value)
    except re.error as exc:
        raise ValueError(f"invalid field contract pattern: {exc}") from exc


def _optional_length(value: object, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer or null")
    return value


def _optional_decimal(value: object, label: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Decimal | str | int):
        raise TypeError(f"{label} must be a Decimal, string, integer, or null")
    try:
        parsed = Decimal(value.strip() if isinstance(value, str) else value)
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be a finite decimal")
    return parsed


def _finite_decimal(value: str) -> Decimal | None:
    if classify_value(value).value not in {
        ContractValueType.INTEGER.value,
        ContractValueType.DECIMAL.value,
    }:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _require_boolean(value: object, label: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{label} must be a boolean")


def _field_name(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty string without surrounding whitespace")
    return value


def _canonical_field_names(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError(f"contract result {label} must be a sequence of strings")
    names = tuple(_field_name(item, f"contract result {label} item") for item in value)
    if len(names) != len(set(names)):
        raise ValueError(f"contract result {label} must contain unique fields")
    return tuple(sorted(names))


def _violation_sort_key(item: ContractViolation) -> tuple[object, ...]:
    return (
        item.row_number,
        item.field,
        item.code.value,
        item.source,
        "" if item.value is None else item.value,
        item.message,
    )


def _source_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not value:
        raise ValueError(f"{label} must be non-empty")
    return value


def _validate_dataset_shape(dataset: Dataset) -> None:
    _source_text(dataset.source, "dataset source")
    for index, header in enumerate(dataset.headers):
        _field_name(header, f"dataset header[{index}]")
    headers = set(dataset.headers)
    rows: set[int] = set()
    for index, record in enumerate(dataset.records):
        if not isinstance(record, Record):
            raise TypeError(f"dataset records[{index}] must be a Record instance")
        if record.row_number in rows:
            raise ValueError(f"dataset record row numbers must be unique: {record.row_number}")
        rows.add(record.row_number)
        _source_text(record.source, f"dataset record at row {record.row_number} source")
        for raw_field, value in record.values.items():
            field = _field_name(
                raw_field,
                f"dataset record at row {record.row_number} field name",
            )
            if field not in headers:
                raise ValueError(
                    f"dataset record at row {record.row_number} contains field {field!r} "
                    "that is absent from the dataset headers"
                )
            if value is not None and not isinstance(value, str):
                raise TypeError(
                    f"dataset record at row {record.row_number} field {field!r} "
                    "must be a string or null"
                )
