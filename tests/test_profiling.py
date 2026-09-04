from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from reconforge.errors import InputError
from reconforge.model import Dataset, Record
from reconforge.profiling import (
    PROFILE_VERSION,
    ProfileOptions,
    ValueKind,
    classify_value,
    load_json_profile,
    profile_dataset,
    profile_fingerprint,
    profile_from_dict,
    profile_to_dict,
)


def make_dataset(source: str = "دفتر المبيعات.csv") -> Dataset:
    headers = ("flag", "amount", "due", "note")
    rows = (
        ("TRUE", "001", "2026-01-03", "قيمة"),
        ("false", "2.50", "2025-12-31", "=SUM(A1:A2)"),
        (None, "-3e0", "2026-02-29", "قيمة"),
        ("true", "2.50", "2026-01-01", None),
    )
    return Dataset(
        source,
        headers,
        tuple(
            Record(source, index + 2, dict(zip(headers, values, strict=True)))
            for index, values in enumerate(rows)
        ),
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ValueKind.NULL),
        ("true", ValueKind.BOOLEAN),
        ("FALSE", ValueKind.BOOLEAN),
        ("+001", ValueKind.INTEGER),
        ("-0", ValueKind.INTEGER),
        ("1.0", ValueKind.DECIMAL),
        (".5", ValueKind.DECIMAL),
        ("-2E+4", ValueKind.DECIMAL),
        ("2024-02-29", ValueKind.DATE),
        ("2023-02-29", ValueKind.TEXT),
        ("NaN", ValueKind.TEXT),
        ("Infinity", ValueKind.TEXT),
        (" 1", ValueKind.TEXT),
        ("١", ValueKind.TEXT),
        ("", ValueKind.TEXT),
    ],
)
def test_classification_is_strict_and_deterministic(
    value: str | None,
    expected: ValueKind,
) -> None:
    assert classify_value(value) is expected


def test_classification_rejects_non_cell_values() -> None:
    with pytest.raises(TypeError, match="strings or null"):
        classify_value(1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_values_limit": -1},
        {"top_values_limit": 1001},
        {"top_values_limit": True},
        {"examples_limit": -1},
        {"examples_limit": 1.5},
    ],
)
def test_profile_options_enforce_bounded_integer_limits(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="integer between 0 and 1000"):
        ProfileOptions(**kwargs)  # type: ignore[arg-type]


def test_profile_collects_counts_ranges_and_bounded_samples() -> None:
    profile = profile_dataset(
        make_dataset(),
        ProfileOptions(top_values_limit=2, examples_limit=2),
    )

    assert profile.source == "دفتر المبيعات.csv"
    assert profile.record_count == 4
    assert profile.headers == ("flag", "amount", "due", "note")

    flag = profile.field("flag")
    assert (flag.null_count, flag.non_null_count, flag.distinct_count) == (1, 3, 3)
    assert flag.count("null") == 1
    assert flag.count(ValueKind.BOOLEAN) == 3
    assert [(item.value, item.count) for item in flag.top_values] == [
        ("TRUE", 1),
        ("false", 1),
    ]
    assert flag.examples == ("TRUE", "false")

    amount = profile.field("amount")
    assert amount.count(ValueKind.INTEGER) == 1
    assert amount.count(ValueKind.DECIMAL) == 3
    assert amount.numeric_range == ("-3", "2.5")
    assert amount.date_range is None
    assert (amount.min_length, amount.max_length) == (3, 4)
    assert [(item.value, item.count) for item in amount.top_values] == [
        ("2.50", 2),
        ("-3e0", 1),
    ]

    due = profile.field("due")
    assert due.count(ValueKind.DATE) == 3
    assert due.count(ValueKind.TEXT) == 1
    assert due.date_range is None  # invalid leap day makes the field mixed

    note = profile.field("note")
    assert note.top_values[0].value == "قيمة"
    assert note.top_values[0].count == 2
    assert note.examples == ("=SUM(A1:A2)", "قيمة")


def test_profile_is_invariant_to_record_order() -> None:
    dataset = make_dataset()
    reordered = Dataset(dataset.source, dataset.headers, tuple(reversed(dataset.records)))

    assert profile_dataset(dataset) == profile_dataset(reordered)
    assert profile_fingerprint(profile_dataset(dataset)) == profile_fingerprint(
        profile_dataset(reordered)
    )


