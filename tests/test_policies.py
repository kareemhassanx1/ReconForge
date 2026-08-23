from __future__ import annotations

from decimal import Decimal

import pytest

from reconforge.errors import ConfigurationError
from reconforge.model import Dataset, Record
from reconforge.policies import (
    ComparisonError,
    ComparisonPolicy,
    ComparisonPolicyKind,
    prepare_dataset_fields,
)
from reconforge.reconcile import ReconciliationConfig, reconcile


def make_dataset(source: str, rows: list[dict[str, str | None]]) -> Dataset:
    return Dataset(
        source=source,
        headers=("id", "amount", "settled_on", "customer"),
        records=tuple(Record(source, index, values) for index, values in enumerate(rows, start=2)),
    )


def row(
    identifier: str,
    amount: str | None = "10",
    settled_on: str | None = "2026-08-23",
    customer: str | None = "Northwind",
) -> dict[str, str | None]:
    return {
        "id": identifier,
        "amount": amount,
        "settled_on": settled_on,
        "customer": customer,
    }


def test_exact_comparison_remains_the_default() -> None:
    result = reconcile(
        make_dataset("left.csv", [row("A", amount="10.00")]),
        make_dataset("right.csv", [row("A", amount="10.0")]),
        ReconciliationConfig(key_fields=("id",)),
    )

    assert not result.is_clean
    assert result.differences[0].field == "amount"
    assert result.comparison_policies == ()


@pytest.mark.parametrize(
    ("right_amount", "is_clean"),
    [("10.010", True), ("9.990", True), ("10.0101", False), ("9.9899", False)],
)
def test_decimal_tolerance_has_inclusive_boundaries(
    right_amount: str,
    is_clean: bool,
) -> None:
    result = reconcile(
        make_dataset("left.csv", [row("A", amount="10.000")]),
        make_dataset("right.csv", [row("A", amount=right_amount)]),
        ReconciliationConfig(
            key_fields=("id",),
            comparison_policies={"amount": ComparisonPolicy.decimal("0.01")},
        ),
    )

    assert result.is_clean is is_clean


def test_decimal_policy_uses_decimal_arithmetic_not_binary_floats() -> None:
    policy = ComparisonPolicy.decimal(Decimal("0.1"))

    assert policy.values_match(policy.prepare("0.2"), policy.prepare("0.3"))


def test_date_policy_normalizes_supported_iso_representations() -> None:
    result = reconcile(
        make_dataset("left.csv", [row("A", settled_on="2026-08-23")]),
        make_dataset("right.csv", [row("A", settled_on="20260823")]),
        ReconciliationConfig(
            key_fields=("id",),
            comparison_policies={"settled_on": ComparisonPolicy.date()},
        ),
    )

    assert result.is_clean


def test_text_policy_is_unicode_aware_case_insensitive_and_whitespace_stable() -> None:
    result = reconcile(
        make_dataset("left.csv", [row("A", customer="  CAFÉ\tＳＴＯＲＥ ")]),
        make_dataset("right.csv", [row("A", customer="café store")]),
        ReconciliationConfig(
            key_fields=("id",),
            comparison_policies={"customer": ComparisonPolicy.text()},
        ),
    )

    assert result.is_clean


@pytest.mark.parametrize(
    ("left_value", "right_value", "is_clean"),
    [(None, None, True), (None, "0", False), ("0", None, False)],
)
def test_comparison_policies_preserve_null_semantics(
    left_value: str | None,
    right_value: str | None,
    is_clean: bool,
) -> None:
    result = reconcile(
        make_dataset("left.csv", [row("A", amount=left_value)]),
        make_dataset("right.csv", [row("A", amount=right_value)]),
        ReconciliationConfig(
            key_fields=("id",),
            comparison_policies={"amount": ComparisonPolicy.decimal("0.01")},
        ),
    )

    assert result.is_clean is is_clean


