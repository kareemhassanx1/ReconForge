"""Deterministic exporters for ReconForge's canonical report model."""

from __future__ import annotations

import csv
import html
import io
import json
from collections.abc import Iterable, Mapping
from contextlib import suppress
from enum import StrEnum
from pathlib import Path

from reconforge.errors import ConfigurationError


class ReportFormat(StrEnum):
    """Built-in output formats for canonical reconciliation reports."""

    JSON = "json"
    CSV = "csv"
    HTML = "html"

    @classmethod
    def named(cls, name: str) -> ReportFormat:
        try:
            return cls(name.strip().lower())
        except ValueError as exc:
            choices = ", ".join(item.value for item in cls)
            raise ValueError(f"unknown report format {name!r}; expected one of: {choices}") from exc


_CSV_COLUMNS = (
    "scope",
    "pair_name",
    "status",
    "issue_type",
    "key",
    "field",
    "left_value",
    "right_value",
    "side",
    "source",
    "row_numbers",
    "details",
)


def render_report(report: Mapping[str, object], format: ReportFormat | str) -> str:
    """Render one canonical single or batch report without timestamps or side effects."""

    selected = ReportFormat.named(format) if isinstance(format, str) else format
    if not isinstance(selected, ReportFormat):
        raise TypeError("report format must be a ReportFormat or string")
    canonical = _mapping(report, "report")
    _report_kind(canonical)
    if selected is ReportFormat.JSON:
        return json.dumps(canonical, indent=2, ensure_ascii=False) + "\n"
    if selected is ReportFormat.CSV:
        return _render_csv(canonical)
    return _render_html(canonical)


