from __future__ import annotations

import csv
import io

import pytest

from reconforge.model import Dataset, Record
from reconforge.query import ResultQuery, query_result
from reconforge.query_exporters import render_result_page_csv, render_result_page_html
from reconforge.reconcile import ReconciliationConfig, reconcile


def make_page(field: str = "=amount"):
    left = Dataset(
        "left.csv",
        ("id", field),
        (Record("left.csv", 2, {"id": "A", field: "café"}),),
    )
    right = Dataset(
        "right.csv",
        ("id", field),
        (Record("right.csv", 2, {"id": "A", field: "قيمة"}),),
    )
    result = reconcile(left, right, ReconciliationConfig(key_fields=("id",)))
    return query_result(result, ResultQuery())


def test_csv_result_page_export_is_deterministic_and_formula_safe() -> None:
    page = make_page()

    rendered = render_result_page_csv(page)
    rows = list(csv.DictReader(io.StringIO(rendered)))

    assert rendered == render_result_page_csv(page)
    assert rendered.startswith("kind,key,field,source_rows,details\n")
    assert rows == [
        {
            "kind": "field_difference",
            "key": '["A"]',
            "field": "'=amount",
            "source_rows": (
                '[{"row_number":2,"side":"left"},{"row_number":2,"side":"right"}]'
            ),
            "details": '{"left_value":"café","right_value":"قيمة"}',
        }
    ]


def test_csv_result_page_export_validates_its_public_input() -> None:
    with pytest.raises(TypeError, match="must be a ResultPage"):
        render_result_page_csv(object())  # type: ignore[arg-type]


def test_html_result_page_export_is_self_contained_deterministic_and_escaped() -> None:
    page = make_page("<script>alert(1)</script>")

    rendered = render_result_page_html(page)

    assert rendered == render_result_page_html(page)
    assert rendered.startswith('<!doctype html>\n<html lang="en">')
    assert "<style>" in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "Matching issues</dt><dd>1" in rendered
    assert "https://" not in rendered
    assert "generated_at" not in rendered


def test_html_result_page_export_validates_its_public_input() -> None:
    with pytest.raises(TypeError, match="must be a ResultPage"):
        render_result_page_html(object())  # type: ignore[arg-type]
