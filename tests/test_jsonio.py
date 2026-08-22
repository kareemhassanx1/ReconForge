from __future__ import annotations

from pathlib import Path

import pytest

from reconforge.errors import InputError
from reconforge.jsonio import JsonReadOptions, read_json_dataset, read_ndjson_dataset


def write_file(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_reads_json_array_with_scalar_values(tmp_path: Path) -> None:
    path = write_file(
        tmp_path,
        "records.json",
        """
        [
          {"id": 7, "amount": 12.500, "active": true, "note": null},
          {"id": 8, "amount": 4, "active": false, "note": " ready "}
        ]
        """,
    )

    dataset = read_json_dataset(path)

    assert dataset.headers == ("id", "amount", "active", "note")
    assert [record.row_number for record in dataset.records] == [1, 2]
    assert dataset.records[0].values == {
        "id": "7",
        "amount": "12.500",
        "active": "true",
        "note": None,
    }
    assert dataset.records[1].value("note") == "ready"


def test_reads_ndjson_and_preserves_physical_line_numbers(tmp_path: Path) -> None:
    path = write_file(
        tmp_path,
        "records.ndjson",
        '\n{"id": "A", "amount": 10}\n\n{"id": "B", "amount": 20}\n',
    )

    dataset = read_ndjson_dataset(path)

    assert dataset.headers == ("id", "amount")
    assert [record.row_number for record in dataset.records] == [2, 4]


def test_json_strings_can_preserve_whitespace_and_empty_values(tmp_path: Path) -> None:
    path = write_file(tmp_path, "records.json", '[{"id": " A ", "note": ""}]')
    options = JsonReadOptions(trim_values=False, empty_as_none=False)

    dataset = read_json_dataset(path, options)

    assert dataset.records[0].value("id") == " A "
    assert dataset.records[0].value("note") == ""


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("empty.json", "", "input file is empty"),
        ("empty-array.json", "[]", "contains no records"),
        ("object.json", '{"id": 1}', "top-level array"),
        ("scalar-record.json", '[{"id": 1}, 2]', "record/line 2.*must be an object"),
        ("empty-record.json", "[{}]", "record/line 1.*has no fields"),
    ],
)
def test_rejects_invalid_json_dataset_shapes(
    tmp_path: Path,
    name: str,
    content: str,
    message: str,
) -> None:
    path = write_file(tmp_path, name, content)

    with pytest.raises(InputError, match=message):
        read_json_dataset(path)


def test_json_syntax_error_reports_line_and_column(tmp_path: Path) -> None:
    path = write_file(tmp_path, "broken.json", '[\n  {"id": 1},\n]')

    with pytest.raises(InputError, match=r"line 3, column 1"):
        read_json_dataset(path)


def test_ndjson_syntax_error_reports_physical_line_and_column(tmp_path: Path) -> None:
    path = write_file(tmp_path, "broken.ndjson", '\n{"id": 1}\n{"id": }\n')

    with pytest.raises(InputError, match=r"line 3, column 8"):
        read_ndjson_dataset(path)


@pytest.mark.parametrize("reader", [read_json_dataset, read_ndjson_dataset])
def test_rejects_duplicate_json_object_keys(tmp_path: Path, reader) -> None:
    suffix = ".json" if reader is read_json_dataset else ".ndjson"
    content = '[{"id": 1, "id": 2}]' if reader is read_json_dataset else '{"id": 1, "id": 2}'
    path = write_file(tmp_path, f"duplicate{suffix}", content)

    with pytest.raises(InputError, match="duplicate object key 'id'"):
        reader(path)


@pytest.mark.parametrize(
    "content",
    [
        '[{"id": 1, "metadata": {"source": "api"}}]',
        '[{"id": 1, "tags": ["one", "two"]}]',
    ],
)
def test_rejects_nested_json_values(tmp_path: Path, content: str) -> None:
    path = write_file(tmp_path, "nested.json", content)

    with pytest.raises(InputError, match="nested or unsupported JSON value"):
        read_json_dataset(path)


def test_rejects_missing_fields_in_later_objects(tmp_path: Path) -> None:
    path = write_file(
        tmp_path,
        "missing.json",
        '[{"id": 1, "amount": 10}, {"id": 2}]',
    )

    with pytest.raises(InputError, match=r"record/line 2: missing fields \['amount'\]"):
        read_json_dataset(path)


def test_rejects_unexpected_fields_in_later_objects(tmp_path: Path) -> None:
    path = write_file(
        tmp_path,
        "extra.ndjson",
        '{"id": 1}\n{"id": 2, "amount": 10}\n',
    )

    with pytest.raises(InputError, match=r"record/line 2: unexpected fields \['amount'\]"):
        read_ndjson_dataset(path)


def test_rejects_fields_that_collide_after_header_trimming(tmp_path: Path) -> None:
    path = write_file(tmp_path, "headers.json", '[{"id": 1, " id ": 2}]')

    with pytest.raises(InputError, match="duplicate object field name"):
        read_json_dataset(path)


def test_missing_json_file_uses_the_common_input_error(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="input file does not exist"):
        read_json_dataset(tmp_path / "missing.json")
