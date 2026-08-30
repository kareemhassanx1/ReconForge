from __future__ import annotations

import json
from pathlib import Path

from reconforge.model import Dataset, Record
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.report import load_json_report, result_to_dict, write_json_report
from reconforge.validation import RequiredRule


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
        "validation_violations": 0,
        "issues": 1,
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
    assert report["configuration"]["validation_rules"] == {}
    assert report["validation_violations"] == []


def test_json_report_is_written_with_a_trailing_newline(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "report.json"

    returned = write_json_report(make_result(), destination)

    assert returned == destination.resolve()
    content = destination.read_text(encoding="utf-8")
    assert content.endswith("\n")
    assert json.loads(content)["status"] == "differences_found"
    assert not destination.with_name("report.json.tmp").exists()


def test_json_report_can_be_loaded_back_for_investigation(tmp_path: Path) -> None:
    destination = tmp_path / "report.json"
    original = make_result()
    write_json_report(original, destination)

    loaded = load_json_report(destination)

    assert loaded == original


def test_report_separates_validation_only_status_and_context() -> None:
    left = Dataset(
        source="left.csv",
        headers=("id", "status"),
        records=(Record("left.csv", 2, {"id": "A", "status": None}),),
    )
    right = Dataset(
        source="right.csv",
        headers=("id", "status"),
        records=(Record("right.csv", 2, {"id": "A", "status": None}),),
    )
    result = reconcile(
        left,
        right,
        ReconciliationConfig(
            key_fields=("id",),
            validation_rules={"status": (RequiredRule(),)},
        ),
    )

    report = result_to_dict(result)

    assert report["status"] == "validation_violations_found"
    assert report["summary"]["discrepancies"] == 0
    assert report["summary"]["validation_violations"] == 2
    assert report["summary"]["issues"] == 2
    assert report["configuration"]["validation_rules"] == {"status": ["required"]}
    assert report["differences"] == []
    assert report["validation_violations"][0] == {
        "side": "left",
        "source": "left.csv",
        "row_number": 2,
        "field": "status",
        "value": None,
        "rule": "required",
        "message": "value is required",
    }


def test_report_has_combined_status_for_differences_and_validation() -> None:
    left = Dataset(
        source="left.csv",
        headers=("id", "status"),
        records=(Record("left.csv", 2, {"id": "A", "status": None}),),
    )
    right = Dataset(
        source="right.csv",
        headers=("id", "status"),
        records=(Record("right.csv", 2, {"id": "A", "status": "paid"}),),
    )
    result = reconcile(
        left,
        right,
        ReconciliationConfig(
            key_fields=("id",),
            validation_rules={"status": (RequiredRule(),)},
        ),
    )

    report = result_to_dict(result)

    assert report["status"] == "differences_and_validation_violations_found"
    assert report["summary"]["discrepancies"] == 1
    assert report["summary"]["validation_violations"] == 1
    assert report["summary"]["issues"] == 2
