"""Deterministic exporters for paginated result investigations."""

from __future__ import annotations

import csv
import html
import io
import json
from contextlib import suppress
from pathlib import Path

from reconforge.exporters import ReportFormat
from reconforge.query import ResultPage, SourceRow, result_page_to_dict

_CSV_COLUMNS = ("kind", "key", "field", "source_rows", "details")


def render_result_page(page: ResultPage, format: ReportFormat | str) -> str:
    """Render an investigation page in a supported deterministic format."""

    selected = ReportFormat.named(format) if isinstance(format, str) else format
    if not isinstance(selected, ReportFormat):
        raise TypeError("result page format must be a ReportFormat or string")
    if selected is ReportFormat.JSON:
        return json.dumps(result_page_to_dict(page), indent=2, ensure_ascii=False) + "\n"
    if selected is ReportFormat.CSV:
        return render_result_page_csv(page)
    return render_result_page_html(page)


def write_result_page(
    page: ResultPage,
    destination: str | Path,
    format: ReportFormat | str,
) -> Path:
    """Render and atomically write a result investigation page."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    payload = render_result_page(page, format)
    try:
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        temporary.replace(path)
    except OSError:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
    return path.resolve()


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
            "source_rows": _source_rows_json(issue.source_rows),
            "details": _compact_json(dict(issue.details)),
        }
        writer.writerow({name: _safe_csv_cell(row[name]) for name in _CSV_COLUMNS})
    return destination.getvalue()


def render_result_page_html(page: ResultPage) -> str:
    """Render one result investigation page as a self-contained HTML document."""

    if not isinstance(page, ResultPage):
        raise TypeError("result page must be a ResultPage instance")
    rows = "".join(
        "<tr>"
        f"<td>{_escape(issue.kind.value)}</td>"
        f"<td>{_escape('' if issue.key is None else _compact_json(list(issue.key)))}</td>"
        f"<td>{_escape(issue.field)}</td>"
        f"<td>{_escape(_source_rows_json(issue.source_rows))}</td>"
        f"<td>{_escape(_compact_json(dict(issue.details)))}</td>"
        "</tr>"
        for issue in page.issues
    )
    if not rows:
        rows = '<tr><td colspan="5">No issues on this page.</td></tr>'
    facets = result_page_to_dict(page)["facets"]
    canonical = _escape(
        json.dumps(result_page_to_dict(page), indent=2, ensure_ascii=False, sort_keys=True)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReconForge result investigation</title>
<style>
:root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
body {{ max-width: 1180px; margin: 0 auto; padding: 2rem; line-height: 1.45; }}
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
<h1>ReconForge result investigation</h1>
<dl class="metrics">
<div class="metric"><dt>Total issues</dt><dd>{page.total_issues}</dd></div>
<div class="metric"><dt>Matching issues</dt><dd>{page.matched_issues}</dd></div>
<div class="metric"><dt>Returned issues</dt><dd>{page.returned_issues}</dd></div>
</dl>
<p>Facet counts: <code>{_escape(_compact_json(facets))}</code></p>
<h2>Issues</h2>
<div class="table-wrap"><table>
<thead><tr><th>kind</th><th>key</th><th>field</th><th>source rows</th><th>details</th></tr></thead>
<tbody>{rows}</tbody>
</table></div>
<details><summary>Investigation data</summary><pre>{canonical}</pre></details>
</main>
</body>
</html>
"""


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_rows_json(source_rows: tuple[SourceRow, ...]) -> str:
    return _compact_json([{"side": row.side, "row_number": row.row_number} for row in source_rows])


def _safe_csv_cell(value: str) -> str:
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value


def _escape(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)
