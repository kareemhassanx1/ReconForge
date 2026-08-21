from __future__ import annotations

import pytest

from reconforge.errors import ConfigurationError
from reconforge.model import Dataset, Record
from reconforge.normalization import (
    FieldNormalizer,
    NormalizationError,
    NormalizerKind,
    normalize_dataset_fields,
)
from reconforge.reconcile import ReconciliationConfig, reconcile


@pytest.mark.parametrize(
    ("normalizer", "source", "expected"),
    [
        (FieldNormalizer.integer(), "+00042", "42"),
        (FieldNormalizer.integer(), "-0", "0"),
        (FieldNormalizer.decimal(), "001.2500", "1.25"),
        (FieldNormalizer.decimal(), "1.25E+3", "1.25E+3"),
        (FieldNormalizer.decimal(), "-0.000", "0"),
        (FieldNormalizer.date(), "2026-08-21", "2026-08-21"),
        (FieldNormalizer.text(), "  CAFÉ\tStore  ", "café store"),
        (FieldNormalizer.text(), "ＫＡＲＥＥＭ", "kareem"),
    ],
)
def test_builtin_normalizers_produce_canonical_values(
    normalizer: FieldNormalizer,
    source: str,
    expected: str,
) -> None:
    assert normalizer.normalize(source) == expected


@pytest.mark.parametrize(
    "normalizer",
    [
        FieldNormalizer.text(),
        FieldNormalizer.integer(),
        FieldNormalizer.decimal(),
        FieldNormalizer.date(),
    ],
)
def test_all_normalizers_preserve_null(normalizer: FieldNormalizer) -> None:
    assert normalizer.normalize(None) is None


@pytest.mark.parametrize(
    ("normalizer", "value", "message"),
    [
        (FieldNormalizer.integer(), "1.0", "base-10 integer"),
        (FieldNormalizer.integer(), "1_000", "base-10 integer"),
        (FieldNormalizer.decimal(), "NaN", "finite decimal"),
        (FieldNormalizer.decimal(), "Infinity", "finite decimal"),
        (FieldNormalizer.date(), "21/08/2026", "YYYY-MM-DD"),
        (FieldNormalizer.date(), "2026-02-30", "YYYY-MM-DD"),
    ],
)
def test_invalid_values_have_stable_type_specific_reasons(
    normalizer: FieldNormalizer,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        normalizer.normalize(value)


def test_named_normalizers_are_case_insensitive() -> None:
    assert FieldNormalizer.named(" DECIMAL ").kind is NormalizerKind.DECIMAL


def test_direct_normalizer_construction_resolves_string_kind() -> None:
    assert FieldNormalizer(" INTEGER ").kind is NormalizerKind.INTEGER


def test_unknown_normalizer_lists_supported_types() -> None:
    with pytest.raises(
        ValueError,
        match="expected one of: text, integer, decimal, date",
    ):
        FieldNormalizer.named("money")


def test_dataset_normalization_collects_issues_in_row_and_field_order() -> None:
    source = Dataset(
        source="ledger.csv",
        headers=("id", "amount", "settled_on"),
        records=(
            Record(
                "ledger.csv",
                2,
                {"id": "A", "amount": "bad", "settled_on": "tomorrow"},
            ),
            Record(
                "ledger.csv",
                3,
                {"id": "B", "amount": "also-bad", "settled_on": "2026-08-21"},
            ),
        ),
    )

    with pytest.raises(NormalizationError) as captured:
        normalize_dataset_fields(
            source,
            {
                "settled_on": FieldNormalizer.date(),
                "amount": FieldNormalizer.decimal(),
            },
        )

    issues = captured.value.issues
    assert [(issue.row_number, issue.field) for issue in issues] == [
        (2, "amount"),
        (2, "settled_on"),
        (3, "amount"),
    ]
    assert "ledger.csv row 2 field 'amount' value 'bad' as decimal" in str(captured.value)


def make_dataset(source: str, rows: list[dict[str, str | None]]) -> Dataset:
    return Dataset(
        source=source,
        headers=("id", "amount", "settled_on", "customer"),
        records=tuple(Record(source, index, values) for index, values in enumerate(rows, start=2)),
    )


def test_reconciliation_compares_normalized_values_but_preserves_raw_records() -> None:
    left = make_dataset(
        "left.csv",
        [
            {
                "id": "0007",
                "amount": "12.500",
                "settled_on": "2026-08-21",
                "customer": "CAFÉ   STORE",
            }
        ],
    )
    right = make_dataset(
        "right.csv",
        [
            {
                "id": "7",
                "amount": "12.5",
                "settled_on": "2026-08-21",
                "customer": "café store",
            }
        ],
    )
    config = ReconciliationConfig(
        key_fields=("id",),
        field_normalizers={
            "id": FieldNormalizer.integer(),
            "amount": FieldNormalizer.decimal(),
            "settled_on": FieldNormalizer.date(),
            "customer": FieldNormalizer.text(),
        },
    )

    result = reconcile(left, right, config)

    assert result.is_clean
    assert result.matched_count == 1
    assert left.records[0].value("amount") == "12.500"
    assert result.field_normalizers == (
        ("amount", "decimal"),
        ("customer", "text"),
        ("id", "integer"),
        ("settled_on", "date"),
    )


def test_invalid_values_are_checked_even_when_their_keys_would_not_match() -> None:
    left = make_dataset(
        "left.csv",
        [
            {
                "id": "A",
                "amount": "10",
                "settled_on": "2026-08-21",
                "customer": "One",
            }
        ],
    )
    right = make_dataset(
        "right.csv",
        [
            {
                "id": "B",
                "amount": "not-a-number",
                "settled_on": "2026-08-21",
                "customer": "Two",
            }
        ],
    )

    with pytest.raises(NormalizationError, match="right.csv row 2 field 'amount'"):
        reconcile(
            left,
            right,
            ReconciliationConfig(
                key_fields=("id",),
                field_normalizers={"amount": FieldNormalizer.decimal()},
            ),
        )


def test_normalizer_cannot_target_an_ignored_field() -> None:
    with pytest.raises(ValueError, match="ignored fields cannot have normalizers"):
        ReconciliationConfig(
            key_fields=("id",),
            ignore_fields=("notes",),
            field_normalizers={"notes": FieldNormalizer.text()},
        )


def test_normalizer_must_target_a_key_or_compared_field() -> None:
    left = make_dataset(
        "left",
        [
            {
                "id": "A",
                "amount": "10",
                "settled_on": "2026-08-21",
                "customer": "One",
            }
        ],
    )
    right = make_dataset(
        "right",
        [
            {
                "id": "A",
                "amount": "10",
                "settled_on": "2026-08-21",
                "customer": "One",
            }
        ],
    )

    with pytest.raises(ConfigurationError, match=r"unused: \['customer'\]"):
        reconcile(
            left,
            right,
            ReconciliationConfig(
                key_fields=("id",),
                compare_fields=("amount",),
                field_normalizers={"customer": FieldNormalizer.text()},
            ),
        )
