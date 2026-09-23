"""Deterministic, immutable profiles for normalized tabular datasets."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from reconforge.errors import InputError
from reconforge.json_safety import JsonNestingTooDeepError, validate_json_nesting
from reconforge.model import CellValue, Dataset

_INTEGER_PATTERN = re.compile(r"[+-]?[0-9]+")
_DECIMAL_PATTERN = re.compile(
    r"[+-]?(?:(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|"
    r"[0-9]+[eE][+-]?[0-9]+)"
)
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_PROFILE_FIELDS = frozenset(
    {"profile_version", "fingerprint", "source", "record_count", "options", "fields"}
)
_OPTIONS_FIELDS = frozenset({"top_values_limit", "examples_limit"})
_FIELD_FIELDS = frozenset(
    {
        "name",
        "total_count",
        "null_count",
        "non_null_count",
        "distinct_count",
        "kind_counts",
        "min_length",
        "max_length",
        "numeric_range",
        "date_range",
        "top_values",
        "examples",
    }
)
_RANGE_FIELDS = frozenset({"minimum", "maximum"})
_TOP_VALUE_FIELDS = frozenset({"value", "count"})

PROFILE_VERSION = 1


class ValueKind(StrEnum):
    """Mutually exclusive value categories, evaluated in declaration order."""

    NULL = "null"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"
    TEXT = "text"


_KIND_ORDER: tuple[ValueKind, ...] = tuple(ValueKind)


@dataclass(frozen=True, slots=True)
class ProfileOptions:
    """Limits controlling bounded, deterministic samples in a dataset profile."""

    top_values_limit: int = 10
    examples_limit: int = 5

    def __post_init__(self) -> None:
        _bounded_limit(self.top_values_limit, "top_values_limit")
        _bounded_limit(self.examples_limit, "examples_limit")


@dataclass(frozen=True, slots=True)
class ValueFrequency:
    """One non-null source value and its exact occurrence count."""

    value: str
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("value frequency values must be strings")
        if type(self.count) is not int or self.count < 1:
            raise ValueError("value frequency counts must be positive integers")


@dataclass(frozen=True, slots=True)
class FieldProfile:
    """An immutable statistical summary for one dataset column."""

    name: str
    total_count: int
    null_count: int
    non_null_count: int
    distinct_count: int
    kind_counts: tuple[tuple[ValueKind, int], ...]
    min_length: int | None
    max_length: int | None
    numeric_range: tuple[str, str] | None
    date_range: tuple[str, str] | None
    top_values: tuple[ValueFrequency, ...]
    examples: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError("field profile names must be strings")
        for value, label in (
            (self.total_count, "total_count"),
            (self.null_count, "null_count"),
            (self.non_null_count, "non_null_count"),
            (self.distinct_count, "distinct_count"),
        ):
            _non_negative_integer(value, label)
        if self.null_count + self.non_null_count != self.total_count:
            raise ValueError("field null and non-null counts must equal total_count")
        if self.distinct_count > self.non_null_count:
            raise ValueError("field distinct_count cannot exceed non_null_count")
        if (self.distinct_count == 0) != (self.non_null_count == 0):
            raise ValueError(
                "field distinct_count must be zero exactly when non_null_count is zero"
            )

        if not isinstance(self.kind_counts, tuple):
            raise TypeError("kind_counts must be a tuple")
        expected_kinds = _KIND_ORDER
        actual_kinds = tuple(item[0] for item in self.kind_counts if len(item) == 2)
        if len(actual_kinds) != len(self.kind_counts) or actual_kinds != expected_kinds:
            raise ValueError("kind_counts must contain every ValueKind exactly once in order")
        for kind, count in self.kind_counts:
            if not isinstance(kind, ValueKind):
                raise TypeError("kind_counts keys must be ValueKind instances")
            _non_negative_integer(count, f"{kind.value} kind count")
        counts = dict(self.kind_counts)
        if sum(counts.values()) != self.total_count:
            raise ValueError("kind counts must equal total_count")
        if counts[ValueKind.NULL] != self.null_count:
            raise ValueError("null kind count must equal null_count")

        self._validate_lengths()
        self._validate_ranges(counts)
        self._validate_samples(counts)

    def _validate_lengths(self) -> None:
        if self.non_null_count == 0:
            if self.min_length is not None or self.max_length is not None:
                raise ValueError("an all-null field cannot have length bounds")
            return
        if type(self.min_length) is not int or self.min_length < 0:
            raise ValueError("min_length must be a non-negative integer for non-null fields")
        if type(self.max_length) is not int or self.max_length < self.min_length:
            raise ValueError("max_length must be an integer at least as large as min_length")

    def _validate_ranges(self, counts: Mapping[ValueKind, int]) -> None:
        numeric_count = counts[ValueKind.INTEGER] + counts[ValueKind.DECIMAL]
        numeric_applicable = self.non_null_count > 0 and numeric_count == self.non_null_count
        _validate_optional_range(
            self.numeric_range,
            label="numeric_range",
            applicable=numeric_applicable,
            value_validator=_canonical_decimal_value,
            order_key=_decimal_value,
        )
        date_applicable = self.non_null_count > 0 and counts[ValueKind.DATE] == self.non_null_count
        _validate_optional_range(
            self.date_range,
            label="date_range",
            applicable=date_applicable,
            value_validator=_canonical_date_value,
            order_key=date.fromisoformat,
        )

    def _validate_samples(self, counts: Mapping[ValueKind, int]) -> None:
        if not isinstance(self.top_values, tuple):
            raise TypeError("top_values must be a tuple")
        if not all(isinstance(item, ValueFrequency) for item in self.top_values):
            raise TypeError("top_values must contain ValueFrequency instances")
        if len(self.top_values) > self.distinct_count:
            raise ValueError("top_values cannot exceed distinct_count")
        if len({item.value for item in self.top_values}) != len(self.top_values):
            raise ValueError("top_values must contain unique values")
        expected_order = tuple(sorted(self.top_values, key=lambda item: (-item.count, item.value)))
        if self.top_values != expected_order:
            raise ValueError("top_values must be sorted by descending count then value")
        if sum(item.count for item in self.top_values) > self.non_null_count:
            raise ValueError("top value counts cannot exceed non_null_count")
        sampled_by_kind: Counter[ValueKind] = Counter()
        for item in self.top_values:
            sampled_by_kind[classify_value(item.value)] += item.count
        if any(sampled_by_kind[kind] > counts[kind] for kind in _KIND_ORDER):
            raise ValueError("top values are inconsistent with kind_counts")

        if not isinstance(self.examples, tuple) or any(
            not isinstance(value, str) for value in self.examples
        ):
            raise TypeError("examples must be a tuple of strings")
        if len(self.examples) > self.distinct_count:
            raise ValueError("examples cannot exceed distinct_count")
        if self.examples != tuple(sorted(set(self.examples))):
            raise ValueError("examples must contain unique values in sorted order")
        example_kinds = Counter(classify_value(value) for value in self.examples)
        if any(example_kinds[kind] > counts[kind] for kind in _KIND_ORDER):
            raise ValueError("examples are inconsistent with kind_counts")
        for value in (*tuple(item.value for item in self.top_values), *self.examples):
            length = len(value)
            if self.min_length is None or self.max_length is None:
                raise ValueError("non-null samples require length bounds")
            if not self.min_length <= length <= self.max_length:
                raise ValueError("sample values are inconsistent with length bounds")
            if self.numeric_range is not None:
                parsed = Decimal(value)
                minimum, maximum = (Decimal(item) for item in self.numeric_range)
                if not minimum <= parsed <= maximum:
                    raise ValueError("sample values are outside numeric_range")
            if self.date_range is not None:
                parsed_date = date.fromisoformat(value)
                minimum_date, maximum_date = (date.fromisoformat(item) for item in self.date_range)
                if not minimum_date <= parsed_date <= maximum_date:
                    raise ValueError("sample values are outside date_range")

    def count(self, kind: ValueKind | str) -> int:
        """Return the count for a kind without exposing a mutable mapping."""

        selected = kind if isinstance(kind, ValueKind) else ValueKind(kind)
        return dict(self.kind_counts)[selected]


@dataclass(frozen=True, slots=True)
class DatasetProfile:
    """A deterministic profile of every field in a single dataset."""

    source: str
    record_count: int
    options: ProfileOptions
    fields: tuple[FieldProfile, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source, str):
            raise TypeError("profile source must be a string")
        _non_negative_integer(self.record_count, "record_count")
        if not isinstance(self.options, ProfileOptions):
            raise TypeError("profile options must be a ProfileOptions instance")
        if not isinstance(self.fields, tuple) or any(
            not isinstance(field, FieldProfile) for field in self.fields
        ):
            raise TypeError("profile fields must be a tuple of FieldProfile instances")
        if not self.fields:
            raise ValueError("a dataset profile requires at least one field")
        names = tuple(field.name for field in self.fields)
        if len(names) != len(set(names)):
            raise ValueError("profile field names must be unique")
        for field in self.fields:
            if field.total_count != self.record_count:
                raise ValueError(
                    f"field {field.name!r} total_count must equal profile record_count"
                )
            if len(field.top_values) > self.options.top_values_limit:
                raise ValueError(f"field {field.name!r} exceeds the configured top_values_limit")
            if len(field.examples) > self.options.examples_limit:
                raise ValueError(f"field {field.name!r} exceeds the configured examples_limit")
            expected_top_values = min(field.distinct_count, self.options.top_values_limit)
            if len(field.top_values) != expected_top_values:
                raise ValueError(
                    f"field {field.name!r} must contain {expected_top_values} top values"
                )
            expected_examples = min(field.distinct_count, self.options.examples_limit)
            if len(field.examples) != expected_examples:
                raise ValueError(f"field {field.name!r} must contain {expected_examples} examples")
            if self.options.top_values_limit >= field.distinct_count:
                if sum(item.count for item in field.top_values) != field.non_null_count:
                    raise ValueError(
                        f"field {field.name!r} complete top values must count every non-null value"
                    )
                recorded_kinds: Counter[ValueKind] = Counter()
                for item in field.top_values:
                    recorded_kinds[classify_value(item.value)] += item.count
                expected_kinds = dict(field.kind_counts)
                if any(
                    recorded_kinds[kind] != expected_kinds[kind]
                    for kind in _KIND_ORDER
                    if kind is not ValueKind.NULL
                ):
                    raise ValueError(
                        f"field {field.name!r} complete top values must match kind_counts"
                    )
                complete_values = {item.value for item in field.top_values}
                if any(value not in complete_values for value in field.examples):
                    raise ValueError(
                        f"field {field.name!r} examples must occur in complete top values"
                    )

    @property
    def headers(self) -> tuple[str, ...]:
        """Return profiled field names in original dataset order."""

        return tuple(field.name for field in self.fields)

    def field(self, name: str) -> FieldProfile:
        """Look up a field profile by its exact source header."""

        for field in self.fields:
            if field.name == name:
                return field
        raise KeyError(f"field {name!r} does not exist in profile for {self.source}")


def classify_value(value: CellValue) -> ValueKind:
    """Classify a cell without coercing or trimming its source representation."""

    if value is None:
        return ValueKind.NULL
    if not isinstance(value, str):
        raise TypeError("profile values must be strings or null")
    if value.casefold() in {"true", "false"}:
        return ValueKind.BOOLEAN
    if _INTEGER_PATTERN.fullmatch(value):
        return ValueKind.INTEGER
    if _DECIMAL_PATTERN.fullmatch(value):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            pass
        else:
            if parsed.is_finite():
                return ValueKind.DECIMAL
    if _DATE_PATTERN.fullmatch(value):
        try:
            date.fromisoformat(value)
        except ValueError:
            pass
        else:
            return ValueKind.DATE
    return ValueKind.TEXT


def profile_dataset(
    dataset: Dataset,
    options: ProfileOptions | None = None,
) -> DatasetProfile:
    """Build a bounded profile while preserving dataset and header order."""

    if not isinstance(dataset, Dataset):
        raise TypeError("dataset must be a Dataset instance")
    selected = ProfileOptions() if options is None else options
    if not isinstance(selected, ProfileOptions):
        raise TypeError("options must be a ProfileOptions instance")
    _validate_dataset_records(dataset)
    return DatasetProfile(
        source=dataset.source,
        record_count=len(dataset.records),
        options=selected,
        fields=tuple(_profile_field(dataset, field, selected) for field in dataset.headers),
    )


def profile_to_dict(profile: DatasetProfile) -> dict[str, Any]:
    """Serialize a profile to its exact version-one canonical schema."""

    if not isinstance(profile, DatasetProfile):
        raise TypeError("profile must be a DatasetProfile instance")
    body = _profile_body(profile)
    return {
        "profile_version": PROFILE_VERSION,
        "fingerprint": _fingerprint_body(body),
        **body,
    }


def profile_from_dict(payload: Mapping[str, object]) -> DatasetProfile:
    """Load and semantically validate an exact version-one profile mapping."""

    profile_object = _exact_object(payload, "profile", _PROFILE_FIELDS)
    version = profile_object.get("profile_version")
    if type(version) is not int or version != PROFILE_VERSION:
        raise ValueError(f"unsupported profile version {version!r}; expected {PROFILE_VERSION}")
    fingerprint = profile_object.get("fingerprint")
    if not isinstance(fingerprint, str) or _FINGERPRINT_PATTERN.fullmatch(fingerprint) is None:
        raise ValueError("profile.fingerprint must be sha256 followed by 64 lowercase hex digits")

    options_object = _exact_object(
        profile_object.get("options"), "profile.options", _OPTIONS_FIELDS
    )
    options = ProfileOptions(
        top_values_limit=_integer(
            options_object.get("top_values_limit"), "profile.options.top_values_limit"
        ),
        examples_limit=_integer(
            options_object.get("examples_limit"), "profile.options.examples_limit"
        ),
    )
    record_count = _integer(profile_object.get("record_count"), "profile.record_count")
    source = _string(profile_object.get("source"), "profile.source")
    fields = tuple(
        _field_from_dict(item, index)
        for index, item in enumerate(_array(profile_object.get("fields"), "profile.fields"))
    )
    result = DatasetProfile(
        source=source,
        record_count=record_count,
        options=options,
        fields=fields,
    )
    expected = profile_fingerprint(result)
    if fingerprint != expected:
        raise ValueError(f"profile fingerprint is {fingerprint!r}; expected {expected!r}")
    return result


def profile_fingerprint(profile: DatasetProfile) -> str:
    """Return a stable SHA-256 checksum of a profile's semantic content."""

    if not isinstance(profile, DatasetProfile):
        raise TypeError("profile must be a DatasetProfile instance")
    return _fingerprint_body(_profile_body(profile))


