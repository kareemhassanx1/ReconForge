"""Common dataset-reader interface and built-in format dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from reconforge.csvio import CsvReadOptions, read_csv_dataset
from reconforge.errors import InputError
from reconforge.jsonio import JsonReadOptions, read_json_dataset, read_ndjson_dataset
from reconforge.model import Dataset


class DatasetFormat(StrEnum):
    """Input formats supported by the built-in reader collection."""

    CSV = "csv"
    JSON = "json"
    NDJSON = "ndjson"

    @classmethod
    def named(cls, name: str) -> DatasetFormat:
        """Resolve a public format name with a useful error message."""

        aliases = {"jsonl": cls.NDJSON}
        normalized = name.strip().lower()
        if normalized in aliases:
            return aliases[normalized]
        try:
            return cls(normalized)
        except ValueError as exc:
            choices = ", ".join(item.value for item in cls)
            raise ValueError(
                f"unknown dataset format {name!r}; expected one of: {choices}"
            ) from exc


@dataclass(frozen=True, slots=True)
class DatasetReadOptions:
    """Format-neutral options used to select and configure a reader."""

    format: DatasetFormat | str | None = None
    encoding: str = "utf-8-sig"
    delimiter: str | None = None
    trim_headers: bool = True
    trim_values: bool = True
    empty_as_none: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.format, str) and not isinstance(self.format, DatasetFormat):
            object.__setattr__(self, "format", DatasetFormat.named(self.format))


class DatasetReader(Protocol):
    """Contract implemented by every built-in dataset reader."""

    format: DatasetFormat
    extensions: tuple[str, ...]

    def read(self, path: Path, options: DatasetReadOptions) -> Dataset:
        """Read one path into the shared immutable dataset model."""


@dataclass(frozen=True, slots=True)
class CsvDatasetReader:
    format: DatasetFormat = DatasetFormat.CSV
    extensions: tuple[str, ...] = (".csv",)

    def read(self, path: Path, options: DatasetReadOptions) -> Dataset:
        return read_csv_dataset(
            path,
            CsvReadOptions(
                delimiter=options.delimiter,
                encoding=options.encoding,
                trim_headers=options.trim_headers,
                trim_values=options.trim_values,
                empty_as_none=options.empty_as_none,
            ),
        )


@dataclass(frozen=True, slots=True)
class JsonDatasetReader:
    format: DatasetFormat = DatasetFormat.JSON
    extensions: tuple[str, ...] = (".json",)

    def read(self, path: Path, options: DatasetReadOptions) -> Dataset:
        return read_json_dataset(path, _json_options(options))


@dataclass(frozen=True, slots=True)
class NdjsonDatasetReader:
    format: DatasetFormat = DatasetFormat.NDJSON
    extensions: tuple[str, ...] = (".ndjson", ".jsonl")

    def read(self, path: Path, options: DatasetReadOptions) -> Dataset:
        return read_ndjson_dataset(path, _json_options(options))


_READERS: tuple[DatasetReader, ...] = (
    CsvDatasetReader(),
    JsonDatasetReader(),
    NdjsonDatasetReader(),
)


def read_dataset(
    path: str | Path,
    options: DatasetReadOptions | None = None,
) -> Dataset:
    """Read a dataset using an explicit format or a recognized extension."""

    resolved = Path(path)
    selected = options or DatasetReadOptions()
    reader = _reader_for_format(selected.format) if selected.format else _reader_for_path(resolved)
    if selected.delimiter is not None and reader.format is not DatasetFormat.CSV:
        raise InputError(
            f"a delimiter can only be used with CSV input, not {reader.format.value}: {resolved}"
        )
    return reader.read(resolved, selected)


def _reader_for_format(format_name: DatasetFormat | str) -> DatasetReader:
    selected = DatasetFormat.named(format_name) if isinstance(format_name, str) else format_name
    for reader in _READERS:
        if reader.format is selected:
            return reader
    raise AssertionError(f"no built-in reader for {selected}")


def _reader_for_path(path: Path) -> DatasetReader:
    suffix = path.suffix.lower()
    for reader in _READERS:
        if suffix in reader.extensions:
            return reader
    recognized = ", ".join(extension for reader in _READERS for extension in reader.extensions)
    display = suffix or "<none>"
    raise InputError(
        f"cannot detect dataset format from extension {display!r} for {path}; "
        f"recognized extensions: {recognized}; provide the format explicitly"
    )


def _json_options(options: DatasetReadOptions) -> JsonReadOptions:
    return JsonReadOptions(
        encoding=options.encoding,
        trim_headers=options.trim_headers,
        trim_values=options.trim_values,
        empty_as_none=options.empty_as_none,
    )
