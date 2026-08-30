from __future__ import annotations

import csv
import io

import pytest

from reconforge.model import Dataset, Record
from reconforge.query import ResultQuery, query_result
from reconforge.query_exporters import render_result_page_csv
from reconforge.reconcile import ReconciliationConfig, reconcile


def make_page():
    left = Dataset(
        "left.csv",
        ("id", "=amount"),
        (Record("left.csv", 2, {"id": "A", "=amount": "café"}),),
    )
    right = Dataset(
        "right.csv",
        ("id", "=amount"),
        (Record("right.csv", 2, {"id": "A", "=amount": "قيمة"}),),
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
