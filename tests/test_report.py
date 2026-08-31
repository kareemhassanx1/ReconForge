from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from reconforge.errors import InputError
from reconforge.model import Dataset, Record
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.report import load_json_report, result_from_dict, result_to_dict, write_json_report
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


def mutate_report(path: tuple[str | int, ...], *, remove: bool) -> dict[str, object]:
    report = deepcopy(result_to_dict(make_result()))
    target: object = report
    for component in path[:-1]:
        target = target[component]  # type: ignore[index]
    final = path[-1]
    if remove:
        del target[final]  # type: ignore[index]
    else:
        target[final] = "unexpected"  # type: ignore[index]
    return report


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


@pytest.mark.parametrize(
    "content",
    [
        '{"report_version":1,"report_version":1}',
        '{"report_version":1,"summary":{"issues":1,"issues":2}}',
    ],
)
def test_json_report_rejects_duplicate_object_keys(tmp_path: Path, content: str) -> None:
    source = tmp_path / "duplicate.json"
    source.write_text(content, encoding="utf-8")

    with pytest.raises(InputError, match="duplicate object key"):
        load_json_report(source)


def test_json_report_loader_keeps_bom_compatibility(tmp_path: Path) -> None:
    source = tmp_path / "bom.json"
    source.write_text(
        "\ufeff" + json.dumps(result_to_dict(make_result()), ensure_ascii=False),
        encoding="utf-8",
    )

    assert load_json_report(source) == make_result()


@pytest.mark.parametrize(
    ("path", "location"),
    [
        (("extra",), "report"),
        (("summary", "extra"), "report.summary"),
        (("configuration", "extra"), "report.configuration"),
        (("schema", "extra"), "report.schema"),
        (("missing_records", "extra"), "report.missing_records"),
        (("duplicate_keys", "extra"), "report.duplicate_keys"),
        (("differences", 0, "extra"), r"report.differences\[0\]"),
    ],
)
def test_report_shape_rejects_unknown_fields(path: tuple[str | int, ...], location: str) -> None:
    with pytest.raises(ValueError, match=rf"{location} contains unknown field"):
        result_from_dict(mutate_report(path, remove=False))


@pytest.mark.parametrize(
    ("path", "location"),
    [
        (("status",), "report"),
        (("summary", "issues"), "report.summary"),
        (("configuration", "key_fields"), "report.configuration"),
        (("schema", "fields_only_left"), "report.schema"),
        (("missing_records", "from_left"), "report.missing_records"),
        (("duplicate_keys", "left"), "report.duplicate_keys"),
        (("differences", 0, "left_row"), r"report.differences\[0\]"),
    ],
)
def test_report_shape_rejects_missing_fields(path: tuple[str | int, ...], location: str) -> None:
    with pytest.raises(ValueError, match=rf"{location} is missing required field"):
        result_from_dict(mutate_report(path, remove=True))


@pytest.mark.parametrize(
    ("section", "side", "item", "location"),
    [
        (
            "validation_violations",
            None,
            {
                "side": "left",
                "source": "left.csv",
                "row_number": 2,
                "field": "amount",
                "value": None,
                "rule": "required",
                "message": "missing",
                "extra": True,
            },
            r"report.validation_violations\[0\]",
        ),
        (
            "missing_records",
            "from_left",
            {"key": ["B"], "right_row": 3, "extra": True},
            r"report.missing_records.from_left\[0\]",
        ),
        (
            "duplicate_keys",
            "left",
            {"key": ["B"], "row_numbers": [3, 4], "extra": True},
            r"report.duplicate_keys.left\[0\]",
        ),
    ],
)
def test_report_shape_checks_every_issue_object(
    section: str,
    side: str | None,
    item: dict[str, object],
    location: str,
) -> None:
    report = result_to_dict(make_result())
    collection = report[section] if side is None else report[section][side]
    collection.append(item)

    with pytest.raises(ValueError, match=rf"{location} contains unknown field"):
        result_from_dict(report)


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
