from __future__ import annotations

import json
from pathlib import Path

from reconforge.model import Dataset, Record
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.report import result_to_dict, write_json_report


def make_result():
    left = Dataset(
        source="left.csv",
        headers=("id", "amount"),
        records=(Record("left.csv", 2, {"id": "A", "amount": "10"}),),
    )
    right = Dataset(
        source="right.csv",
        headers=("id", "amount"),
        records=(Record("right.csv", 2, {"id": "A", "amount": "11"}),),
    )
    return reconcile(left, right, ReconciliationConfig(key_fields=("id",)))


def test_report_structure_contains_version_status_and_summary() -> None:
    report = result_to_dict(make_result())

    assert report["report_version"] == 1
    assert report["status"] == "differences_found"
    assert report["summary"] == {
        "matched_records": 1,
        "discrepancies": 1,
        "field_differences": 1,
        "missing_from_left": 0,
        "missing_from_right": 0,
        "duplicate_keys_left": 0,
        "duplicate_keys_right": 0,
    }
    assert report["differences"][0]["field"] == "amount"
    assert report["configuration"]["ignored_fields"] == []
    assert report["configuration"]["case_sensitive_keys"] is True
    assert report["configuration"]["field_normalizers"] == {}
    assert report["configuration"]["comparison_policies"] == {}


def test_json_report_is_written_with_a_trailing_newline(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "report.json"

    returned = write_json_report(make_result(), destination)

    assert returned == destination.resolve()
    content = destination.read_text(encoding="utf-8")
    assert content.endswith("\n")
    assert json.loads(content)["status"] == "differences_found"
    assert not destination.with_name("report.json.tmp").exists()