def load_json_profile(path: str | Path) -> DatasetProfile:
    """Load a UTF-8 profile with duplicate-key and nesting-depth protection."""

    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise InputError(f"dataset profile does not exist: {source}") from exc
    except UnicodeError as exc:
        raise InputError(f"dataset profile is not valid UTF-8: {source}") from exc
    except OSError as exc:
        raise InputError(f"cannot read dataset profile {source}: {exc}") from exc
    try:
        payload = json.loads(text, object_pairs_hook=_unique_object)
        validate_json_nesting(payload)
    except _DuplicateObjectKeyError as exc:
        raise InputError(f"{source}: invalid profile JSON: {exc}") from exc
    except (JsonNestingTooDeepError, RecursionError) as exc:
        raise InputError(f"{source}: profile JSON exceeds the maximum nesting depth") from exc
    except json.JSONDecodeError as exc:
        raise InputError(
            f"{source}: invalid profile JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise InputError(f"{source}: top-level profile JSON value must be an object")
    try:
        return profile_from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise InputError(f"{source}: invalid dataset profile: {exc}") from exc


def _validate_dataset_records(dataset: Dataset) -> None:
    expected = set(dataset.headers)
    for record in dataset.records:
        actual = set(record.values)
        if actual != expected:
            missing = sorted(expected - actual)
            unknown = sorted(actual - expected)
            details: list[str] = []
            if missing:
                details.append(f"missing fields {missing!r}")
            if unknown:
                details.append(f"unknown fields {unknown!r}")
            raise ValueError(
                f"dataset record at row {record.row_number} does not match headers: "
                + "; ".join(details)
            )
        for field in dataset.headers:
            value = record.values[field]
            if value is not None and not isinstance(value, str):
                raise TypeError(
                    f"dataset record at row {record.row_number} field {field!r} "
                    "must be a string or null"
                )


def _profile_field(dataset: Dataset, field: str, options: ProfileOptions) -> FieldProfile:
    values = tuple(record.values[field] for record in dataset.records)
    kinds = tuple(classify_value(value) for value in values)
    non_null_kinds = tuple(kind for kind in kinds if kind is not ValueKind.NULL)
    counts = Counter(kinds)
    non_null_values = tuple(value for value in values if value is not None)
    frequencies = Counter(non_null_values)
    ranked = sorted(frequencies.items(), key=lambda item: (-item[1], item[0]))
    lengths = tuple(len(value) for value in non_null_values)

    numeric_values: list[Decimal] = []
    if non_null_values and all(
        kind in {ValueKind.INTEGER, ValueKind.DECIMAL} for kind in non_null_kinds
    ):
        numeric_values = [Decimal(value) for value in non_null_values]
    date_values: list[date] = []
    if non_null_values and all(kind is ValueKind.DATE for kind in non_null_kinds):
        date_values = [date.fromisoformat(value) for value in non_null_values]

    return FieldProfile(
        name=field,
        total_count=len(values),
        null_count=counts[ValueKind.NULL],
        non_null_count=len(non_null_values),
        distinct_count=len(frequencies),
        kind_counts=tuple((kind, counts[kind]) for kind in _KIND_ORDER),
        min_length=min(lengths, default=None),
        max_length=max(lengths, default=None),
        numeric_range=(
            (_canonical_decimal(min(numeric_values)), _canonical_decimal(max(numeric_values)))
            if numeric_values
            else None
        ),
        date_range=(
            (min(date_values).isoformat(), max(date_values).isoformat()) if date_values else None
        ),
        top_values=tuple(
            ValueFrequency(value, count) for value, count in ranked[: options.top_values_limit]
        ),
        examples=tuple(sorted(frequencies)[: options.examples_limit]),
    )


def _profile_body(profile: DatasetProfile) -> dict[str, Any]:
    return {
        "source": profile.source,
        "record_count": profile.record_count,
        "options": {
            "top_values_limit": profile.options.top_values_limit,
            "examples_limit": profile.options.examples_limit,
        },
        "fields": [_field_to_dict(field) for field in profile.fields],
    }


def _field_to_dict(field: FieldProfile) -> dict[str, Any]:
    return {
        "name": field.name,
        "total_count": field.total_count,
        "null_count": field.null_count,
        "non_null_count": field.non_null_count,
        "distinct_count": field.distinct_count,
        "kind_counts": {kind.value: count for kind, count in field.kind_counts},
        "min_length": field.min_length,
        "max_length": field.max_length,
        "numeric_range": _range_to_dict(field.numeric_range),
        "date_range": _range_to_dict(field.date_range),
        "top_values": [
            {"value": frequency.value, "count": frequency.count} for frequency in field.top_values
        ],
        "examples": list(field.examples),
    }


def _field_from_dict(value: object, index: int) -> FieldProfile:
    location = f"profile.fields[{index}]"
    item = _exact_object(value, location, _FIELD_FIELDS)
    kind_object = _exact_object(
        item.get("kind_counts"),
        f"{location}.kind_counts",
        frozenset(kind.value for kind in _KIND_ORDER),
    )
    return FieldProfile(
        name=_string(item.get("name"), f"{location}.name"),
        total_count=_integer(item.get("total_count"), f"{location}.total_count"),
        null_count=_integer(item.get("null_count"), f"{location}.null_count"),
        non_null_count=_integer(item.get("non_null_count"), f"{location}.non_null_count"),
        distinct_count=_integer(item.get("distinct_count"), f"{location}.distinct_count"),
        kind_counts=tuple(
            (kind, _integer(kind_object.get(kind.value), f"{location}.kind_counts.{kind.value}"))
            for kind in _KIND_ORDER
        ),
        min_length=_optional_integer(item.get("min_length"), f"{location}.min_length"),
        max_length=_optional_integer(item.get("max_length"), f"{location}.max_length"),
        numeric_range=_range_from_dict(item.get("numeric_range"), f"{location}.numeric_range"),
        date_range=_range_from_dict(item.get("date_range"), f"{location}.date_range"),
        top_values=tuple(
            _frequency_from_dict(raw, frequency_index, location)
            for frequency_index, raw in enumerate(
                _array(item.get("top_values"), f"{location}.top_values")
            )
        ),
        examples=tuple(
            _string(raw, f"{location}.examples[{example_index}]")
            for example_index, raw in enumerate(
                _array(item.get("examples"), f"{location}.examples")
            )
        ),
    )


def _frequency_from_dict(value: object, index: int, field_location: str) -> ValueFrequency:
    location = f"{field_location}.top_values[{index}]"
    item = _exact_object(value, location, _TOP_VALUE_FIELDS)
    return ValueFrequency(
        value=_string(item.get("value"), f"{location}.value"),
        count=_integer(item.get("count"), f"{location}.count"),
    )


def _range_to_dict(value: tuple[str, str] | None) -> dict[str, str] | None:
    if value is None:
        return None
    return {"minimum": value[0], "maximum": value[1]}


def _range_from_dict(value: object, location: str) -> tuple[str, str] | None:
    if value is None:
        return None
    item = _exact_object(value, location, _RANGE_FIELDS)
    return (
        _string(item.get("minimum"), f"{location}.minimum"),
        _string(item.get("maximum"), f"{location}.maximum"),
    )


def _validate_optional_range(
    value: tuple[str, str] | None,
    *,
    label: str,
    applicable: bool,
    value_validator: object,
    order_key: object,
) -> None:
    if not applicable:
        if value is not None:
            raise ValueError(f"{label} is only valid for consistently typed fields")
        return
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError(f"{label} is required and must contain minimum and maximum")
    minimum, maximum = value
    if not isinstance(minimum, str) or not isinstance(maximum, str):
        raise TypeError(f"{label} bounds must be strings")
    validator = value_validator  # keep validation helpers explicit and type-checker-neutral
    key = order_key
    if not callable(validator) or not callable(key):
        raise AssertionError("range validators must be callable")
    validator(minimum)
    validator(maximum)
    if key(minimum) > key(maximum):
        raise ValueError(f"{label} minimum cannot be greater than maximum")


def _canonical_decimal(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    sign, raw_digits, raw_exponent = value.as_tuple()
    exponent = cast(int, raw_exponent)
    digits = list(raw_digits)
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


def _canonical_decimal_value(value: str) -> None:
    parsed = _decimal_value(value)
    if _canonical_decimal(parsed) != value:
        raise ValueError(f"numeric_range bound {value!r} is not canonical")


def _decimal_value(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"numeric range bound {value!r} is not a decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"numeric range bound {value!r} must be finite")
    return parsed


def _canonical_date_value(value: str) -> None:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"date_range bound {value!r} is not an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"date_range bound {value!r} is not canonical")


def _fingerprint_body(body: Mapping[str, object]) -> str:
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _bounded_limit(value: object, name: str) -> None:
    if type(value) is not int or not 0 <= value <= 1000:
        raise ValueError(f"{name} must be an integer between 0 and 1000")


def _non_negative_integer(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _integer(value: object, location: str) -> int:
    _non_negative_integer(value, location)
    return value  # type: ignore[return-value]


def _optional_integer(value: object, location: str) -> int | None:
    if value is None:
        return None
    return _integer(value, location)


def _string(value: object, location: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{location} must be a string")
    return value


def _array(value: object, location: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{location} must be an array")
    return value


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
    """Internal signal for an ambiguous JSON object."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateObjectKeyError(f"duplicate object key {key!r}")
        payload[key] = value
    return payload
