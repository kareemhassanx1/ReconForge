from __future__ import annotations

from decimal import Decimal

import pytest

from reconforge.errors import ConfigurationError
from reconforge.model import Dataset, Record
from reconforge.normalization import FieldNormalizer, normalize_dataset_fields
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.validation import (
    AllowedValuesRule,
    NumericRangeRule,
    RequiredRule,
    TextPatternRule,
    ValidationRuleKind,
    validate_dataset_fields,
)


def make_dataset(source: str, rows: list[dict[str, str | None]]) -> Dataset:
    return Dataset(
        source=source,
        headers=("id", "amount", "status", "reference", "note"),
        records=tuple(Record(source, index, values) for index, values in enumerate(rows, start=2)),
    )


def row(
    identifier: str,
    *,
    amount: str | None = "10",
    status: str | None = "paid",
    reference: str | None = "INV-100",
    note: str | None = "ready",
) -> dict[str, str | None]:
    return {
        "id": identifier,
        "amount": amount,
        "status": status,
        "reference": reference,
        "note": note,
    }


def test_required_rule_only_rejects_null_values() -> None:
    rule = RequiredRule()

    assert rule.kind is ValidationRuleKind.REQUIRED
    assert rule.check(None) == "value is required"
    assert rule.check("") is None
    assert rule.check("0") is None


def test_allowed_values_rule_is_exact_and_skips_null() -> None:
    rule = AllowedValuesRule(("paid", "pending"))

    assert rule.kind is ValidationRuleKind.ALLOWED
    assert rule.check(None) is None
    assert rule.check("paid") is None
    assert rule.check("Paid") == "expected one of: 'paid', 'pending'"
    assert rule.descriptor == 'allowed:["paid","pending"]'


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ((), "at least one value"),
        (("paid", "paid"), "must be unique"),
        (("paid", 1), "must be strings"),
        ("paid", "sequence of strings"),
    ],
)
def test_allowed_values_configuration_is_validated(value: object, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        AllowedValuesRule(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", None),
        ("10.00", None),
        ("-0.001", "expected a value within [0,10]"),
        ("10.001", "expected a value within [0,10]"),
        ("not-money", "expected a finite decimal number"),
        ("NaN", "expected a finite decimal number"),
        (None, None),
    ],
)
def test_numeric_range_has_inclusive_decimal_boundaries(
    value: str | None,
    expected: str | None,
) -> None:
    rule = NumericRangeRule("0", Decimal("10.000"))

    assert rule.check(value) == expected
    assert rule.descriptor == "numeric_range:[0,10]"