def test_named_policy_parses_case_and_decimal_tolerance() -> None:
    policy = ComparisonPolicy.named(" DECIMAL:0.0100 ")

    assert policy.kind is ComparisonPolicyKind.DECIMAL
    assert policy.tolerance == Decimal("0.0100")
    assert policy.descriptor == "decimal:tolerance=0.01"


@pytest.mark.parametrize("tolerance", ["-0.01", "NaN", "Infinity", "not-a-number"])
def test_decimal_tolerance_must_be_finite_and_non_negative(tolerance: str) -> None:
    with pytest.raises(ValueError, match="finite non-negative"):
        ComparisonPolicy.decimal(tolerance)


@pytest.mark.parametrize(
    "specification",
    ["date:YYYY-MM-DD", "text:casefold", "exact:anything"],
)
def test_non_decimal_policies_reject_options(specification: str) -> None:
    with pytest.raises(ValueError, match="does not accept an option"):
        ComparisonPolicy.named(specification)


def test_policy_validation_collects_bad_values_in_row_and_field_order() -> None:
    dataset = make_dataset(
        "ledger.csv",
        [
            row("A", amount="bad", settled_on="tomorrow"),
            row("B", amount="also-bad", settled_on="2026-08-23"),
        ],
    )
    normalized = {record.row_number: {} for record in dataset.records}

    with pytest.raises(ComparisonError) as captured:
        prepare_dataset_fields(
            dataset,
            {
                "settled_on": ComparisonPolicy.date(),
                "amount": ComparisonPolicy.decimal("0.01"),
            },
            normalized,
        )

    issues = captured.value.issues
    assert [(issue.row_number, issue.field) for issue in issues] == [
        (2, "amount"),
        (2, "settled_on"),
        (3, "amount"),
    ]
    assert "ledger.csv row 2 field 'amount' value 'bad'" in str(captured.value)


def test_invalid_policy_values_are_checked_on_unmatched_rows() -> None:
    left = make_dataset("left.csv", [row("A")])
    right = make_dataset("right.csv", [row("B", amount="not-money")])

    with pytest.raises(ComparisonError, match="right.csv row 2 field 'amount'"):
        reconcile(
            left,
            right,
            ReconciliationConfig(
                key_fields=("id",),
                comparison_policies={"amount": ComparisonPolicy.decimal("0.01")},
            ),
        )


def test_policy_cannot_target_an_ignored_field() -> None:
    with pytest.raises(ValueError, match="ignored fields cannot have comparison policies"):
        ReconciliationConfig(
            key_fields=("id",),
            ignore_fields=("customer",),
            comparison_policies={"customer": ComparisonPolicy.text()},
        )


def test_policy_must_target_a_compared_field() -> None:
    left = make_dataset("left.csv", [row("A")])
    right = make_dataset("right.csv", [row("A")])

    with pytest.raises(ConfigurationError, match=r"unused: \['customer'\]"):
        reconcile(
            left,
            right,
            ReconciliationConfig(
                key_fields=("id",),
                compare_fields=("amount",),
                comparison_policies={"customer": ComparisonPolicy.text()},
            ),
        )


def test_policy_cannot_target_a_key_field() -> None:
    left = make_dataset("left.csv", [row("A")])
    right = make_dataset("right.csv", [row("a")])

    with pytest.raises(ConfigurationError, match=r"unused: \['id'\]"):
        reconcile(
            left,
            right,
            ReconciliationConfig(
                key_fields=("id",),
                comparison_policies={"id": ComparisonPolicy.text()},
            ),
        )


def test_result_records_policies_in_stable_field_order() -> None:
    result = reconcile(
        make_dataset("left.csv", [row("A")]),
        make_dataset("right.csv", [row("A")]),
        ReconciliationConfig(
            key_fields=("id",),
            comparison_policies={
                "settled_on": ComparisonPolicy.date(),
                "amount": ComparisonPolicy.decimal("0.0100"),
            },
        ),
    )

    assert result.comparison_policies == (
        ("amount", "decimal:tolerance=0.01"),
        ("settled_on", "date"),
    )
