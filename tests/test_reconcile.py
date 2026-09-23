from __future__ import annotations

import pytest

from reconforge.model import Dataset, Record
from reconforge.reconcile import ReconciliationConfig, reconcile


def dataset(
    source: str,
    headers: tuple[str, ...],
    rows: list[dict[str, str | None]],
) -> Dataset:
    return Dataset(
        source=source,
        headers=headers,
        records=tuple(
            Record(source=source, row_number=index, values=row)
            for index, row in enumerate(rows, start=2)
        ),
    )


def test_equal_datasets_reconcile_cleanly() -> None:
    left = dataset("left", ("id", "amount"), [{"id": "A", "amount": "10"}])
    right = dataset("right", ("id", "amount"), [{"id": "A", "amount": "10"}])

    result = reconcile(left, right, ReconciliationConfig(key_fields=("id",)))

    assert result.is_clean
    assert result.matched_count == 1
    assert result.difference_count == 0
    assert result.compared_fields == ("amount",)


def test_reports_each_field_level_difference() -> None:
    left = dataset(
        "left",
        ("id", "amount", "status"),
        [{"id": "A", "amount": "10", "status": "paid"}],
    )
    right = dataset(
        "right",
        ("id", "amount", "status"),
        [{"id": "A", "amount": "12", "status": "pending"}],
    )

    result = reconcile(left, right, ReconciliationConfig(key_fields=("id",)))

    assert not result.is_clean
    assert result.difference_count == 2
    assert [item.field for item in result.differences] == ["amount", "status"]
    assert result.differences[0].left_row == 2
    assert result.differences[0].right_row == 2


def test_reports_records_missing_from_each_side() -> None:
    left = dataset(
        "left",
        ("id", "amount"),
        [{"id": "A", "amount": "10"}, {"id": "B", "amount": "20"}],
    )
    right = dataset(
        "right",
        ("id", "amount"),
        [{"id": "A", "amount": "10"}, {"id": "C", "amount": "30"}],
    )

    result = reconcile(left, right, ReconciliationConfig(key_fields=("id",)))

    assert [(item.key, item.row_number) for item in result.missing_from_right] == [(("B",), 3)]
    assert [(item.key, item.row_number) for item in result.missing_from_left] == [(("C",), 3)]


def test_duplicate_keys_are_reported_and_not_compared_arbitrarily() -> None:
    left = dataset(
        "left",
        ("id", "amount"),
        [{"id": "A", "amount": "10"}, {"id": "A", "amount": "11"}],
    )
    right = dataset("right", ("id", "amount"), [{"id": "A", "amount": "10"}])

    result = reconcile(left, right, ReconciliationConfig(key_fields=("id",)))

    assert result.matched_count == 0
    assert result.duplicate_left[0].key == ("A",)
    assert result.duplicate_left[0].row_numbers == (2, 3)
    assert result.differences == ()


def test_composite_keys_match_records() -> None:
    headers = ("account", "period", "balance")
    left = dataset(
        "left",
        headers,
        [{"account": "100", "period": "2026-08", "balance": "45"}],
    )
    right = dataset(
        "right",
        headers,
        [{"account": "100", "period": "2026-08", "balance": "46"}],
    )
    config = ReconciliationConfig(key_fields=("account", "period"))

    result = reconcile(left, right, config)

    assert result.differences[0].key == ("100", "2026-08")


def test_key_matching_can_ignore_case() -> None:
    left = dataset("left", ("id", "value"), [{"id": "Invoice-A", "value": "10"}])
    right = dataset("right", ("id", "value"), [{"id": "invoice-a", "value": "10"}])
    config = ReconciliationConfig(key_fields=("id",), case_sensitive_keys=False)

    result = reconcile(left, right, config)

    assert result.is_clean
    assert result.matched_count == 1


