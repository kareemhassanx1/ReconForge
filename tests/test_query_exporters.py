from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from reconforge.model import Dataset, Record
from reconforge.query import ResultQuery, query_result, result_page_to_dict
from reconforge.query_exporters import (
    render_result_page,
    render_result_page_csv,
    render_result_page_html,
    write_result_page,
)
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
            "source_rows": ('[{"row_number":2,"side":"left"},{"row_number":2,"side":"right"}]'),
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


def test_result_page_renderer_dispatches_json_and_rejects_unknown_formats() -> None:
    page = make_page()

    rendered = render_result_page(page, "json")

    assert json.loads(rendered) == result_page_to_dict(page)
    assert rendered.endswith("\n")
    with pytest.raises(ValueError, match="expected one of: json, csv, html"):
        render_result_page(page, "pdf")


def test_write_result_page_is_atomic_and_preserves_unicode(tmp_path: Path) -> None:
    page = make_page()
    destination = tmp_path / "nested" / "investigation.html"

    returned = write_result_page(page, destination, "html")

    assert returned == destination.resolve()
    assert "café" in destination.read_text(encoding="utf-8")
    assert not destination.with_name(f".{destination.name}.tmp").exists()
