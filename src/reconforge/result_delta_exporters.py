"""Deterministic exporters for paginated result deltas."""

from __future__ import annotations

import csv
import io
import json

from reconforge.query import ResultIssue, issue_fingerprint
from reconforge.result_delta import DeltaPage

_CSV_COLUMNS = (
    "status",
    "kind",
    "key",
    "field",
    "baseline_fingerprint",
    "current_fingerprint",
    "baseline_source_rows",
    "current_source_rows",
    "baseline_details",
    "current_details",
)


def render_delta_page_csv(page: DeltaPage) -> str:
    """Render one result-delta page as formula-safe CSV."""

    if not isinstance(page, DeltaPage):
        raise TypeError("delta page must be a DeltaPage instance")
    destination = io.StringIO(newline="")
    writer = csv.DictWriter(destination, fieldnames=_CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for item in page.issues:
        issue = item.issue
        row = {
            "status": item.status.value,
            "kind": issue.kind.value,
            "key": "" if issue.key is None else _compact_json(list(issue.key)),
            "field": issue.field or "",
            "baseline_fingerprint": _fingerprint(item.baseline),
            "current_fingerprint": _fingerprint(item.current),
            "baseline_source_rows": _source_rows(item.baseline),
            "current_source_rows": _source_rows(item.current),
            "baseline_details": _details(item.baseline),
            "current_details": _details(item.current),
        }
        writer.writerow({column: _safe_csv_cell(row[column]) for column in _CSV_COLUMNS})
    return destination.getvalue()


def _fingerprint(issue: ResultIssue | None) -> str:
    return "" if issue is None else issue_fingerprint(issue)


def _source_rows(issue: ResultIssue | None) -> str:
    if issue is None:
        return ""
    return _compact_json(
        [{"side": row.side, "row_number": row.row_number} for row in issue.source_rows]
    )


def _details(issue: ResultIssue | None) -> str:
    return "" if issue is None else _compact_json(dict(issue.details))


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_csv_cell(value: str) -> str:
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value