def test_selected_comparison_fields_limit_value_checks() -> None:
    headers = ("id", "amount", "updated_at")
    left = dataset(
        "left",
        headers,
        [{"id": "A", "amount": "10", "updated_at": "yesterday"}],
    )
    right = dataset(
        "right",
        headers,
        [{"id": "A", "amount": "10", "updated_at": "today"}],
    )
    config = ReconciliationConfig(key_fields=("id",), compare_fields=("amount",))

    result = reconcile(left, right, config)

    assert result.is_clean
    assert result.compared_fields == ("amount",)


def test_ignored_fields_do_not_create_differences() -> None:
    headers = ("id", "amount", "exported_at")
    left = dataset(
        "left",
        headers,
        [{"id": "A", "amount": "10", "exported_at": "08:00"}],
    )
    right = dataset(
        "right",
        headers,
        [{"id": "A", "amount": "10", "exported_at": "09:00"}],
    )
    config = ReconciliationConfig(key_fields=("id",), ignore_fields=("exported_at",))

    result = reconcile(left, right, config)

    assert result.is_clean
    assert result.compared_fields == ("amount",)


def test_schema_drift_is_reported() -> None:
    left = dataset("left", ("id", "legacy"), [{"id": "A", "legacy": "x"}])
    right = dataset("right", ("id", "modern"), [{"id": "A", "modern": "x"}])

    result = reconcile(left, right, ReconciliationConfig(key_fields=("id",)))

    assert result.fields_only_left == ("legacy",)
    assert result.fields_only_right == ("modern",)
    assert not result.is_clean


def test_ignored_one_sided_schema_field_does_not_dirty_result() -> None:
    left = dataset("left", ("id", "legacy"), [{"id": "A", "legacy": "x"}])
    right = dataset("right", ("id",), [{"id": "A"}])
    config = ReconciliationConfig(key_fields=("id",), ignore_fields=("legacy",))

    result = reconcile(left, right, config)

    assert result.is_clean


def test_missing_key_field_has_clear_configuration_error() -> None:
    left = dataset("left", ("id",), [{"id": "A"}])
    right = dataset("right", ("other",), [{"other": "A"}])

    try:
        reconcile(left, right, ReconciliationConfig(key_fields=("id",)))
    except Exception as exc:
        assert type(exc).__name__ == "ConfigurationError"
        assert "right input right" in str(exc)
    else:
        raise AssertionError("expected reconciliation to reject the missing key field")


def test_key_fields_must_be_unique() -> None:
    try:
        ReconciliationConfig(key_fields=("id", "id"))
    except ValueError as exc:
        assert "must be unique" in str(exc)
    else:
        raise AssertionError("expected duplicate key fields to be rejected")


def test_dataset_rejects_aliased_source_rows_before_values_can_be_overwritten() -> None:
    with pytest.raises(ValueError, match="row numbers must be unique"):
        Dataset(
            "export.csv",
            ("id", "amount"),
            (
                Record("export.csv", 2, {"id": "A", "amount": "10"}),
                Record("export.csv", 2, {"id": "B", "amount": "20"}),
            ),
        )


@pytest.mark.parametrize("row_number", [True, 1.5, 0, -1])
def test_record_positions_are_positive_integers(row_number) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        Record("export.csv", row_number, {"id": "A"})


def test_dataset_retains_its_input_snapshot_after_caller_mutation() -> None:
    headers = ["id", "amount"]
    records = [Record("left", 2, {"id": "A", "amount": "10"})]
    left = Dataset("left", headers, records)
    headers.append("accidental")
    records.clear()
    right = dataset("right", ("id", "amount"), [{"id": "A", "amount": "10"}])

    result = reconcile(left, right, ReconciliationConfig(key_fields=("id",)))

    assert result.is_clean
    assert result.matched_count == 1
    assert left.headers == ("id", "amount")


def test_dataset_rejects_non_record_items() -> None:
    with pytest.raises(TypeError, match="Record instances"):
        Dataset("left", ("id",), ({"id": "A"},))
