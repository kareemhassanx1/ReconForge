from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from reconforge.errors import InputError
from reconforge.model import Dataset, Record
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.report import (
    load_json_report,
    result_fingerprint,
    result_from_dict,
    result_to_dict,
    write_json_report,
)
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


def make_status_result(*, difference: bool, violation: bool):
    left = Dataset(
        "left.csv",
        ("id", "value", "status"),
        (
            Record(
                "left.csv",
                2,
                {"id": "A", "value": "old", "status": None if violation else "ok"},
            ),
        ),
    )
    right = Dataset(
        "right.csv",
        ("id", "value", "status"),
        (
            Record(
                "right.csv",
                2,
                {"id": "A", "value": "new" if difference else "old", "status": "ok"},
            ),
        ),
    )
    return reconcile(
        left,
        right,
        ReconciliationConfig(
            key_fields=("id",),
            compare_fields=("value",),
            validation_rules={"status": (RequiredRule(),)},
        ),
    )


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


def test_report_parser_rejects_boolean_version_alias() -> None:
    report = result_to_dict(make_result())
    report["report_version"] = True

    with pytest.raises(ValueError, match="unsupported report version True"):
        result_from_dict(report)


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


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda config: config.update(key_fields=[]),
            "key_fields must contain at least one field",
        ),
        (
            lambda config: config.update(key_fields=["id", "id"]),
            "key_fields must contain unique fields",
        ),
        (
            lambda config: config.update(compared_fields=["amount", "amount"]),
            "compared_fields must contain unique fields",
        ),
        (
            lambda config: config.update(ignored_fields=["note", "note"]),
            "ignored_fields must contain unique fields",
        ),
        (
            lambda config: config.update(compared_fields=["id"]),
            "key_fields and compared_fields must not overlap",
        ),
        (
            lambda config: config.update(ignored_fields=["id"]),
            "key_fields and ignored_fields must not overlap",
        ),
        (
            lambda config: config.update(ignored_fields=["amount"]),
            "compared_fields and ignored_fields must not overlap",
        ),
        (
            lambda config: config["field_normalizers"].update(note="text"),
            "field_normalizers contains fields outside",
        ),
        (
            lambda config: config["comparison_policies"].update(id="exact"),
            "comparison_policies contains fields outside",
        ),
        (
            lambda config: config["validation_rules"].update(amount=[]),
            "validation_rules entries must not be empty",
        ),
    ],
)
def test_report_configuration_rejects_invalid_scope(mutator, message: str) -> None:
    report = result_to_dict(make_result())
    mutator(report["configuration"])

    with pytest.raises(ValueError, match=message):
        result_from_dict(report)


@pytest.mark.parametrize(
    ("section", "descriptor", "message"),
    [
        ("field_normalizers", "money", "unknown normalizer type"),
        ("field_normalizers", " Decimal ", "canonical descriptor"),
        ("comparison_policies", "decimal:tolerance=-1", "finite non-negative"),
        ("comparison_policies", "decimal:tolerance=NaN", "finite non-negative"),
        ("comparison_policies", "decimal:tolerance=1.0", "canonical descriptor"),
    ],
)
def test_report_rejects_invalid_recorded_normalizer_and_policy_descriptors(
    section: str, descriptor: str, message: str
) -> None:
    report = result_to_dict(make_result())
    report["configuration"][section]["amount"] = descriptor

    with pytest.raises(ValueError, match=message):
        result_from_dict(report)


@pytest.mark.parametrize(
    ("descriptor", "message"),
    [
        ("unknown", "unknown validation rule descriptor"),
        ("allowed:not-json", "Expecting value"),
        ('allowed:["A","A"]', "allowed values must be unique"),
        ("numeric_range:[0,+inf]", "unbounded maximum must be exclusive"),
        ("numeric_range:[10,0]", "minimum cannot exceed maximum"),
        ("numeric_range:[NaN,10]", "finite decimal"),
        ("pattern:[", "unterminated character set"),
    ],
)
def test_report_rejects_invalid_recorded_validation_descriptors(
    descriptor: str, message: str
) -> None:
    report = result_to_dict(make_result())
    report["configuration"]["validation_rules"]["amount"] = [descriptor]

    with pytest.raises(ValueError, match=message):
        result_from_dict(report)


