from __future__ import annotations

from pathlib import Path

import pytest

from reconforge.errors import InputError
from reconforge.readers import DatasetFormat, DatasetReadOptions, read_dataset


def write_file(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("name", "content", "expected_id"),
    [
        ("records.csv", "id,amount\nA,10\n", "A"),
        ("records.json", '[{"id": "B", "amount": 20}]', "B"),
        ("records.ndjson", '{"id": "C", "amount": 30}\n', "C"),
        ("records.jsonl", '{"id": "D", "amount": 40}\n', "D"),
    ],
)
def test_detects_builtin_reader_from_extension(
    tmp_path: Path,
    name: str,
    content: str,
    expected_id: str,
) -> None:
    path = write_file(tmp_path, name, content)

    dataset = read_dataset(path)

    assert dataset.records[0].value("id") == expected_id


def test_extension_detection_is_case_insensitive(tmp_path: Path) -> None:
    path = write_file(tmp_path, "RECORDS.JSON", '[{"id": "A"}]')

    assert read_dataset(path).records[0].value("id") == "A"


def test_explicit_format_reads_a_file_without_extension(tmp_path: Path) -> None:
    path = write_file(tmp_path, "export", '{"id": "A"}\n')

    dataset = read_dataset(path, DatasetReadOptions(format=DatasetFormat.NDJSON))

    assert dataset.records[0].value("id") == "A"


def test_string_format_name_accepts_jsonl_alias(tmp_path: Path) -> None:
    path = write_file(tmp_path, "export.data", '{"id": "A"}\n')

    dataset = read_dataset(path, DatasetReadOptions(format="jsonl"))

    assert dataset.records[0].value("id") == "A"


def test_unknown_extension_lists_recognized_extensions(tmp_path: Path) -> None:
    path = write_file(tmp_path, "records.txt", "id,amount\nA,10\n")

    with pytest.raises(InputError, match=r"recognized extensions: .csv, .json, .ndjson, .jsonl"):
        read_dataset(path)


def test_unknown_explicit_format_lists_choices() -> None:
    with pytest.raises(ValueError, match="expected one of: csv, json, ndjson"):
        DatasetReadOptions(format="xml")


def test_delimiter_is_rejected_for_non_csv_input(tmp_path: Path) -> None:
    path = write_file(tmp_path, "records.json", '[{"id": "A"}]')

    with pytest.raises(InputError, match="delimiter can only be used with CSV"):
        read_dataset(path, DatasetReadOptions(delimiter=";"))


def test_explicit_csv_format_forwards_delimiter_options(tmp_path: Path) -> None:
    path = write_file(tmp_path, "records.data", "id;amount\nA;10\n")

    dataset = read_dataset(
        path,
        DatasetReadOptions(format="csv", delimiter=";"),
    )

    assert dataset.records[0].value("amount") == "10"
