"""Deterministic exporters for paginated result investigations."""

from __future__ import annotations

import csv
import io
import json

from reconforge.query import ResultPage

_CSV_COLUMNS = ("kind", "key", "field", "source_rows", "details")


def render_result_page_csv(page: ResultPage) -> str:
    """Render one result investigation page as formula-safe CSV."""

    if not isinstance(page, ResultPage):
        raise TypeError("result page must be a ResultPage instance")
    destination = io.StringIO(newline="")
    writer = csv.DictWriter(destination, fieldnames=_CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for issue in page.issues:
        row = {
            "kind": issue.kind.value,
            "key": "" if issue.key is None else _compact_json(list(issue.key)),
            "field": issue.field or "",
            "source_rows": _compact_json(
                [
                    {"side": source_row.side, "row_number": source_row.row_number}
                    for source_row in issue.source_rows
                ]
            ),
            "details": _compact_json(dict(issue.details)),
        }
        writer.writerow({name: _safe_csv_cell(row[name]) for name in _CSV_COLUMNS})
    return destination.getvalue()


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_csv_cell(value: str) -> str:
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value
