from __future__ import annotations

import pytest

from reconforge.model import Dataset, Record
from reconforge.query import result_issues
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.result_delta import DeltaIssue, DeltaStatus, compare_results


def make_result(left_ids: tuple[str, ...], right_ids: tuple[str, ...]):
    left = Dataset(
        "left.csv",
        ("id", "value"),
        tuple(
            Record("left.csv", index + 2, {"id": key, "value": f"left-{key}"})
            for index, key in enumerate(left_ids)
        ),
    )
    right = Dataset(
        "right.csv",
        ("id", "value"),
        tuple(
            Record("right.csv", index + 2, {"id": key, "value": f"right-{key}"})
            for index, key in enumerate(right_ids)
        ),
    )
    return reconcile(left, right, ReconciliationConfig(key_fields=("id",)))


def test_compare_results_classifies_added_resolved_and_unchanged_issues() -> None:
    baseline = make_result(("A", "B"), ("A", "C"))
    current = make_result(("A", "B", "D"), ("A", "C", "E"))

    delta = compare_results(baseline, current)

    assert delta.baseline_total == 3
    assert delta.current_total == 5
    assert len(delta.unchanged) == 3
    assert [item.issue.key for item in delta.added] == [("E",), ("D",)]
    assert delta.resolved == ()
    assert delta.changed_count == 2
    assert [item.status for item in delta.issues] == [
        DeltaStatus.UNCHANGED,
        DeltaStatus.UNCHANGED,
        DeltaStatus.ADDED,
        DeltaStatus.UNCHANGED,
        DeltaStatus.ADDED,
    ]


def test_compare_results_reports_resolved_issues_in_baseline_order() -> None:
    baseline = make_result(("A", "B", "D"), ("A", "C", "E"))
    current = make_result(("A",), ("A",))

    delta = compare_results(baseline, current)

    assert [item.issue.key for item in delta.resolved] == [("C",), ("E",), ("B",), ("D",)]
    assert delta.changed_count == 4


def test_compare_results_never_mutates_source_results() -> None:
    baseline = make_result(("A", "B"), ("A", "C"))
    current = make_result(("A", "D"), ("A", "E"))
    baseline_issues = result_issues(baseline)
    current_issues = result_issues(current)

    compare_results(baseline, current)

    assert result_issues(baseline) == baseline_issues
    assert result_issues(current) == current_issues


def test_delta_public_objects_validate_inconsistent_states() -> None:
    issue = result_issues(make_result(("A",), ("A",)))[0]

    with pytest.raises(TypeError, match="DeltaStatus"):
        DeltaIssue("added", None, issue)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="only a current"):
        DeltaIssue(DeltaStatus.ADDED, issue, issue)
    with pytest.raises(ValueError, match="only a baseline"):
        DeltaIssue(DeltaStatus.RESOLVED, issue, issue)
    with pytest.raises(TypeError, match="baseline must be"):
        compare_results(object(), make_result(("A",), ("A",)))  # type: ignore[arg-type]