def test_report_accepts_every_canonical_recorded_descriptor() -> None:
    report = result_to_dict(make_result())
    report["configuration"]["field_normalizers"] = {"id": "text", "amount": "decimal"}
    report["configuration"]["comparison_policies"] = {"amount": "decimal:tolerance=0.01"}
    report["configuration"]["validation_rules"] = {
        "amount": [
            "required",
            'allowed:["10","11"]',
            "numeric_range:[0,100)",
            r"pattern:\d+",
        ]
    }

    loaded = result_from_dict(report)

    assert dict(loaded.field_normalizers) == {"amount": "decimal", "id": "text"}
    assert dict(loaded.comparison_policies) == {"amount": "decimal:tolerance=0.01"}
    assert dict(loaded.validation_rules)["amount"][-1] == r"pattern:\d+"


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda report: report["differences"][0].update(key=[]),
            r"differences\[0\].key must contain 1 component",
        ),
        (
            lambda report: report["differences"][0].update(field="note"),
            "is not a compared field",
        ),
        (
            lambda report: report["missing_records"]["from_left"].append(
                {"key": ["B", "extra"], "right_row": 3}
            ),
            r"from_left\[0\].key must contain 1 component",
        ),
        (
            lambda report: report["duplicate_keys"]["left"].append(
                {"key": ["B"], "row_numbers": [3]}
            ),
            "row_numbers must contain at least two rows",
        ),
        (
            lambda report: report["duplicate_keys"]["right"].append(
                {"key": ["B"], "row_numbers": [3, 3]}
            ),
            "row_numbers must contain unique rows",
        ),
        (
            lambda report: report["schema"]["fields_only_left"].append("id"),
            "fields_only_left contains configured fields",
        ),
    ],
)
def test_report_rejects_issues_outside_configuration(mutator, message: str) -> None:
    report = result_to_dict(make_result())
    mutator(report)

    with pytest.raises(ValueError, match=message):
        result_from_dict(report)


@pytest.mark.parametrize(
    ("side", "field", "rule", "message"),
    [
        ("center", "amount", "required", "side must be 'left' or 'right'"),
        ("left", "note", "required", "has no configured validation rules"),
        ("right", "amount", "allowed:[]", "is not configured for field"),
    ],
)
def test_report_rejects_validation_issues_without_declared_rule(
    side: str, field: str, rule: str, message: str
) -> None:
    report = result_to_dict(make_result())
    report["configuration"]["validation_rules"] = {"amount": ["required"]}
    report["validation_violations"] = [
        {
            "side": side,
            "source": "left.csv",
            "row_number": 2,
            "field": field,
            "value": None,
            "rule": rule,
            "message": "value is required",
        }
    ]

    with pytest.raises(ValueError, match=message):
        result_from_dict(report)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda report: report["differences"].append(deepcopy(report["differences"][0])),
            r"differences\[1\] duplicates the identity",
        ),
        (
            lambda report: report["missing_records"]["from_left"].extend(
                [
                    {"key": ["B"], "right_row": 3},
                    {"key": ["B"], "right_row": 4},
                ]
            ),
            r"from_left\[1\] duplicates the identity",
        ),
        (
            lambda report: report["duplicate_keys"]["left"].extend(
                [
                    {"key": ["B"], "row_numbers": [3, 4]},
                    {"key": ["B"], "row_numbers": [5, 6]},
                ]
            ),
            r"duplicate_keys.left\[1\] duplicates the identity",
        ),
        (
            lambda report: report["schema"].update(fields_only_left=["note", "note"]),
            r"fields_only_left\[1\] duplicates the identity",
        ),
        (
            lambda report: report["schema"].update(
                fields_only_left=["left_note"],
                fields_only_right=["left_note"],
            ),
            "cannot be exclusive to both sides",
        ),
    ],
)
def test_report_rejects_duplicate_issue_identities(mutator, message: str) -> None:
    report = result_to_dict(make_result())
    mutator(report)

    with pytest.raises(ValueError, match=message):
        result_from_dict(report)


def test_report_rejects_duplicate_validation_violations_with_consistent_summary() -> None:
    report = result_to_dict(make_status_result(difference=False, violation=True))
    report["validation_violations"].append(deepcopy(report["validation_violations"][0]))
    report["summary"]["validation_violations"] = 2
    report["summary"]["issues"] = 2

    with pytest.raises(
        ValueError,
        match=r"validation_violations\[1\] duplicates the identity.*violations\[0\]",
    ):
        result_from_dict(report)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda report: report["missing_records"]["from_left"].append(
            {"key": ["A"], "right_row": 3}
        ),
        lambda report: (
            report["missing_records"]["from_left"].append({"key": ["B"], "right_row": 3}),
            report["missing_records"]["from_right"].append({"key": ["B"], "left_row": 3}),
        ),
        lambda report: report["duplicate_keys"]["left"].append(
            {"key": ["A"], "row_numbers": [2, 3]}
        ),
    ],
)
def test_report_rejects_contradictory_key_classifications(mutator) -> None:
    report = result_to_dict(make_result())
    mutator(report)

    with pytest.raises(ValueError, match="contradictory classifications for key"):
        result_from_dict(report)


