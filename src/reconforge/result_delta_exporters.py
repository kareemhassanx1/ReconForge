"""Deterministic exporters for paginated result deltas."""

from __future__ import annotations

import csv
import html
import io
import json

from reconforge.query import ResultIssue, issue_fingerprint
from reconforge.result_delta import DeltaIssue, DeltaPage, delta_page_to_dict

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


def render_delta_page_html(page: DeltaPage) -> str:
    """Render one result-delta page as a self-contained HTML document."""

    if not isinstance(page, DeltaPage):
        raise TypeError("delta page must be a DeltaPage instance")
    rows = "".join(_html_row(item) for item in page.issues)
    if not rows:
        rows = '<tr><td colspan="7">No delta entries on this page.</td></tr>'
    payload = delta_page_to_dict(page)
    facets = _escape(_compact_json(payload["facets"]))
    canonical = _escape(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReconForge result delta</title>
<style>
:root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
body {{ max-width: 1200px; margin: 0 auto; padding: 2rem; line-height: 1.45; }}
.metrics {{ display: flex; flex-wrap: wrap; gap: .75rem; padding: 0; }}
.metric {{ border: 1px solid #8888; border-radius: .5rem; padding: .65rem .85rem; }}
.metric dt {{ font-size: .8rem; opacity: .75; }} .metric dd {{ margin: 0; font-size: 1.25rem; }}
.table-wrap {{ overflow-x: auto; }} table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #8886; padding: .45rem; text-align: left; vertical-align: top; }}
th {{ background: Canvas; }} pre {{ overflow: auto; padding: 1rem; border: 1px solid #8886; }}
</style>
</head>
<body>
<main>
<h1>ReconForge result delta</h1>
<dl class="metrics">
<div class="metric"><dt>Baseline issues</dt><dd>{page.baseline_total}</dd></div>
<div class="metric"><dt>Current issues</dt><dd>{page.current_total}</dd></div>
<div class="metric"><dt>Matching entries</dt><dd>{page.matched_entries}</dd></div>
<div class="metric"><dt>Returned entries</dt><dd>{page.returned_entries}</dd></div>
</dl>
<p>Facet counts: <code>{facets}</code></p>
<h2>Delta entries</h2>
<div class="table-wrap"><table>
<thead><tr><th>status</th><th>kind</th><th>key</th><th>field</th><th>baseline</th><th>current</th><th>details</th></tr></thead>
<tbody>{rows}</tbody>
</table></div>
<details><summary>Canonical delta data</summary><pre>{canonical}</pre></details>
</main>
</body>
</html>
"""


def _html_row(item: DeltaIssue) -> str:
    issue = item.issue
    values = (
        item.status.value,
        issue.kind.value,
        "" if issue.key is None else _compact_json(list(issue.key)),
        issue.field,
        _source_rows(item.baseline),
        _source_rows(item.current),
        _details(item.current if item.current is not None else item.baseline),
    )
    return "<tr>" + "".join(f"<td>{_escape(value)}</td>" for value in values) + "</tr>"


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


def _escape(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)
