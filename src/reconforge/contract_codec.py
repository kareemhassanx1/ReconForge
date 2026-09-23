"""Strict versioned JSON codec for :mod:`reconforge.schema_contract`."""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from reconforge.atomic_io import atomic_write_text
from reconforge.errors import ConfigurationError, InputError
from reconforge.json_safety import JsonNestingTooDeepError, validate_json_nesting
from reconforge.schema_contract import (
    ContractValueType,
    DatasetContract,
    FieldContract,
    format_decimal,
)

CONTRACT_VERSION = 1

_CONTRACT_FIELDS = frozenset({"contract_version", "allow_extra_fields", "fields"})
_FIELD_CONTRACT_FIELDS = frozenset(
    {
        "required",
        "nullable",
        "unique",
        "types",
        "allowed_values",
        "pattern",
        "min_length",
        "max_length",
        "minimum",
        "maximum",
    }
)


def contract_to_dict(contract: DatasetContract) -> dict[str, Any]:
    """Serialize ``contract`` into the exact canonical version-one schema."""

    if not isinstance(contract, DatasetContract):
        raise TypeError("contract must be a DatasetContract instance")
    return {
        "contract_version": CONTRACT_VERSION,
        "allow_extra_fields": contract.allow_extra_fields,
        "fields": {
            name: _field_to_dict(field_contract) for name, field_contract in contract.fields.items()
        },
    }


def contract_from_dict(payload: Mapping[str, object]) -> DatasetContract:
    """Parse and validate an exact version-one contract mapping.

    Semantic and structural definition errors use :class:`ConfigurationError`.
    File access and JSON syntax errors are reserved for :func:`load_json_contract`.
    """

    try:
        validate_json_nesting(payload)
        return _contract_from_dict(payload)
    except JsonNestingTooDeepError as exc:
        raise ConfigurationError("dataset contract exceeds the maximum nesting depth") from exc
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid dataset contract: {exc}") from exc


