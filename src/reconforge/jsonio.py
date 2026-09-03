"""JSON and newline-delimited JSON ingestion for tabular records."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from reconforge.errors import InputError
from reconforge.json_safety import JsonNestingTooDeepError, validate_json_nesting
from reconforge.model import CellValue, Dataset, Record


@dataclass(frozen=True, slots=True)
class JsonReadOptions:
    """Controls string normalization while reading JSON records."""

    encoding: str = "utf-8-sig"
    trim_headers: bool = True
    trim_values: bool = True
    empty_as_none: bool = True


class _DuplicateJsonKey(ValueError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


def read_json_dataset(
    path: str | Path,
    options: JsonReadOptions | None = None,
) -> Dataset:
    """Read a JSON array of flat objects as a dataset."""

    resolved = _require_input_file(path)
    selected = options or JsonReadOptions()
    content = _read_text(resolved, selected.encoding)
    if not content.strip():
        raise InputError(f"input file is empty: {resolved}")

    try:
        payload = _decode_json(content)
    except json.JSONDecodeError as exc:
        raise InputError(
            f"invalid JSON in {resolved} at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except _DuplicateJsonKey as exc:
        raise InputError(f"duplicate object key {exc.key!r} in {resolved}") from exc
    except (JsonNestingTooDeepError, RecursionError) as exc:
        raise InputError(f"JSON in {resolved} exceeds the maximum nesting depth") from exc

    if not isinstance(payload, list):
        raise InputError(f"JSON input must contain a top-level array of objects: {resolved}")
    if not payload:
        raise InputError(f"JSON array contains no records: {resolved}")

    rows = tuple((index, value) for index, value in enumerate(payload, start=1))
    return _objects_to_dataset(resolved, rows, selected, format_name="JSON")


def read_ndjson_dataset(
    path: str | Path,
    options: JsonReadOptions | None = None,
) -> Dataset:
    """Read one flat JSON object per non-blank source line."""

    resolved = _require_input_file(path)
    selected = options or JsonReadOptions()
    content = _read_text(resolved, selected.encoding)
    if not content.strip():
        raise InputError(f"input file is empty: {resolved}")

    rows: list[tuple[int, Any]] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = _decode_json(line)
        except json.JSONDecodeError as exc:
            raise InputError(
                f"invalid NDJSON in {resolved} at line {line_number}, column {exc.colno}: {exc.msg}"
            ) from exc
        except _DuplicateJsonKey as exc:
            raise InputError(
                f"duplicate object key {exc.key!r} in {resolved} at line {line_number}"
            ) from exc
        except (JsonNestingTooDeepError, RecursionError) as exc:
            raise InputError(
                f"NDJSON in {resolved} at line {line_number} exceeds the maximum nesting depth"
            ) from exc
        rows.append((line_number, value))

    if not rows:
        raise InputError(f"NDJSON input contains no records: {resolved}")
    return _objects_to_dataset(resolved, tuple(rows), selected, format_name="NDJSON")


def _decode_json(content: str) -> Any:
    payload = json.loads(
        content,
        parse_float=Decimal,
        object_pairs_hook=_unique_object,
    )
    validate_json_nesting(payload)
    return payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _objects_to_dataset(
    path: Path,
    rows: tuple[tuple[int, Any], ...],
    options: JsonReadOptions,
    *,
    format_name: str,
) -> Dataset:
    first_number, first_value = rows[0]
    first = _require_object(first_value, path, first_number, format_name)
    headers = _normalize_headers(tuple(first), path, first_number, options)

    records: list[Record] = []
    expected = set(headers)
    for row_number, value in rows:
        source_object = _require_object(value, path, row_number, format_name)
        normalized_object = _normalize_object_keys(source_object, path, row_number, options)
        actual = set(normalized_object)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            details = []
            if missing:
                details.append(f"missing fields {missing!r}")
            if extra:
                details.append(f"unexpected fields {extra!r}")
            raise InputError(
                f"inconsistent {format_name} object in {path} at record/line "
                f"{row_number}: {', '.join(details)}"
            )

        normalized_values = {
            header: _json_scalar_to_cell(
                normalized_object[header],
                path,
                row_number,
                header,
                options,
            )
            for header in headers
        }
        records.append(Record(source=str(path), row_number=row_number, values=normalized_values))

    return Dataset(source=str(path), headers=headers, records=tuple(records))


def _require_object(
    value: Any,
    path: Path,
    row_number: int,
    format_name: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError(f"{format_name} record/line {row_number} in {path} must be an object")
    if not value:
        raise InputError(f"{format_name} record/line {row_number} in {path} has no fields")
    return value


def _normalize_headers(
    raw_headers: tuple[str, ...],
    path: Path,
    row_number: int,
    options: JsonReadOptions,
) -> tuple[str, ...]:
    headers = tuple(header.strip() if options.trim_headers else header for header in raw_headers)
    if any(not header for header in headers):
        raise InputError(f"empty object field name in {path} at record/line {row_number}")
    duplicates = sorted({header for header in headers if headers.count(header) > 1})
    if duplicates:
        raise InputError(
            f"duplicate object field name(s) after normalization in {path} at "
            f"record/line {row_number}: {duplicates!r}"
        )
    return headers


def _normalize_object_keys(
    value: dict[str, Any],
    path: Path,
    row_number: int,
    options: JsonReadOptions,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        header = key.strip() if options.trim_headers else key
        if not header:
            raise InputError(f"empty object field name in {path} at record/line {row_number}")
        if header in normalized:
            raise InputError(
                f"duplicate object field name {header!r} after normalization in {path} "
                f"at record/line {row_number}"
            )
        normalized[header] = item
    return normalized


def _json_scalar_to_cell(
    value: Any,
    path: Path,
    row_number: int,
    field: str,
    options: JsonReadOptions,
) -> CellValue:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        normalized = value.strip() if options.trim_values else value
        if options.empty_as_none and normalized == "":
            return None
        return normalized
    if isinstance(value, int | Decimal):
        return str(value)
    raise InputError(
        f"nested or unsupported JSON value in {path} at record/line {row_number}, "
        f"field {field!r}; expected string, number, boolean, or null"
    )


def _require_input_file(path: str | Path) -> Path:
    resolved = Path(path)
    if not resolved.exists():
        raise InputError(f"input file does not exist: {resolved}")
    if not resolved.is_file():
        raise InputError(f"input path is not a file: {resolved}")
    return resolved


def _read_text(path: Path, encoding: str) -> str:
    try:
        return path.read_text(encoding=encoding)
    except UnicodeError as exc:
        raise InputError(f"cannot decode {path} using {encoding}") from exc
    except OSError as exc:
        raise InputError(f"cannot read input file {path}: {exc}") from exc