def write_report(
    report: Mapping[str, object],
    destination: str | Path,
    format: ReportFormat | str,
) -> Path:
    """Render and atomically write a canonical report."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    payload = render_report(report, format)
    try:
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        temporary.replace(path)
    except OSError:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
    return path.resolve()


def infer_report_format(path: str | Path) -> ReportFormat | None:
    """Infer a report format from a recognized output suffix."""

    suffix = Path(path).suffix.lower()
    return {
        ".json": ReportFormat.JSON,
        ".csv": ReportFormat.CSV,
        ".html": ReportFormat.HTML,
        ".htm": ReportFormat.HTML,
    }.get(suffix)


def _render_csv(report: dict[str, object]) -> str:
    destination = io.StringIO(newline="")
    writer = csv.DictWriter(destination, fieldnames=_CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in _report_rows(report):
        writer.writerow({name: _safe_csv_cell(row.get(name, "")) for name in _CSV_COLUMNS})
    return destination.getvalue()


def _report_rows(report: dict[str, object]) -> Iterable[dict[str, object]]:
    kind = _report_kind(report)
    if kind == "single":
        yield from _single_report_rows(report, scope="report", pair_name="")
        return

    status = _string(report.get("status"), "batch report status")
    yield _summary_row("batch", "", status, _mapping(report.get("summary"), "batch summary"))
    unmatched = _mapping(report.get("unmatched_inputs"), "unmatched inputs")
    if "execution" in report:
        yield {
            "scope": "batch",
            "status": status,
            "issue_type": "batch_execution",
            "details": _compact_json(report["execution"]),
        }
    for side in ("left", "right"):
        for source in _array(unmatched.get(side), f"unmatched_inputs.{side}"):
            yield {
                "scope": "batch",
                "status": status,
                "issue_type": f"unmatched_{side}",
                "side": side,
                "source": _string(source, f"unmatched {side} source"),
            }
    for index, raw_pair in enumerate(_array(report.get("pairs"), "batch pairs")):
        pair = _mapping(raw_pair, f"batch pairs[{index}]")
        name = _string(pair.get("name"), f"batch pairs[{index}].name")
        pair_report = _mapping(pair.get("result"), f"batch pairs[{index}].result")
        yield from _single_report_rows(pair_report, scope="pair", pair_name=name)


def _single_report_rows(
    report: dict[str, object], *, scope: str, pair_name: str
) -> Iterable[dict[str, object]]:
    if report.get("report_version") != 1:
        raise ConfigurationError("CSV export supports report_version 1")
    status = _string(report.get("status"), "report status")
    yield _summary_row(scope, pair_name, status, _mapping(report.get("summary"), "summary"))
    base = {"scope": scope, "pair_name": pair_name, "status": status}

    for index, raw_item in enumerate(_array(report.get("differences"), "differences")):
        item = _mapping(raw_item, f"differences[{index}]")
        yield {
            **base,
            "issue_type": "field_difference",
            "key": _compact_json(item.get("key")),
            "field": item.get("field", ""),
            "left_value": item.get("left_value"),
            "right_value": item.get("right_value"),
            "row_numbers": _compact_json([item.get("left_row"), item.get("right_row")]),
        }
    for index, raw_item in enumerate(
        _array(report.get("validation_violations"), "validation_violations")
    ):
        item = _mapping(raw_item, f"validation_violations[{index}]")
        yield {
            **base,
            "issue_type": "validation_violation",
            "field": item.get("field", ""),
            "left_value": item.get("value"),
            "side": item.get("side", ""),
            "source": item.get("source", ""),
            "row_numbers": _compact_json([item.get("row_number")]),
            "details": _compact_json({"rule": item.get("rule"), "message": item.get("message")}),
        }
    missing = _mapping(report.get("missing_records"), "missing_records")
    for side, row_key in (("left", "right_row"), ("right", "left_row")):
        for index, raw_item in enumerate(
            _array(missing.get(f"from_{side}"), f"missing_records.from_{side}")
        ):
            item = _mapping(raw_item, f"missing_records.from_{side}[{index}]")
            yield {
                **base,
                "issue_type": f"missing_from_{side}",
                "key": _compact_json(item.get("key")),
                "side": side,
                "row_numbers": _compact_json([item.get(row_key)]),
            }
    duplicates = _mapping(report.get("duplicate_keys"), "duplicate_keys")
    for side in ("left", "right"):
        for index, raw_item in enumerate(_array(duplicates.get(side), f"duplicate_keys.{side}")):
            item = _mapping(raw_item, f"duplicate_keys.{side}[{index}]")
            yield {
                **base,
                "issue_type": f"duplicate_key_{side}",
                "key": _compact_json(item.get("key")),
                "side": side,
                "row_numbers": _compact_json(item.get("row_numbers")),
            }
    schema = _mapping(report.get("schema"), "schema")
    for side in ("left", "right"):
        for field in _array(schema.get(f"fields_only_{side}"), f"schema.fields_only_{side}"):
            yield {
                **base,
                "issue_type": f"field_only_{side}",
                "field": field,
                "side": side,
            }


def _summary_row(
    scope: str,
    pair_name: str,
    status: str,
    summary: Mapping[str, object],
) -> dict[str, object]:
    return {
        "scope": scope,
        "pair_name": pair_name,
        "status": status,
        "issue_type": "summary",
        "details": _compact_json(summary),
    }


def _render_html(report: dict[str, object]) -> str:
    kind = _report_kind(report)
    title = "ReconForge Batch Report" if kind == "batch" else "ReconForge Report"
    status = _string(report.get("status"), "report status")
    rows = list(_report_rows(report))
    summary = _mapping(report.get("summary"), "report summary")
    summary_cells = "".join(
        f'<div class="metric"><dt>{_escape(label.replace("_", " ").title())}</dt>'
        f"<dd>{_escape(value)}</dd></div>"
        for label, value in summary.items()
    )
    table_rows = "".join(
        "<tr>"
        + "".join(f"<td>{_escape(row.get(column, ''))}</td>" for column in _CSV_COLUMNS)
        + "</tr>"
        for row in rows
    )
    headings = "".join(
        f'<th scope="col">{_escape(name.replace("_", " "))}</th>' for name in _CSV_COLUMNS
    )
    canonical = _escape(json.dumps(report, indent=2, ensure_ascii=False))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(title)} — {_escape(status)}</title>
<style>
:root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
body {{ max-width: 1180px; margin: 0 auto; padding: 2rem; line-height: 1.45; }}
h1 {{ margin-bottom: .25rem; }} .status {{ font-weight: 700; }}
.metrics {{ display: flex; flex-wrap: wrap; gap: .75rem; padding: 0; }}
.metric {{ border: 1px solid #8888; border-radius: .5rem; padding: .65rem .85rem; }}
.metric dt {{ font-size: .8rem; opacity: .75; }} .metric dd {{ margin: 0; font-size: 1.25rem; }}
.table-wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-size: .85rem; }}
th, td {{ border: 1px solid #8886; padding: .45rem; text-align: left; vertical-align: top; }}
th {{ position: sticky; top: 0; background: Canvas; }}
pre {{ overflow: auto; padding: 1rem; border: 1px solid #8886; }}
</style>
</head>
<body>
<main>
<h1>{_escape(title)}</h1>
<p>Status: <span class="status">{_escape(status)}</span></p>
<dl class="metrics">{summary_cells}</dl>
<h2>Report rows</h2>
<div class="table-wrap">
<table><thead><tr>{headings}</tr></thead><tbody>{table_rows}</tbody></table>
</div>
<details><summary>Canonical report data</summary><pre>{canonical}</pre></details>
</main>
</body>
</html>
"""


def _report_kind(report: Mapping[str, object]) -> str:
    if report.get("report_version") == 1:
        return "single"
    if report.get("batch_report_version") == 1:
        return "batch"
    raise ConfigurationError("report must contain supported report_version or batch_report_version")


def _mapping(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{location} must be an object")
    return dict(value)


def _array(value: object, location: str) -> list[object]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{location} must be an array")
    return value


def _string(value: object, location: str) -> str:
    if not isinstance(value, str):
        raise ConfigurationError(f"{location} must be a string")
    return value


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_csv_cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        value = _compact_json(value)
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value


def _escape(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)