def load_json_contract(path: str | Path) -> DatasetContract:
    """Load a UTF-8 contract with duplicate-key and nesting protection."""

    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise InputError(f"dataset contract does not exist: {source}") from exc
    except UnicodeError as exc:
        raise InputError(f"dataset contract is not valid UTF-8: {source}") from exc
    except OSError as exc:
        raise InputError(f"cannot read dataset contract {source}: {exc}") from exc

    try:
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        validate_json_nesting(payload)
    except _DuplicateObjectKeyError as exc:
        raise InputError(f"{source}: invalid contract JSON: {exc}") from exc
    except _NonFiniteJsonNumberError as exc:
        raise InputError(f"{source}: invalid contract JSON: {exc}") from exc
    except (JsonNestingTooDeepError, RecursionError) as exc:
        raise InputError(f"{source}: contract JSON exceeds the maximum nesting depth") from exc
    except json.JSONDecodeError as exc:
        raise InputError(
            f"{source}: invalid contract JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise InputError(f"{source}: top-level contract JSON value must be an object")
    try:
        return contract_from_dict(payload)
    except ConfigurationError as exc:
        raise ConfigurationError(f"{source}: {exc}") from exc


def write_json_contract(contract: DatasetContract, destination: str | Path) -> Path:
    """Write canonical pretty JSON atomically and return the resolved path."""

    payload = json.dumps(
        contract_to_dict(contract),
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )
    return atomic_write_text(destination, payload + "\n")


def _contract_from_dict(payload: Mapping[str, object]) -> DatasetContract:
    root = _exact_object(payload, "contract", _CONTRACT_FIELDS)
    version = root["contract_version"]
    if type(version) is not int or version != CONTRACT_VERSION:
        raise ValueError(f"unsupported contract version {version!r}; expected {CONTRACT_VERSION}")
    allow_extra_fields = _boolean(
        root["allow_extra_fields"],
        "contract.allow_extra_fields",
    )
    raw_fields = _object(root["fields"], "contract.fields")
    if not raw_fields:
        raise ValueError("contract.fields must contain at least one field")
    fields: dict[str, FieldContract] = {}
    for raw_name in sorted(raw_fields):
        name = _canonical_text(raw_name, "contract.fields field name")
        fields[name] = _field_from_dict(raw_fields[raw_name], name)
    return DatasetContract(fields=fields, allow_extra_fields=allow_extra_fields)


def _field_to_dict(contract: FieldContract) -> dict[str, Any]:
    return {
        "required": contract.required,
        "nullable": contract.nullable,
        "unique": contract.unique,
        "types": [item.value for item in contract.allowed_types],
        "allowed_values": (
            None if contract.allowed_values is None else list(contract.allowed_values)
        ),
        "pattern": contract.pattern,
        "min_length": contract.min_length,
        "max_length": contract.max_length,
        "minimum": None
        if contract.minimum is None
        else format_decimal(cast(Decimal, contract.minimum)),
        "maximum": None
        if contract.maximum is None
        else format_decimal(cast(Decimal, contract.maximum)),
    }


def _field_from_dict(value: object, name: str) -> FieldContract:
    location = f"contract.fields[{name!r}]"
    payload = _exact_object(value, location, _FIELD_CONTRACT_FIELDS)
    raw_types = _array(payload["types"], f"{location}.types")
    if not raw_types:
        raise ValueError(f"{location}.types must contain at least one value type")
    types: list[ContractValueType] = []
    for index, raw_type in enumerate(raw_types):
        item_location = f"{location}.types[{index}]"
        text = _string(raw_type, item_location)
        try:
            parsed = ContractValueType.named(text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{item_location} is invalid: {exc}") from exc
        if text != parsed.value:
            raise ValueError(f"{item_location} must use canonical value {parsed.value!r}")
        types.append(parsed)

    allowed_values = _optional_string_array(
        payload["allowed_values"],
        f"{location}.allowed_values",
    )
    pattern = _optional_string(payload["pattern"], f"{location}.pattern")
    minimum = _canonical_decimal_bound(payload["minimum"], f"{location}.minimum")
    maximum = _canonical_decimal_bound(payload["maximum"], f"{location}.maximum")
    try:
        parsed_contract = FieldContract(
            required=_boolean(payload["required"], f"{location}.required"),
            nullable=_boolean(payload["nullable"], f"{location}.nullable"),
            unique=_boolean(payload["unique"], f"{location}.unique"),
            allowed_types=tuple(types),
            allowed_values=allowed_values,
            pattern=pattern,
            min_length=_optional_non_negative_integer(
                payload["min_length"], f"{location}.min_length"
            ),
            max_length=_optional_non_negative_integer(
                payload["max_length"], f"{location}.max_length"
            ),
            minimum=minimum,
            maximum=maximum,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{location} is contradictory or invalid: {exc}") from exc
    if tuple(types) != parsed_contract.allowed_types:
        canonical_types = [item.value for item in parsed_contract.allowed_types]
        raise ValueError(f"{location}.types must use canonical order: {canonical_types!r}")
    if allowed_values is not None and allowed_values != parsed_contract.allowed_values:
        raise ValueError(f"{location}.allowed_values must be sorted in code-point order")
    return parsed_contract


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


def _array(value: object, location: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{location} must be an array")
    return value


def _optional_string_array(value: object, location: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    values = _array(value, location)
    result = tuple(_string(item, f"{location}[{index}]") for index, item in enumerate(values))
    if not result:
        raise ValueError(f"{location} must contain at least one value when provided")
    return result


def _canonical_decimal_bound(value: object, location: str) -> Decimal | None:
    if value is None:
        return None
    text = _string(value, location)
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{location} must be a finite decimal") from exc
    if not parsed.is_finite() or format_decimal(parsed) != text:
        raise ValueError(
            f"{location} must be a canonical finite decimal string; "
            "ordinary magnitudes use fixed point and extreme magnitudes use uppercase E"
        )
    return parsed


def _optional_non_negative_integer(value: object, location: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{location} must be a non-negative integer or null")
    return value


def _boolean(value: object, location: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{location} must be a boolean")
    return value


def _string(value: object, location: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{location} must be a string")
    return value


def _optional_string(value: object, location: str) -> str | None:
    if value is None:
        return None
    return _string(value, location)


def _canonical_text(value: object, location: str) -> str:
    text = _string(value, location)
    if not text or text != text.strip():
        raise ValueError(f"{location} must be non-empty without surrounding whitespace")
    return text


class _DuplicateObjectKeyError(ValueError):
    """Internal signal for an ambiguous JSON object."""


class _NonFiniteJsonNumberError(ValueError):
    """Internal signal for non-standard NaN and infinity literals."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateObjectKeyError(f"duplicate object key {key!r}")
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> object:
    raise _NonFiniteJsonNumberError(f"non-finite JSON number {value!r} is not supported")