@pytest.mark.parametrize(
    "field",
    [
        "discrepancies",
        "validation_violations",
        "issues",
        "field_differences",
        "missing_from_left",
        "missing_from_right",
        "duplicate_keys_left",
        "duplicate_keys_right",
    ],
)
def test_report_recomputes_every_derived_summary_count(field: str) -> None:
    report = result_to_dict(make_result())
    report["summary"][field] += 1

    with pytest.raises(ValueError, match=rf"report.summary.{field} is .* expected .* content"):
        result_from_dict(report)


def test_report_requires_all_summary_counts_to_be_non_negative_integers() -> None:
    report = result_to_dict(make_result())
    report["summary"]["missing_from_left"] = "0"

    with pytest.raises(ValueError, match="missing_from_left must be a non-negative integer"):
        result_from_dict(report)


@pytest.mark.parametrize(
    ("difference", "violation", "status"),
    [
        (False, False, "clean"),
        (True, False, "differences_found"),
        (False, True, "validation_violations_found"),
        (True, True, "differences_and_validation_violations_found"),
    ],
)
def test_report_accepts_all_derived_statuses(
    difference: bool, violation: bool, status: str
) -> None:
    result = make_status_result(difference=difference, violation=violation)
    report = result_to_dict(result)

    assert report["status"] == status
    assert result_from_dict(report) == result


@pytest.mark.parametrize("status", ["clean", "issues_found", "", 1, None])
def test_report_rejects_status_that_does_not_match_content(status: object) -> None:
    report = result_to_dict(make_result())
    report["status"] = status

    with pytest.raises(ValueError, match="report.status"):
        result_from_dict(report)


def test_result_fingerprint_is_stable_and_content_sensitive() -> None:
    original = make_status_result(difference=True, violation=False)
    same = make_status_result(difference=True, violation=False)
    changed = make_status_result(difference=False, violation=False)

    fingerprint = result_fingerprint(original)

    assert fingerprint == result_fingerprint(same)
    assert fingerprint != result_fingerprint(changed)
    assert fingerprint.startswith("sha256:")
    assert len(fingerprint) == len("sha256:") + 64


def test_result_fingerprint_ignores_json_layout_key_order_and_bom(tmp_path: Path) -> None:
    result = make_result()
    report = result_to_dict(result)
    compact = tmp_path / "compact.json"
    pretty = tmp_path / "pretty.json"
    compact.write_text(
        "\ufeff" + json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    pretty.write_text(json.dumps(report, ensure_ascii=False, indent=4), encoding="utf-8")

    assert result_fingerprint(load_json_report(compact)) == result_fingerprint(
        load_json_report(pretty)
    )


def test_result_fingerprint_handles_unicode_deterministically() -> None:
    left = Dataset(
        "يسار.csv",
        ("id", "value"),
        (Record("يسار.csv", 2, {"id": "أ", "value": "قديم"}),),
    )
    right = Dataset(
        "يمين.csv",
        ("id", "value"),
        (Record("يمين.csv", 2, {"id": "أ", "value": "جديد"}),),
    )
    result = reconcile(left, right, ReconciliationConfig(key_fields=("id",)))

    assert result_fingerprint(result) == result_fingerprint(result)


def test_result_fingerprint_validates_public_input() -> None:
    with pytest.raises(TypeError, match="ReconciliationResult"):
        result_fingerprint(object())  # type: ignore[arg-type]


def test_strict_report_round_trip_accepts_all_issue_kinds() -> None:
    left = Dataset(
        "left.csv",
        ("id", "value", "status", "left_only"),
        (
            Record("left.csv", 2, {"id": "A", "value": "one", "status": "ok", "left_only": "L"}),
            Record("left.csv", 3, {"id": "B", "value": "two", "status": None, "left_only": "L"}),
            Record("left.csv", 4, {"id": "D", "value": "three", "status": "ok", "left_only": "L"}),
            Record("left.csv", 5, {"id": "D", "value": "four", "status": "ok", "left_only": "L"}),
        ),
    )
    right = Dataset(
        "right.csv",
        ("id", "value", "status", "right_only"),
        (
            Record(
                "right.csv", 2, {"id": "A", "value": "changed", "status": "ok", "right_only": "R"}
            ),
            Record("right.csv", 3, {"id": "C", "value": "two", "status": "ok", "right_only": "R"}),
            Record(
                "right.csv", 4, {"id": "E", "value": "three", "status": "ok", "right_only": "R"}
            ),
            Record("right.csv", 5, {"id": "E", "value": "four", "status": "ok", "right_only": "R"}),
        ),
    )
    result = reconcile(
        left,
        right,
        ReconciliationConfig(
            key_fields=("id",),
            validation_rules={"status": (RequiredRule(),)},
        ),
    )

    restored = result_from_dict(result_to_dict(result))

    assert restored == result
    assert restored.issue_count == 8


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
