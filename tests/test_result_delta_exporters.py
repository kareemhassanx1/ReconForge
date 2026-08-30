from __future__ import annotations

import csv
import io

import pytest
from test_result_delta import make_moved_result

from reconforge.result_delta import compare_results, query_delta
from reconforge.result_delta_exporters import render_delta_page_csv


def test_delta_csv_is_deterministic_structural_and_formula_safe() -> None:
    page = query_delta(
        compare_results(
            make_moved_result(0, "=amount"),
            make_moved_result(2, "=amount"),
        )
    )

    rendered = render_delta_page_csv(page)
    rows = list(csv.DictReader(io.StringIO(rendered)))

    assert rendered == render_delta_page_csv(page)
    assert rendered.startswith(
        "status,kind,key,field,baseline_fingerprint,current_fingerprint,"
        "baseline_source_rows,current_source_rows,baseline_details,current_details\n"
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "moved"
    assert rows[0]["kind"] == "field_difference"
    assert rows[0]["key"] == '["A"]'
    assert rows[0]["field"] == "'=amount"
    assert rows[0]["baseline_source_rows"] == (
        '[{"row_number":2,"side":"left"},{"row_number":2,"side":"right"}]'
    )
    assert rows[0]["current_source_rows"] == (
        '[{"row_number":4,"side":"left"},{"row_number":4,"side":"right"}]'
    )


def test_delta_csv_validates_public_input() -> None:
    with pytest.raises(TypeError, match="must be a DeltaPage"):
        render_delta_page_csv(object())  # type: ignore[arg-type]
