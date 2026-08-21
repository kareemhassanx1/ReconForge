from __future__ import annotations

from pathlib import Path

import pytest

from reconforge.csvio import CsvReadOptions, read_csv_dataset
from reconforge.errors import InputError


def write_csv(tmp_path: Path, content: str, name: str = "input.csv") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_reads_and_normalizes_a_csv_dataset(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        " invoice_id , customer ,notes\n INV-1 , Northwind , \nINV-2,Contoso,ready\n",
    )

    dataset = read_csv_dataset(path)

    assert dataset.headers == ("invoice_id", "customer", "notes")
    assert len(dataset.records) == 2
    assert dataset.records[0].row_number == 2
    assert dataset.records[0].values == {
        "invoice_id": "INV-1",
        "customer": "Northwind",
        "notes": None,
    }


def test_detects_semicolon_delimiter(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "id;amount\nA;10\nB;20\n")

    dataset = read_csv_dataset(path)

    assert dataset.headers == ("id", "amount")
    assert [record.value("amount") for record in dataset.records] == ["10", "20"]


def test_explicit_delimiter_supports_pipe_files(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "id|status\nA|paid\n")

    dataset = read_csv_dataset(path, CsvReadOptions(delimiter="|"))

    assert dataset.records[0].value("status") == "paid"


def test_blank_data_rows_are_skipped_without_changing_source_row_numbers(
    tmp_path: Path,
) -> None:
    path = write_csv(tmp_path, "id,value\n\nA,1\n   ,   \nB,2\n")

    dataset = read_csv_dataset(path)

    assert [record.row_number for record in dataset.records] == [3, 5]


def test_rejects_duplicate_headers_after_normalization(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "id, id \nA,B\n")

    with pytest.raises(InputError, match="duplicate column"):
        read_csv_dataset(path)


def test_rejects_empty_headers(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "id,,amount\nA,x,10\n")

    with pytest.raises(InputError, match="empty column name"):
        read_csv_dataset(path)


def test_rejects_rows_with_missing_values(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "id,amount,status\nA,10\n")

    with pytest.raises(InputError, match="row 2.*2 values.*expected 3"):
        read_csv_dataset(path)


def test_rejects_rows_with_extra_values(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "id,amount\nA,10,extra\n")

    with pytest.raises(InputError, match="row 2.*3 values.*expected 2"):
        read_csv_dataset(path)


def test_rejects_empty_files(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "")

    with pytest.raises(InputError, match="input file is empty"):
        read_csv_dataset(path)


def test_rejects_missing_input_file(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="does not exist"):
        read_csv_dataset(tmp_path / "missing.csv")


def test_can_preserve_whitespace_and_empty_strings(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "id,value\n A , \n")
    options = CsvReadOptions(trim_values=False, empty_as_none=False)

    dataset = read_csv_dataset(path, options)

    assert dataset.records[0].value("id") == " A "
    assert dataset.records[0].value("value") == " "


def test_delimiter_must_be_one_character() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        CsvReadOptions(delimiter="::")
