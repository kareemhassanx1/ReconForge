"""Deterministic JSON, CSV, and HTML rendering for dataset profiles."""

from __future__ import annotations

import csv
import html
import io
import json
from pathlib import Path

from reconforge.atomic_io import atomic_write_text
from reconforge.exporters import ReportFormat
from reconforge.profiling import DatasetProfile, FieldProfile, ValueKind, profile_to_dict

_CSV_COLUMNS = (
    "source",
    "record_count",
    "field",
    "total_count",
    "null_count",
    "non_null_count",
    "distinct_count",
    "null_values",
    "boolean_values",
    "integer_values",
    "decimal_values",
    "date_values",
    "text_values",
    "min_length",
    "max_length",
    "numeric_minimum",
    "numeric_maximum",
    "date_minimum",
    "date_maximum",
    "top_values",
    "examples",
    "fingerprint",
)


def render_profile_report(
    profile: DatasetProfile,
    format: ReportFormat | str,
) -> str:
    """Render one profile without timestamps, I/O, or environment-dependent data."""

    if not isinstance(profile, DatasetProfile):
        raise TypeError("profile must be a DatasetProfile instance")
    selected = ReportFormat.named(format) if isinstance(format, str) else format
    if not isinstance(selected, ReportFormat):
        raise TypeError("report format must be a ReportFormat or string")
    if selected is ReportFormat.JSON:
        return (
            json.dumps(
                profile_to_dict(profile),
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        )
    if selected is ReportFormat.CSV:
        return _render_csv(profile)
    return _render_html(profile)


def write_profile_report(
    profile: DatasetProfile,
    destination: str | Path,
    format: ReportFormat | str,
) -> Path:
    """Render and atomically write a profile, returning the resolved path."""

    return atomic_write_text(destination, render_profile_report(profile, format))


def _render_csv(profile: DatasetProfile) -> str:
    destination = io.StringIO(newline="")
    writer = csv.DictWriter(destination, fieldnames=_CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    fingerprint = profile_to_dict(profile)["fingerprint"]
    for field in profile.fields:
        row = _field_row(profile, field, str(fingerprint))
        writer.writerow({name: _safe_csv_cell(row[name]) for name in _CSV_COLUMNS})
    return destination.getvalue()


def _field_row(
    profile: DatasetProfile,
    field: FieldProfile,
    fingerprint: str,
) -> dict[str, object]:
    counts = dict(field.kind_counts)
    numeric_minimum, numeric_maximum = field.numeric_range or ("", "")
    date_minimum, date_maximum = field.date_range or ("", "")
    return {
        "source": profile.source,
        "record_count": profile.record_count,
        "field": field.name,
        "total_count": field.total_count,
        "null_count": field.null_count,
        "non_null_count": field.non_null_count,
        "distinct_count": field.distinct_count,
        "null_values": counts[ValueKind.NULL],
        "boolean_values": counts[ValueKind.BOOLEAN],
        "integer_values": counts[ValueKind.INTEGER],
        "decimal_values": counts[ValueKind.DECIMAL],
        "date_values": counts[ValueKind.DATE],
        "text_values": counts[ValueKind.TEXT],
        "min_length": "" if field.min_length is None else field.min_length,
        "max_length": "" if field.max_length is None else field.max_length,
        "numeric_minimum": numeric_minimum,
        "numeric_maximum": numeric_maximum,
        "date_minimum": date_minimum,
        "date_maximum": date_maximum,
        "top_values": _compact_json(
            [{"value": item.value, "count": item.count} for item in field.top_values]
        ),
        "examples": _compact_json(field.examples),
        "fingerprint": fingerprint,
    }


def _render_html(profile: DatasetProfile) -> str:
    payload = profile_to_dict(profile)
    fingerprint = str(payload["fingerprint"])
    headings = "".join(
        f'<th scope="col">{_escape(column.replace("_", " "))}</th>' for column in _CSV_COLUMNS
    )
    rows = "".join(_html_field_row(profile, field, fingerprint) for field in profile.fields)
    canonical = _escape(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReconForge Dataset Profile — {_escape(profile.source)}</title>
<style>
:root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
body {{ max-width: 1280px; margin: 0 auto; padding: 2rem; line-height: 1.45; }}
h1 {{ margin-bottom: .25rem; }}
.metadata {{ display: flex; flex-wrap: wrap; gap: 1rem; padding: 0; }}
.metadata div {{ border: 1px solid #8888; border-radius: .5rem; padding: .6rem .8rem; }}
.metadata dt {{ font-size: .8rem; opacity: .75; }}
.metadata dd {{ margin: 0; font-weight: 650; }}
.table-wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-size: .82rem; }}
th, td {{ border: 1px solid #8886; padding: .45rem; text-align: left; vertical-align: top; }}
th {{ position: sticky; top: 0; background: Canvas; }}
pre {{ overflow: auto; padding: 1rem; border: 1px solid #8886; }}
</style>
</head>
<body>
<main>
<h1>ReconForge Dataset Profile</h1>
<dl class="metadata">
<div><dt>Source</dt><dd>{_escape(profile.source)}</dd></div>
<div><dt>Records</dt><dd>{profile.record_count}</dd></div>
<div><dt>Fields</dt><dd>{len(profile.fields)}</dd></div>
<div><dt>Fingerprint</dt><dd>{_escape(fingerprint)}</dd></div>
</dl>
<h2>Field statistics</h2>
<div class="table-wrap">
<table><thead><tr>{headings}</tr></thead><tbody>{rows}</tbody></table>
</div>
<details><summary>Canonical profile data</summary><pre>{canonical}</pre></details>
</main>
</body>
</html>
"""


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _html_field_row(profile: DatasetProfile, field: FieldProfile, fingerprint: str) -> str:
    row = _field_row(profile, field, fingerprint)
    cells = "".join(f"<td>{_escape(row[column])}</td>" for column in _CSV_COLUMNS)
    return f"<tr>{cells}</tr>"


def _safe_csv_cell(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)
