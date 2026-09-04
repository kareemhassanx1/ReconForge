from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from reconforge.contract_codec import (
    CONTRACT_VERSION,
    contract_from_dict,
    contract_to_dict,
    load_json_contract,
    write_json_contract,
)
from reconforge.errors import ConfigurationError, InputError
from reconforge.schema_contract import ContractValueType, DatasetContract, FieldContract


def make_contract() -> DatasetContract:
    return DatasetContract(
        fields={
            "amount": FieldContract(
                required=True,
                nullable=False,
                unique=False,
                allowed_types=(ContractValueType.INTEGER, ContractValueType.DECIMAL),
                allowed_values=("0", "1.25", "10"),
                minimum=Decimal("0.00"),
                maximum="10.000",
            ),
            "code": FieldContract(
                required=False,
                nullable=True,
                unique=True,
                allowed_types=(ContractValueType.TEXT,),
                allowed_values=("abc", "xyz"),
                pattern="[a-z]{3}",
                min_length=3,
                max_length=3,
            ),
        },
        allow_extra_fields=True,
    )


def test_contract_codec_round_trips_complete_canonical_model() -> None:
    contract = make_contract()

    payload = contract_to_dict(contract)
    restored = contract_from_dict(payload)

    assert restored == contract
    assert payload == {
        "contract_version": CONTRACT_VERSION,
        "allow_extra_fields": True,
        "fields": {
            "amount": {
                "required": True,
                "nullable": False,
                "unique": False,
                "types": ["integer", "decimal"],
                "allowed_values": ["0", "1.25", "10"],
                "pattern": None,
                "min_length": None,
                "max_length": None,
                "minimum": "0",
                "maximum": "10",
            },
            "code": {
                "required": False,
                "nullable": True,
                "unique": True,
                "types": ["text"],
                "allowed_values": ["abc", "xyz"],
                "pattern": "[a-z]{3}",
                "min_length": 3,
                "max_length": 3,
                "minimum": None,
                "maximum": None,
            },
        },
    }


def test_contract_to_dict_returns_detached_mutable_containers() -> None:
    contract = make_contract()
    payload = contract_to_dict(contract)

    payload["fields"]["code"]["types"].append("date")
    payload["fields"]["amount"]["allowed_values"].clear()

    assert contract.field("code").allowed_types == (ContractValueType.TEXT,)
    assert contract.field("amount").allowed_values == ("0", "1.25", "10")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.pop("fields"), "missing required field.*fields"),
        (lambda value: value.update(extra=True), "unknown field.*extra"),
        (lambda value: value.update(contract_version=2), "unsupported contract version 2"),
        (lambda value: value.update(contract_version=True), "unsupported contract version True"),
        (
            lambda value: value["fields"]["code"].pop("nullable"),
            r"code.*missing required field.*nullable",
        ),
        (
            lambda value: value["fields"]["code"].update(description="x"),
            r"code.*unknown field.*description",
        ),
        (
            lambda value: value["fields"]["code"].update(required=1),
            r"code.*required must be a boolean",
        ),
        (
            lambda value: value["fields"]["code"].update(min_length=True),
            r"code.*min_length must be a non-negative integer",
        ),
        (
            lambda value: value["fields"]["code"].update(types="text"),
            r"code.*types must be an array",
        ),
        (
            lambda value: value["fields"]["code"].update(types=["TEXT"]),
            r"types\[0\] must use canonical value 'text'",
        ),
        (
            lambda value: value["fields"]["code"].update(types=["text", "text"]),
            "allowed_types must be unique",
        ),
        (
            lambda value: value["fields"]["amount"].update(types=["decimal", "integer"]),
            "types must use canonical order",
        ),
        (
            lambda value: value["fields"]["code"].update(allowed_values=["xyz", "abc"]),
            "allowed_values must be sorted",
        ),
    ],
)
def test_contract_from_dict_rejects_structural_type_and_canonical_errors(
    mutate,
    message: str,
) -> None:
    payload = contract_to_dict(make_contract())
    mutate(payload)

    with pytest.raises(ConfigurationError, match=message):
        contract_from_dict(payload)


