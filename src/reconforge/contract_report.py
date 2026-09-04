"""Canonical persistence and rendering for dataset-contract results."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from reconforge.atomic_io import atomic_write_text
from reconforge.errors import InputError
from reconforge.exporters import ReportFormat
from reconforge.json_safety import JsonNestingTooDeepError, validate_json_nesting
from reconforge.schema_contract import (
    ContractResult,
    ContractViolation,
    ContractViolationCode,
)

CONTRACT_REPORT_VERSION = 1

_REPORT_FIELDS = frozenset(
    {
        "contract_report_version",
        "status",
        "dataset",
        "summary",
        "schema",
        "violations",
    }
)
_DATASET_FIELDS = frozenset({"source", "row_count"})
_SUMMARY_FIELDS = frozenset(
    {"checked_fields", "missing_fields", "extra_fields", "violations", "issues"}
)
_SCHEMA_FIELDS = frozenset({"checked_fields", "missing_fields", "extra_fields"})
_VIOLATION_FIELDS = frozenset({"source", "row_number", "field", "value", "code", "message"})
_CSV_COLUMNS = (
    "status",
    "issue_type",
    "source",
    "row_number",
    "field",
    "value",
    "code",
    "message",
)


def contract_result_to_dict(result: ContractResult) -> dict[str, Any]:
    """Serialize a contract result into its exact version-one report schema."""

    if not isinstance(result, ContractResult):
        raise TypeError("result must be a ContractResult instance")
    return {
        "contract_report_version": CONTRACT_REPORT_VERSION,
        "status": _result_status(result),
        "dataset": {
            "source": result.source,
            "row_count": result.row_count,
        },
        "summary": {
            "checked_fields": len(result.checked_fields),
            "missing_fields": len(result.missing_fields),
            "extra_fields": len(result.extra_fields),
            "violations": len(result.violations),
            "issues": result.issue_count,
        },
        "schema": {
            "checked_fields": list(result.checked_fields),
            "missing_fields": list(result.missing_fields),
            "extra_fields": list(result.extra_fields),
        },
        "violations": [
            {
                "source": item.source,
                "row_number": item.row_number,
                "field": item.field,
                "value": item.value,
                "code": item.code.value,
                "message": item.message,
            }
            for item in result.violations
        ],
    }


def contract_result_from_dict(payload: Mapping[str, object]) -> ContractResult:
    """Rebuild a result from a strict, semantically consistent report mapping."""

    try:
        validate_json_nesting(payload)
    except JsonNestingTooDeepError as exc:
        raise ValueError("contract report exceeds the maximum nesting depth") from exc
    report = _exact_object(payload, "contract report", _REPORT_FIELDS)
    version = report["contract_report_version"]
    if type(version) is not int or version != CONTRACT_REPORT_VERSION:
        raise ValueError(
            f"unsupported contract report version {version!r}; expected {CONTRACT_REPORT_VERSION}"
        )
    status = _canonical_text(report["status"], "contract report.status")
    dataset = _exact_object(report["dataset"], "contract report.dataset", _DATASET_FIELDS)
    summary = _exact_object(report["summary"], "contract report.summary", _SUMMARY_FIELDS)
    schema = _exact_object(report["schema"], "contract report.schema", _SCHEMA_FIELDS)

    checked = _field_array(schema["checked_fields"], "contract report.schema.checked_fields")
    missing = _field_array(schema["missing_fields"], "contract report.schema.missing_fields")
    extra = _field_array(schema["extra_fields"], "contract report.schema.extra_fields")
    raw_violations = _array(report["violations"], "contract report.violations")
    violations = tuple(
        _violation_from_dict(value, index) for index, value in enumerate(raw_violations)
    )
    result = ContractResult(
        source=_required_text(dataset["source"], "contract report.dataset.source"),
        row_count=_non_negative_integer(
            dataset["row_count"],
            "contract report.dataset.row_count",
        ),
        checked_fields=checked,
        missing_fields=missing,
        extra_fields=extra,
        violations=violations,
    )
    if violations != result.violations:
        raise ValueError(
            "contract report.violations must use canonical row, field, code, source order"
        )
    _validate_summary(summary, result)
    expected_status = _result_status(result)
    if status != expected_status:
        raise ValueError(
            f"contract report.status is {status!r}; expected {expected_status!r} "
            "from report content"
        )
    return result


def contract_result_fingerprint(result: ContractResult) -> str:
    """Return a deterministic SHA-256 checksum of the complete semantic report."""

    canonical = json.dumps(
        contract_result_to_dict(result),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def load_json_contract_report(path: str | Path) -> ContractResult:
    """Load a canonical UTF-8 contract report with strict JSON validation."""

    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise InputError(f"dataset contract report does not exist: {source}") from exc
    except UnicodeError as exc:
        raise InputError(f"dataset contract report is not valid UTF-8: {source}") from exc
    except OSError as exc:
        raise InputError(f"cannot read dataset contract report {source}: {exc}") from exc
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        validate_json_nesting(payload)
    except _DuplicateObjectKeyError as exc:
        raise InputError(f"{source}: invalid contract report JSON: {exc}") from exc
    except _NonFiniteJsonNumberError as exc:
        raise InputError(f"{source}: invalid contract report JSON: {exc}") from exc
    except (JsonNestingTooDeepError, RecursionError) as exc:
        raise InputError(
            f"{source}: contract report JSON exceeds the maximum nesting depth"
        ) from exc
    except json.JSONDecodeError as exc:
        raise InputError(
            f"{source}: invalid contract report JSON at line {exc.lineno}, "
            f"column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise InputError(f"{source}: top-level contract report JSON value must be an object")
    try:
        return contract_result_from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise InputError(f"{source}: invalid dataset contract report: {exc}") from exc


def render_contract_report(
    result: ContractResult,
    format: ReportFormat | str,
) -> str:
    """Render a result as deterministic JSON, spreadsheet-safe CSV, or HTML."""

    if not isinstance(result, ContractResult):
        raise TypeError("result must be a ContractResult instance")
    selected = ReportFormat.named(format) if isinstance(format, str) else format
    if not isinstance(selected, ReportFormat):
        raise TypeError("report format must be a ReportFormat or string")
    if selected is ReportFormat.JSON:
        return (
            json.dumps(
                contract_result_to_dict(result),
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        )
    if selected is ReportFormat.CSV:
        return _render_csv(result)
    return _render_html(result)


def write_contract_report(
    result: ContractResult,
    destination: str | Path,
    format: ReportFormat | str,
) -> Path:
    """Render and atomically persist a contract report."""

    return atomic_write_text(destination, render_contract_report(result, format))


def _result_status(result: ContractResult) -> str:
    return "clean" if result.is_valid else "issues_found"


def _validate_summary(summary: Mapping[str, object], result: ContractResult) -> None:
    expected = contract_result_to_dict(result)["summary"]
    for name in sorted(_SUMMARY_FIELDS):
        recorded = _non_negative_integer(summary[name], f"contract report.summary.{name}")
        if recorded != expected[name]:
            raise ValueError(
                f"contract report.summary.{name} is {recorded}; "
                f"expected {expected[name]} from report content"
            )


def _violation_from_dict(value: object, index: int) -> ContractViolation:
    location = f"contract report.violations[{index}]"
    payload = _exact_object(value, location, _VIOLATION_FIELDS)
    code_text = _canonical_text(payload["code"], f"{location}.code")
    try:
        code = ContractViolationCode(code_text)
    except ValueError as exc:
        choices = ", ".join(item.value for item in ContractViolationCode)
        raise ValueError(
            f"{location}.code is unknown: {code_text!r}; expected one of: {choices}"
        ) from exc
    cell = payload["value"]
    if cell is not None and not isinstance(cell, str):
        raise ValueError(f"{location}.value must be a string or null")
    return ContractViolation(
        source=_required_text(payload["source"], f"{location}.source"),
        row_number=_positive_integer(payload["row_number"], f"{location}.row_number"),
        field=_canonical_text(payload["field"], f"{location}.field"),
        value=cell,
        code=code,
        message=_required_text(payload["message"], f"{location}.message"),
    )


def _render_csv(result: ContractResult) -> str:
    destination = io.StringIO(newline="")
    writer = csv.DictWriter(destination, fieldnames=_CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in _report_rows(result):
        writer.writerow({name: _safe_csv_cell(row.get(name, "")) for name in _CSV_COLUMNS})
    return destination.getvalue()


def _report_rows(result: ContractResult) -> Iterable[dict[str, object]]:
    status = _result_status(result)
    summary = contract_result_to_dict(result)["summary"]
    yield {
        "status": status,
        "issue_type": "summary",
        "source": result.source,
        "message": _compact_json(summary),
    }
    for field in result.missing_fields:
        yield {
            "status": status,
            "issue_type": "missing_field",
            "source": result.source,
            "field": field,
            "message": "required field is absent from the dataset schema",
        }
    for field in result.extra_fields:
        yield {
            "status": status,
            "issue_type": "extra_field",
            "source": result.source,
            "field": field,
            "message": "field is not declared by the dataset contract",
        }
    for item in result.violations:
        yield {
            "status": status,
            "issue_type": "row_violation",
            "source": item.source,
            "row_number": item.row_number,
            "field": item.field,
            "value": item.value,
            "code": item.code.value,
            "message": item.message,
        }


def _render_html(result: ContractResult) -> str:
    status = _result_status(result)
    payload = contract_result_to_dict(result)
    rows = list(_report_rows(result))
    headings = "".join(
        f'<th scope="col">{_escape(name.replace("_", " "))}</th>' for name in _CSV_COLUMNS
    )
    body = "".join(
        "<tr>"
        + "".join(f"<td>{_escape(row.get(column, ''))}</td>" for column in _CSV_COLUMNS)
        + "</tr>"
        for row in rows
    )
    canonical = _escape(json.dumps(payload, ensure_ascii=False, indent=2))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReconForge Dataset Contract Report — {_escape(status)}</title>
<style>
:root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
body {{ max-width: 1100px; margin: 0 auto; padding: 2rem; line-height: 1.45; }}
h1 {{ margin-bottom: .25rem; }} .status {{ font-weight: 700; }}
.table-wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-size: .875rem; }}
th, td {{ border: 1px solid #8886; padding: .45rem; text-align: left; vertical-align: top; }}
th {{ background: Canvas; position: sticky; top: 0; }}
pre {{ border: 1px solid #8886; overflow: auto; padding: 1rem; }}
</style>
</head>
<body>
<main>
<h1>ReconForge Dataset Contract Report</h1>
<p>Dataset: <strong>{_escape(result.source)}</strong></p>
<p>Status: <span class="status">{_escape(status)}</span></p>
<p>Rows: {_escape(result.row_count)} · Issues: {_escape(result.issue_count)}</p>
<div class="table-wrap">
<table><thead><tr>{headings}</tr></thead><tbody>{body}</tbody></table>
</div>
<details><summary>Canonical report data</summary><pre>{canonical}</pre></details>
</main>
</body>
</html>
"""