def test_profile_fingerprint_intentionally_covers_source_identity() -> None:
    first = profile_dataset(make_dataset("first.csv"))
    second = profile_dataset(make_dataset("second.csv"))

    assert first.fields == second.fields
    assert profile_fingerprint(first) != profile_fingerprint(second)


def test_empty_dataset_has_complete_zero_histograms() -> None:
    profile = profile_dataset(Dataset("empty.csv", ("id", "value"), ()))

    assert profile.record_count == 0
    for field in profile.fields:
        assert field.total_count == 0
        assert field.null_count == 0
        assert field.non_null_count == 0
        assert field.distinct_count == 0
        assert all(count == 0 for _, count in field.kind_counts)
        assert field.min_length is None
        assert field.max_length is None
        assert field.numeric_range is None
        assert field.date_range is None
        assert field.top_values == ()
        assert field.examples == ()


def test_zero_sample_limits_omit_samples_without_losing_statistics() -> None:
    profile = profile_dataset(
        make_dataset(),
        ProfileOptions(top_values_limit=0, examples_limit=0),
    )

    assert all(field.top_values == () and field.examples == () for field in profile.fields)
    assert profile.field("amount").distinct_count == 3


def test_date_range_is_present_for_consistently_typed_date_field() -> None:
    source = "dates.csv"
    dataset = Dataset(
        source,
        ("date",),
        (
            Record(source, 2, {"date": "2025-12-31"}),
            Record(source, 3, {"date": "2024-02-29"}),
            Record(source, 4, {"date": "2026-01-01"}),
        ),
    )

    field = profile_dataset(dataset).field("date")

    assert field.date_range == ("2024-02-29", "2026-01-01")
    assert field.numeric_range is None


def test_numeric_and_date_ranges_ignore_null_values() -> None:
    source = "nullable.csv"
    dataset = Dataset(
        source,
        ("amount", "date"),
        (
            Record(source, 2, {"amount": "10.50", "date": "2025-03-01"}),
            Record(source, 3, {"amount": None, "date": None}),
            Record(source, 4, {"amount": "-2", "date": "2024-12-31"}),
        ),
    )

    profile = profile_dataset(dataset)

    assert profile.field("amount").numeric_range == ("-2", "10.5")
    assert profile.field("date").date_range == ("2024-12-31", "2025-03-01")


def test_large_repeated_unicode_value_is_bounded_and_counted_exactly() -> None:
    value = "🧾ق" * 20_000
    source = "large.csv"
    dataset = Dataset(
        source,
        ("payload",),
        tuple(Record(source, row + 2, {"payload": value}) for row in range(200)),
    )

    field = profile_dataset(dataset, ProfileOptions(1, 1)).field("payload")

    assert field.distinct_count == 1
    assert field.top_values[0].count == 200
    assert field.examples == (value,)
    assert field.min_length == field.max_length == len(value)


def test_profile_models_are_frozen_and_nested_collections_are_immutable() -> None:
    profile = profile_dataset(make_dataset())

    with pytest.raises(FrozenInstanceError):
        profile.record_count = 99  # type: ignore[misc]
    with pytest.raises(TypeError):
        profile.fields[0] = profile.fields[0]  # type: ignore[index]


def test_profile_rejects_records_that_do_not_match_headers() -> None:
    source = "broken.csv"
    missing = Dataset(source, ("id", "amount"), (Record(source, 2, {"id": "A"}),))
    extra = Dataset(
        source,
        ("id",),
        (Record(source, 2, {"id": "A", "extra": "x"}),),
    )

    with pytest.raises(ValueError, match=r"row 2.*missing fields \['amount'\]"):
        profile_dataset(missing)
    with pytest.raises(ValueError, match=r"row 2.*unknown fields \['extra'\]"):
        profile_dataset(extra)


def test_profile_rejects_non_string_record_values() -> None:
    source = "broken.csv"
    dataset = Dataset(
        source,
        ("id",),
        (Record(source, 2, {"id": 42}),),  # type: ignore[dict-item]
    )

    with pytest.raises(TypeError, match=r"row 2 field 'id'.*string or null"):
        profile_dataset(dataset)