@pytest.mark.parametrize(
    "bound",
    ["1.0", "01", "+1", "1e3", "1E+3", "-0", "Infinity", "NaN", 1, True],
)
def test_codec_rejects_noncanonical_or_non_string_decimal_bounds(bound: object) -> None:
    payload = contract_to_dict(make_contract())
    payload["fields"]["amount"]["maximum"] = bound

    with pytest.raises(ConfigurationError, match="maximum.*canonical|maximum must be a string"):
        contract_from_dict(payload)


def test_codec_accepts_bounded_scientific_form_for_extreme_decimal() -> None:
    payload = contract_to_dict(make_contract())
    payload["fields"]["amount"]["maximum"] = "1E+101"

    contract = contract_from_dict(payload)

    assert contract.field("amount").maximum == Decimal("1E+101")
    assert contract_to_dict(contract)["fields"]["amount"]["maximum"] == "1E+101"


def test_json_loader_accepts_bom_and_object_key_reordering(tmp_path: Path) -> None:
    payload = contract_to_dict(make_contract())
    source = tmp_path / "contract.json"
    source.write_text(
        json.dumps(dict(reversed(tuple(payload.items()))), ensure_ascii=False),
        encoding="utf-8-sig",
    )

    restored = load_json_contract(source)

    assert restored == make_contract()


@pytest.mark.parametrize(
    "text",
    [
        '{"contract_version":1,"contract_version":1}',
        (
            '{"contract_version":1,"allow_extra_fields":false,"fields":{"id":'
            '{"required":true,"required":true}}}'
        ),
    ],
)
def test_json_loader_rejects_duplicate_keys_at_every_depth(tmp_path: Path, text: str) -> None:
    source = tmp_path / "duplicate.json"
    source.write_text(text, encoding="utf-8")

    with pytest.raises(InputError, match="duplicate object key"):
        load_json_contract(source)


def test_json_loader_has_clean_file_and_syntax_diagnostics(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="does not exist"):
        load_json_contract(tmp_path / "missing.json")

    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"contract_version":', encoding="utf-8")
    with pytest.raises(InputError, match=r"line 1, column \d+"):
        load_json_contract(malformed)

    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xff\xfe\xfd")
    with pytest.raises(InputError, match="not valid UTF-8"):
        load_json_contract(binary)


def test_json_loader_rejects_non_object_nonfinite_and_excess_depth(tmp_path: Path) -> None:
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(InputError, match="top-level.*must be an object"):
        load_json_contract(array)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"contract_version": NaN}', encoding="utf-8")
    with pytest.raises(InputError, match="non-finite JSON number"):
        load_json_contract(nonfinite)

    deep = tmp_path / "deep.json"
    deep.write_text("[" * 101 + "0" + "]" * 101, encoding="utf-8")
    with pytest.raises(InputError, match="maximum nesting depth"):
        load_json_contract(deep)


def test_contract_from_dict_checks_nesting_before_schema() -> None:
    nested: object = 0
    for _ in range(101):
        nested = [nested]

    with pytest.raises(ConfigurationError, match="maximum nesting depth"):
        contract_from_dict({"unexpected": nested})


def test_json_loader_preserves_configuration_error_for_semantic_failures(
    tmp_path: Path,
) -> None:
    payload = contract_to_dict(make_contract())
    payload["allow_extra_fields"] = 1
    source = tmp_path / "semantic.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigurationError, match=r"semantic\.json.*must be a boolean"):
        load_json_contract(source)


def test_write_json_contract_is_atomic_utf8_and_has_terminal_newline(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "contract.json"

    returned = write_json_contract(make_contract(), destination)

    text = destination.read_text(encoding="utf-8")
    assert returned == destination.resolve()
    assert text.endswith("\n")
    assert json.loads(text) == contract_to_dict(make_contract())
    assert list(destination.parent.glob(f".{destination.name}.*.tmp")) == []


def test_codec_rejects_wrong_public_argument_types() -> None:
    with pytest.raises(TypeError, match="DatasetContract"):
        contract_to_dict({})  # type: ignore[arg-type]

    with pytest.raises(ConfigurationError, match="must be an object"):
        contract_from_dict([])  # type: ignore[arg-type]


def test_payload_mutations_do_not_affect_subsequent_parsing() -> None:
    payload = contract_to_dict(make_contract())
    original = deepcopy(payload)

    contract_from_dict(payload)

    assert payload == original
