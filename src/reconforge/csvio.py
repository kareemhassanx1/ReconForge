"""CSV ingestion with deterministic normalization and validation."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from reconforge.errors import InputError
from reconforge.model import CellValue, Dataset, Record

_DELIMITER_CANDIDATES = ",;\t|"


@dataclass(frozen=True, slots=True)
class CsvReadOptions:
    """Controls how a CSV source is decoded and normalized."""

    delimiter: str | None = None
    encoding: str = "utf-8-sig"
    trim_headers: bool = True
    trim_values: bool = True
    empty_as_none: bool = True

    def __post_init__(self) -> None:
        if self.delimiter is not None and len(self.delimiter) != 1:
            raise ValueError("delimiter must be exactly one character")


def read_csv_dataset(
    path: str | Path,
    options: CsvReadOptions | None = None,
) -> Dataset:
    """Load and validate a CSV file as a normalized :class:`Dataset`."""

    resolved = Path(path)
    selected = options or CsvReadOptions()

    if not resolved.exists():
        raise InputError(f"input file does not exist: {resolved}")
    if not resolved.is_file():
        raise InputError(f"input path is not a file: {resolved}")

    try:
        with resolved.open("r", encoding=selected.encoding, newline="") as stream:
            delimiter = selected.delimiter or _detect_delimiter(stream, resolved)
            stream.seek(0)
            return _read_rows(stream, resolved, delimiter, selected)
    except UnicodeError as exc:
        raise InputError(f"cannot decode {resolved} using {selected.encoding}") from exc
    except OSError as exc:
        raise InputError(f"cannot read input file {resolved}: {exc}") from exc


def _detect_delimiter(stream: TextIO, path: Path) -> str:
    sample = stream.read(8192)
    if not sample.strip():
        raise InputError(f"input file is empty: {path}")

    try:
        return csv.Sniffer().sniff(sample, delimiters=_DELIMITER_CANDIDATES).delimiter
    except csv.Error:
        # Sniffer considers the consistency of every sampled row. That is useful
        # for well-formed data but prevents us from reaching the clearer row-width
        # validation below when a file contains a malformed record. The header is
        # a deterministic fallback and also supports files with blank data rows.
        header = next((line for line in sample.splitlines() if line.strip()), "")
        counts = {candidate: header.count(candidate) for candidate in _DELIMITER_CANDIDATES}
        highest = max(counts.values(), default=0)
        if highest == 0:
            return ","
        winners = [candidate for candidate, count in counts.items() if count == highest]
        if len(winners) == 1:
            return winners[0]
        raise InputError(
            f"could not detect a delimiter in {path}; provide one explicitly"
        ) from None


def _read_rows(
    stream: TextIO,
    path: Path,
    delimiter: str,
    options: CsvReadOptions,
) -> Dataset:
    reader = csv.reader(stream, delimiter=delimiter, strict=True)

    try:
        raw_headers = next(reader)
    except StopIteration as exc:
        raise InputError(f"input file is empty: {path}") from exc
    except csv.Error as exc:
        raise InputError(f"invalid CSV header in {path}: {exc}") from exc

    headers = tuple(_normalize_header(value, options) for value in raw_headers)
    _validate_headers(headers, path)

    records: list[Record] = []
    try:
        for row_number, values in enumerate(reader, start=2):
            if not values or all(not value.strip() for value in values):
                continue
            if len(values) != len(headers):
                raise InputError(
                    f"row {row_number} in {path} has {len(values)} values; expected {len(headers)}"
                )
            normalized = {
                header: _normalize_value(value, options)
                for header, value in zip(headers, values, strict=True)
            }
            records.append(Record(source=str(path), row_number=row_number, values=normalized))
    except csv.Error as exc:
        raise InputError(f"invalid CSV data in {path} near row {reader.line_num}: {exc}") from exc

    return Dataset(source=str(path), headers=headers, records=tuple(records))


def _normalize_header(value: str, options: CsvReadOptions) -> str:
    return value.strip() if options.trim_headers else value


def _normalize_value(value: str, options: CsvReadOptions) -> CellValue:
    normalized = value.strip() if options.trim_values else value
    if options.empty_as_none and normalized == "":
        return None
    return normalized


def _validate_headers(headers: tuple[str, ...], path: Path) -> None:
    if not headers:
        raise InputError(f"input file has no columns: {path}")

    empty_positions = [str(index + 1) for index, header in enumerate(headers) if not header]
    if empty_positions:
        joined = ", ".join(empty_positions)
        raise InputError(f"empty column name in {path} at position(s): {joined}")

    duplicates = sorted({header for header in headers if headers.count(header) > 1})
    if duplicates:
        joined = ", ".join(repr(header) for header in duplicates)
        raise InputError(f"duplicate column name(s) in {path}: {joined}")