def _object(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{location} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{location} keys must be strings")
    return dict(value)


def _exact_object(
    value: object,
    location: str,
    fields: frozenset[str],
) -> dict[str, object]:
    payload = _object(value, location)
    missing = sorted(fields - payload.keys())
    if missing:
        raise ValueError(f"{location} is missing required field(s): {missing!r}")
    unknown = sorted(payload.keys() - fields)
    if unknown:
        raise ValueError(f"{location} contains unknown field(s): {unknown!r}")
    return payload


def _array(value: object, location: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{location} must be an array")
    return value


def _field_array(value: object, location: str) -> tuple[str, ...]:
    fields = tuple(
        _canonical_text(item, f"{location}[{index}]")
        for index, item in enumerate(_array(value, location))
    )
    if len(fields) != len(set(fields)):
        raise ValueError(f"{location} must contain unique field names")
    if fields != tuple(sorted(fields)):
        raise ValueError(f"{location} must be sorted in code-point order")
    return fields


def _canonical_text(value: object, location: str) -> str:
    text = _required_text(value, location)
    if text != text.strip():
        raise ValueError(f"{location} must not contain surrounding whitespace")
    return text


def _required_text(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{location} must be a non-empty string")
    return value


def _non_negative_integer(value: object, location: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{location} must be a non-negative integer")
    return value


def _positive_integer(value: object, location: str) -> int:
    parsed = _non_negative_integer(value, location)
    if parsed == 0:
        raise ValueError(f"{location} must be a positive integer")
    return parsed


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_csv_cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value


def _escape(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


class _DuplicateObjectKeyError(ValueError):
    """Internal signal for ambiguous JSON objects."""


class _NonFiniteJsonNumberError(ValueError):
    """Internal signal for non-standard non-finite JSON numbers."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateObjectKeyError(f"duplicate object key {key!r}")
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> object:
    raise _NonFiniteJsonNumberError(f"non-finite JSON number {value!r} is not supported")