def test_numeric_range_supports_exclusive_and_open_bounds() -> None:
    greater_than_zero = NumericRangeRule(minimum="0", minimum_inclusive=False)
    below_ten = NumericRangeRule(maximum="10", maximum_inclusive=False)

    assert greater_than_zero.check("0") == "expected a value within (0,+inf)"
    assert greater_than_zero.check("0.0001") is None
    assert below_ten.check("10") == "expected a value within (-inf,10)"
    assert below_ten.check("9.999") is None


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "requires a minimum or maximum"),
        ({"minimum": "2", "maximum": "1"}, "cannot exceed"),
        (
            {"minimum": "1", "maximum": "1", "maximum_inclusive": False},
            "must both be inclusive",
        ),
        ({"minimum": "NaN"}, "finite decimal"),
        ({"maximum": "Infinity"}, "finite decimal"),
        ({"minimum": "0", "minimum_inclusive": "yes"}, "must be booleans"),
    ],
)
def test_invalid_numeric_ranges_are_rejected(
    arguments: dict[str, object],
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        NumericRangeRule(**arguments)  # type: ignore[arg-type]


def test_text_pattern_uses_full_match_and_skips_null() -> None:
    rule = TextPatternRule(r"INV-[0-9]{3}")

    assert rule.kind is ValidationRuleKind.PATTERN
    assert rule.check("INV-100") is None
    assert rule.check("prefix-INV-100") == ("expected text matching full pattern 'INV-[0-9]{3}'")
    assert rule.check(None) is None


@pytest.mark.parametrize("pattern", ["", "["])
def test_invalid_text_patterns_are_rejected(pattern: str) -> None:
    with pytest.raises(ValueError):
        TextPatternRule(pattern)


def test_dataset_validation_order_is_row_field_then_configured_rule() -> None:
    dataset = make_dataset(
        "ledger.csv",
        [
            row("A", amount="bad", status=None),
            row("B", amount="-1", status="unknown", reference="bad"),
        ],
    )
    normalized = {record.row_number: {} for record in dataset.records}

    violations = validate_dataset_fields(
        dataset,
        {
            "status": (RequiredRule(), AllowedValuesRule(("paid", "pending"))),
            "reference": (TextPatternRule(r"INV-[0-9]{3}"),),
            "amount": (NumericRangeRule(minimum="0"),),
        },
        normalized,
        side="left",
    )

    assert [(item.row_number, item.field, item.rule) for item in violations] == [
        (2, "amount", "numeric_range:[0,+inf)"),
        (2, "status", "required"),
        (3, "amount", "numeric_range:[0,+inf)"),
        (3, "reference", "pattern:INV-[0-9]{3}"),
        (3, "status", 'allowed:["paid","pending"]'),
    ]
    assert violations[0].source == "ledger.csv"
    assert violations[0].side == "left"
    assert violations[0].value == "bad"


def test_validation_uses_normalized_values_but_preserves_raw_values() -> None:
    dataset = make_dataset("left.csv", [row("A", status="  PAID ")])
    normalizers = {"status": FieldNormalizer.text()}
    normalized = normalize_dataset_fields(dataset, normalizers)

    violations = validate_dataset_fields(
        dataset,
        {"status": (AllowedValuesRule(("paid",)), TextPatternRule("pending"))},
        normalized,
        side="left",
    )

    assert len(violations) == 1
    assert violations[0].rule == "pattern:pending"
    assert violations[0].value == "  PAID "


def test_reconciliation_keeps_validation_separate_from_differences() -> None:
    left = make_dataset("left.csv", [row("A", amount="-1")])
    right = make_dataset("right.csv", [row("A", amount="-1")])

    result = reconcile(
        left,
        right,
        ReconciliationConfig(
            key_fields=("id",),
            validation_rules={"amount": (NumericRangeRule(minimum="0"),)},
        ),
    )

    assert not result.is_clean
    assert result.difference_count == 0
    assert result.validation_violation_count == 2
    assert result.issue_count == 2
    assert result.differences == ()
    assert [item.side for item in result.validation_violations] == ["left", "right"]


def test_reconciliation_can_report_differences_and_violations_together() -> None:
    result = reconcile(
        make_dataset("left.csv", [row("A", amount="-1")]),
        make_dataset("right.csv", [row("A", amount="2")]),
        ReconciliationConfig(
            key_fields=("id",),
            validation_rules={"amount": (NumericRangeRule(minimum="0"),)},
        ),
    )

    assert result.difference_count == 1
    assert result.validation_violation_count == 1
    assert result.issue_count == 2


def test_validation_checks_unmatched_rows_and_ignored_fields() -> None:
    result = reconcile(
        make_dataset("left.csv", [row("A", note=None)]),
        make_dataset("right.csv", [row("B", note=None)]),
        ReconciliationConfig(
            key_fields=("id",),
            ignore_fields=("note",),
            validation_rules={"note": (RequiredRule(),)},
        ),
    )

    assert result.validation_violation_count == 2
    assert result.difference_count == 2
    assert {item.side for item in result.validation_violations} == {"left", "right"}


def test_validation_rules_are_recorded_in_stable_field_order() -> None:
    result = reconcile(
        make_dataset("left.csv", [row("A")]),
        make_dataset("right.csv", [row("A")]),
        ReconciliationConfig(
            key_fields=("id",),
            validation_rules={
                "status": (RequiredRule(), AllowedValuesRule(("paid", "pending"))),
                "amount": (NumericRangeRule("0", "100"),),
            },
        ),
    )

    assert result.validation_rules == (
        ("amount", ("numeric_range:[0,100]",)),
        ("status", ("required", 'allowed:["paid","pending"]')),
    )


def test_validation_rule_field_must_exist_on_both_inputs() -> None:
    left = Dataset("left", ("id", "status"), (Record("left", 2, {"id": "A", "status": "paid"}),))
    right = Dataset("right", ("id",), (Record("right", 2, {"id": "A"}),))

    with pytest.raises(ConfigurationError, match="validation field 'status'.*right input"):
        reconcile(
            left,
            right,
            ReconciliationConfig(
                key_fields=("id",),
                validation_rules={"status": (RequiredRule(),)},
            ),
        )


@pytest.mark.parametrize(
    ("rules", "exception", "message"),
    [
        ({"amount": ()}, ValueError, "cannot be empty"),
        ({"amount": RequiredRule()}, TypeError, "provided as a sequence"),
        ({"amount": (object(),)}, TypeError, "unsupported rule instances"),
        ({"": (RequiredRule(),)}, ValueError, "field names cannot be empty"),
    ],
)
def test_reconciliation_config_rejects_invalid_rule_collections(
    rules: object,
    exception: type[Exception],
    message: str,
) -> None:
    with pytest.raises(exception, match=message):
        ReconciliationConfig(key_fields=("id",), validation_rules=rules)  # type: ignore[arg-type]
