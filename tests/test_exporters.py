from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from reconforge.batch import BatchPair, BatchPairResult, BatchResult
from reconforge.exporters import (
    ReportFormat,
    infer_report_format,
    render_report,
    write_report,
)
from reconforge.model import Dataset, Record
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.report import batch_result_to_dict, result_to_dict


def make_result(left_value: str = "<script>alert(1)</script>", right_value: str = "=cmd"):
    left = Dataset(
        "left<&.csv",
        ("id", "value"),
        (Record("left<&.csv", 2, {"id": "A", "value": left_value}),),
    )
    right = Dataset(
        "right.csv",
        ("id", "value"),
        (Record("right.csv", 2, {"id": "A", "value": right_value}),),
    )
    return reconcile(left, right, ReconciliationConfig(key_fields=("id",)))


def test_csv_export_is_deterministic_and_has_stable_structural_rows() -> None:
    report = result_to_dict(make_result())

    first = render_report(report, ReportFormat.CSV)
    second = render_report(report, "csv")
    rows = list(csv.DictReader(io.StringIO(first)))

    assert first == second
    assert first.startswith(
        "scope,pair_name,status,issue_type,key,field,left_value,right_value,"
        "side,source,row_numbers,details\n"
    )
    assert [row["issue_type"] for row in rows] == ["summary", "field_difference"]
    assert rows[1] == {
        "scope": "report",
        "pair_name": "",
        "status": "differences_found",
        "issue_type": "field_difference",
        "key": '["A"]',
        "field": "value",
        "left_value": "<script>alert(1)</script>",
        "right_value": "'=cmd",
        "side": "",
        "source": "",
        "row_numbers": "[2,2]",
        "details": "",
    }


def test_html_export_is_self_contained_deterministic_and_escapes_values() -> None:
    report = result_to_dict(make_result())

    html = render_report(report, ReportFormat.HTML)

    assert html == render_report(report, ReportFormat.HTML)
    assert html.startswith('<!doctype html>\n<html lang="en">')
    assert "<style>" in html
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "left&lt;&amp;.csv" not in html  # sources are not part of single reports
    assert "https://" not in html
    assert "recorded_at" not in html


def test_batch_csv_contains_summary_pair_and_unmatched_rows(tmp_path: Path) -> None:
    result = make_result("same", "same")
    batch = BatchResult(
        (
            BatchPairResult(
                BatchPair("orders", tmp_path / "left.csv", tmp_path / "right.csv"),
                result,
            ),
        ),
        unmatched_left=(tmp_path / "left-only.csv",),
    )
    report = batch_result_to_dict(
        batch,
        execution={
            "checkpoint": str(tmp_path / "state.json"),
            "executed_pairs": [],
            "reused_pairs": ["orders"],
        },
    )

    rows = list(csv.DictReader(io.StringIO(render_report(report, "csv"))))

    assert [(row["scope"], row["pair_name"], row["issue_type"]) for row in rows] == [
        ("batch", "", "summary"),
        ("batch", "", "batch_execution"),
        ("batch", "", "unmatched_left"),
        ("pair", "orders", "summary"),
    ]
    assert rows[2]["source"] == str(tmp_path / "left-only.csv")
    assert "orders" in render_report(report, "html")


def test_write_report_is_atomic_and_uses_utf8(tmp_path: Path) -> None:
    report = result_to_dict(make_result("café", "قيمة"))
    destination = tmp_path / "nested" / "report.html"

    returned = write_report(report, destination, ReportFormat.HTML)

    assert returned == destination.resolve()
    assert "café" in destination.read_text(encoding="utf-8")
    assert not destination.with_name(f".{destination.name}.tmp").exists()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("report.json", ReportFormat.JSON),
        ("report.csv", ReportFormat.CSV),
        ("report.HTML", ReportFormat.HTML),
        ("report.htm", ReportFormat.HTML),
        ("report.txt", None),
    ],
)
def test_report_format_inference(name: str, expected: ReportFormat | None) -> None:
    assert infer_report_format(name) is expected


def test_json_export_remains_the_canonical_pretty_model() -> None:
    report = result_to_dict(make_result())

    rendered = render_report(report, "json")

    assert json.loads(rendered) == report
    assert rendered.endswith("\n")


def test_unknown_report_format_has_stable_choices() -> None:
    with pytest.raises(ValueError, match="expected one of: json, csv, html"):
        render_report(result_to_dict(make_result()), "pdf")
