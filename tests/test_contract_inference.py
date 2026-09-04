from __future__ import annotations

from decimal import Decimal

import pytest

from reconforge.contract_inference import (
    ContractInferenceOptions,
    infer_contract_from_profile,
    infer_dataset_contract,
)
from reconforge.model import Dataset, Record
from reconforge.profiling import ProfileOptions, profile_dataset
from reconforge.schema_contract import ContractValueType, validate_dataset_contract


def make_dataset() -> Dataset:
    source = "ledger.csv"
    return Dataset(
        source,
        ("id", "amount", "status", "note", "empty"),
        (
            Record(
                source,
                2,
                {
                    "id": "001",
                    "amount": "10.50",
                    "status": "paid",
                    "note": "ok",
                    "empty": None,
                },
            ),
            Record(
                source,
                3,
                {
                    "id": "002",
                    "amount": "12",
                    "status": "pending",
                    "note": "review",
                    "empty": None,
                },
            ),
            Record(
                source,
                4,
                {
                    "id": "003",
                    "amount": None,
                    "status": "paid",
                    "note": "longer",
                    "empty": None,
                },
            ),
        ),
    )


def test_default_inference_is_conservative_and_self_validating() -> None:
    dataset = make_dataset()

    contract = infer_dataset_contract(dataset)

    assert contract.allow_extra_fields is False
    assert contract.field("id").allowed_types == (ContractValueType.INTEGER,)
    assert contract.field("id").required is True
    assert contract.field("id").nullable is False
    assert contract.field("id").unique is False
    assert contract.field("amount").allowed_types == (
        ContractValueType.INTEGER,
        ContractValueType.DECIMAL,
    )
    assert contract.field("amount").nullable is True
    assert contract.field("amount").minimum is None
    assert contract.field("status").allowed_values is None
    assert contract.field("note").min_length is None
    assert contract.field("empty").allowed_types == (ContractValueType.TEXT,)
    assert validate_dataset_contract(dataset, contract).is_valid


def test_opt_in_constraints_capture_observed_properties() -> None:
    dataset = make_dataset()
    options = ContractInferenceOptions(
        allow_extra_fields=True,
        required_fields=False,
        infer_uniqueness=True,
        infer_numeric_bounds=True,
        infer_text_lengths=True,
        allowed_values_limit=3,
    )

    contract = infer_dataset_contract(dataset, options)

    identifier = contract.field("id")
    assert identifier.required is False
    assert identifier.unique is True
    assert identifier.minimum == Decimal("1")
    assert identifier.maximum == Decimal("3")
    assert identifier.allowed_values == ("001", "002", "003")
    amount = contract.field("amount")
    assert amount.minimum == Decimal("10.5")
    assert amount.maximum == Decimal("12")
    assert amount.unique is True
    status = contract.field("status")
    assert status.allowed_values == ("paid", "pending")
    note = contract.field("note")
    assert (note.min_length, note.max_length) == (2, 6)
    assert contract.allow_extra_fields is True


def test_inference_from_a_truncated_profile_avoids_incomplete_allowed_values() -> None:
    profile = profile_dataset(
        make_dataset(),
        ProfileOptions(top_values_limit=1, examples_limit=0),
    )

    contract = infer_contract_from_profile(
        profile,
        ContractInferenceOptions(allowed_values_limit=3),
    )

    assert contract.field("status").allowed_values is None


def test_disabling_nullability_inference_keeps_contract_permissive() -> None:
    contract = infer_dataset_contract(
        make_dataset(),
        ContractInferenceOptions(infer_nullability=False),
    )

    assert all(field.nullable for field in contract.fields.values())


def test_empty_dataset_does_not_infer_non_nullability_without_evidence() -> None:
    dataset = Dataset("empty.csv", ("id", "value"), ())

    contract = infer_dataset_contract(dataset)

    assert all(field.nullable for field in contract.fields.values())
    assert validate_dataset_contract(dataset, contract).is_valid


@pytest.mark.parametrize(
    "changes",
    [
        {"allow_extra_fields": 1},
        {"required_fields": None},
        {"infer_nullability": "yes"},
        {"infer_uniqueness": 1},
        {"infer_numeric_bounds": 0},
        {"infer_text_lengths": []},
    ],
)
def test_inference_options_require_real_booleans(changes: dict[str, object]) -> None:
    with pytest.raises(TypeError, match="must be a boolean"):
        ContractInferenceOptions(**changes)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-1, 1001])
def test_allowed_values_limit_is_bounded(value: int) -> None:
    with pytest.raises(ValueError, match="between 0 and 1000"):
        ContractInferenceOptions(allowed_values_limit=value)


def test_allowed_values_limit_rejects_booleans_and_nonintegers() -> None:
    for value in (True, 1.5, "2"):
        with pytest.raises(TypeError, match="must be an integer"):
            ContractInferenceOptions(allowed_values_limit=value)  # type: ignore[arg-type]


def test_public_inference_functions_validate_inputs() -> None:
    dataset = make_dataset()
    profile = profile_dataset(dataset)

    with pytest.raises(TypeError, match="dataset must be"):
        infer_dataset_contract(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="options must be"):
        infer_dataset_contract(dataset, object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="profile must be"):
        infer_contract_from_profile(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="options must be"):
        infer_contract_from_profile(profile, object())  # type: ignore[arg-type]