def test_profile_round_trip_has_an_exact_versioned_schema() -> None:
    profile = profile_dataset(make_dataset(), ProfileOptions(3, 2))

    payload = profile_to_dict(profile)
    restored = profile_from_dict(json.loads(json.dumps(payload, ensure_ascii=False)))

    assert restored == profile
    assert payload["profile_version"] == PROFILE_VERSION
    assert set(payload) == {
        "profile_version",
        "fingerprint",
        "source",
        "record_count",
        "options",
        "fields",
    }
    assert payload["fingerprint"] == profile_fingerprint(profile)


@pytest.mark.parametrize("field", ["unknown", "missing"])
def test_profile_loader_rejects_unknown_and_missing_top_level_fields(field: str) -> None:
    payload = profile_to_dict(profile_dataset(make_dataset()))
    if field == "unknown":
        payload["unexpected"] = True
    else:
        del payload["source"]

    with pytest.raises(ValueError, match="unknown field|missing required field"):
        profile_from_dict(payload)


@pytest.mark.parametrize("version", [True, 0, 2, "1"])
def test_profile_loader_rejects_noncanonical_versions(version: object) -> None:
    payload = profile_to_dict(profile_dataset(make_dataset()))
    payload["profile_version"] = version

    with pytest.raises(ValueError, match="unsupported profile version"):
        profile_from_dict(payload)


def test_profile_loader_rejects_tampering_and_malformed_fingerprints() -> None:
    payload = profile_to_dict(profile_dataset(make_dataset()))
    tampered = copy.deepcopy(payload)
    tampered["source"] = "different.csv"

    with pytest.raises(ValueError, match="fingerprint is"):
        profile_from_dict(tampered)

    payload["fingerprint"] = "sha256:" + ("A" * 64)
    with pytest.raises(ValueError, match="lowercase hex"):
        profile_from_dict(payload)


def test_profile_loader_rejects_bool_counts_and_inconsistent_semantics() -> None:
    payload = profile_to_dict(profile_dataset(make_dataset()))
    invalid_bool = copy.deepcopy(payload)
    invalid_bool["record_count"] = True
    with pytest.raises(ValueError, match="non-negative integer"):
        profile_from_dict(invalid_bool)

    invalid_count = copy.deepcopy(payload)
    invalid_count["fields"][0]["null_count"] = 2  # type: ignore[index]
    with pytest.raises(ValueError, match="null and non-null counts"):
        profile_from_dict(invalid_count)

    truncated = copy.deepcopy(payload)
    truncated["fields"][0]["top_values"].pop()  # type: ignore[index,union-attr]
    with pytest.raises(ValueError, match="must contain .* top values"):
        profile_from_dict(truncated)


def test_profile_loader_rejects_duplicate_fields_and_top_values() -> None:
    payload = profile_to_dict(profile_dataset(make_dataset()))
    duplicate_field = copy.deepcopy(payload)
    duplicate_field["fields"].append(copy.deepcopy(duplicate_field["fields"][0]))  # type: ignore[union-attr,index]
    with pytest.raises(ValueError, match="field names must be unique"):
        profile_from_dict(duplicate_field)

    duplicate_value = copy.deepcopy(payload)
    top_values = duplicate_value["fields"][0]["top_values"]  # type: ignore[index]
    top_values[1]["value"] = top_values[0]["value"]  # type: ignore[index]
    with pytest.raises(ValueError, match="unique values"):
        profile_from_dict(duplicate_value)


def test_load_json_profile_accepts_bom_and_wraps_schema_errors(tmp_path: Path) -> None:
    profile = profile_dataset(make_dataset())
    path = tmp_path / "profile.json"
    path.write_text("\ufeff" + json.dumps(profile_to_dict(profile)), encoding="utf-8")

    assert load_json_profile(path) == profile

    path.write_text('{"profile_version": 1}', encoding="utf-8")
    with pytest.raises(InputError, match="invalid dataset profile"):
        load_json_profile(path)


def test_load_json_profile_rejects_duplicate_keys_and_bad_json(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    path.write_text('{"source": "a", "source": "b"}', encoding="utf-8")
    with pytest.raises(InputError, match="duplicate object key 'source'"):
        load_json_profile(path)

    path.write_text("{", encoding="utf-8")
    with pytest.raises(InputError, match=r"line 1, column 2"):
        load_json_profile(path)


def test_load_json_profile_reports_missing_and_invalid_utf8(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="does not exist"):
        load_json_profile(tmp_path / "missing.json")

    path = tmp_path / "invalid.json"
    path.write_bytes(b"\xff")
    with pytest.raises(InputError, match="not valid UTF-8"):
        load_json_profile(path)
