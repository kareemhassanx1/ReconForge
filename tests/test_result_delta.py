from __future__ import annotations

import pytest

from reconforge.model import Dataset, Record
from reconforge.query import IssueKind, result_issues
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.result_delta import (
    DeltaIssue,
    DeltaQuery,
    DeltaStatus,
    compare_results,
    filter_delta_issues,
)


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


def make_moved_result(leading_rows: int):
    shared = tuple(
        Record("left.csv", index + 2, {"id": f"S{index}", "value": "same"})
        for index in range(leading_rows)
    )
    left = Dataset(
        "left.csv",
        ("id", "value"),
        (*shared, Record("left.csv", leading_rows + 2, {"id": "A", "value": "old"})),
    )
    right_shared = tuple(
        Record("right.csv", index + 2, {"id": f"S{index}", "value": "same"})
        for index in range(leading_rows)
    )
    right = Dataset(
        "right.csv",
        ("id", "value"),
        (
            *right_shared,
            Record("right.csv", leading_rows + 2, {"id": "A", "value": "new"}),
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


def test_compare_results_classifies_source_row_relocation_as_moved() -> None:
    delta = compare_results(make_moved_result(0), make_moved_result(2))

    assert delta.added == ()
    assert delta.resolved == ()
    assert delta.unchanged == ()
    assert len(delta.moved) == 1
    assert delta.changed_count == 1
    assert delta.moved[0].baseline is not None
    assert delta.moved[0].current is not None
    assert [row.row_number for row in delta.moved[0].baseline.source_rows] == [2, 2]
    assert [row.row_number for row in delta.moved[0].current.source_rows] == [4, 4]


def test_compare_results_never_mutates_source_results() -> None:
    baseline = make_result(("A", "B"), ("A", "C"))
    current = make_result(("A", "D"), ("A", "E"))
    baseline_issues = result_issues(baseline)
    current_issues = result_issues(current)

    compare_results(baseline, current)

    assert result_issues(baseline) == baseline_issues
    assert result_issues(current) == current_issues


def test_delta_filters_use_and_between_dimensions_and_or_within_them() -> None:
    delta = compare_results(
        make_result(("A", "B", "D"), ("A", "C", "E")),
        make_result(("A", "B", "F"), ("A", "C", "G")),
    )

    selected = filter_delta_issues(
        delta,
        DeltaQuery(
            statuses=frozenset({DeltaStatus.ADDED, DeltaStatus.RESOLVED}),
            kinds=frozenset({IssueKind.MISSING_FROM_LEFT}),
            keys=frozenset({("E",), ("G",)}),
        ),
    )
    field_differences = filter_delta_issues(
        delta,
        DeltaQuery(fields=frozenset({"value"})),
    )

    assert [(item.status, item.issue.key) for item in selected] == [
        (DeltaStatus.ADDED, ("G",)),
        (DeltaStatus.RESOLVED, ("E",)),
    ]
    assert [item.status for item in field_differences] == [DeltaStatus.UNCHANGED]
    assert filter_delta_issues(delta) == delta.issues


def test_delta_filters_validate_public_values() -> None:
    with pytest.raises(ValueError, match="unknown delta status.*unchanged"):
        DeltaStatus.named("regressed")
    with pytest.raises(TypeError, match="DeltaStatus"):
        DeltaQuery(statuses=frozenset({"added"}))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="IssueKind"):
        DeltaQuery(kinds=frozenset({"field_difference"}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty strings"):
        DeltaQuery(fields=frozenset({""}))
    with pytest.raises(TypeError, match="ResultDelta"):
        filter_delta_issues(object())  # type: ignore[arg-type]


def test_delta_public_objects_validate_inconsistent_states() -> None:
    issue = result_issues(make_result(("A",), ("A",)))[0]

    with pytest.raises(TypeError, match="DeltaStatus"):
        DeltaIssue("added", None, issue)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="only a current"):
        DeltaIssue(DeltaStatus.ADDED, issue, issue)
    with pytest.raises(ValueError, match="only a baseline"):
        DeltaIssue(DeltaStatus.RESOLVED, issue, issue)
    with pytest.raises(ValueError, match="different source locations"):
        DeltaIssue(DeltaStatus.MOVED, issue, issue)
    with pytest.raises(TypeError, match="baseline must be"):
        compare_results(object(), make_result(("A",), ("A",)))  # type: ignore[arg-type]
