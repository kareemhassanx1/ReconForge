from __future__ import annotations

import pytest

from reconforge.errors import ConfigurationError
from reconforge.model import Dataset, Record
from reconforge.query import (
    IssueKind,
    IssueSort,
    ResultQuery,
    SourceRow,
    SourceRowRange,
    query_result,
    result_page_to_dict,
)
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.report import result_to_dict
from reconforge.validation import RequiredRule


def make_result():
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
    return reconcile(
        left,
        right,
        ReconciliationConfig(
            key_fields=("id",),
            validation_rules={"status": (RequiredRule(),)},
        ),
    )


def test_default_query_exposes_every_issue_in_stable_kind_order() -> None:
    page = query_result(make_result())

    assert page.total_issues == 8
    assert page.matched_issues == 8
    assert page.returned_issues == 8
    assert not page.has_more
    assert page.next_offset is None
    assert [issue.kind for issue in page.issues] == [
        IssueKind.FIELD_DIFFERENCE,
        IssueKind.VALIDATION_VIOLATION,
        IssueKind.MISSING_FROM_LEFT,
        IssueKind.MISSING_FROM_RIGHT,
        IssueKind.DUPLICATE_KEY_LEFT,
        IssueKind.DUPLICATE_KEY_RIGHT,
        IssueKind.FIELD_ONLY_LEFT,
        IssueKind.FIELD_ONLY_RIGHT,
    ]


def test_filters_use_and_between_dimensions_and_or_within_each_dimension() -> None:
    result = make_result()

    by_kind_and_key = query_result(
        result,
        ResultQuery(
            kinds=frozenset({IssueKind.FIELD_DIFFERENCE, IssueKind.MISSING_FROM_LEFT}),
            keys=frozenset({("A",), ("C",)}),
        ),
    )
    by_field = query_result(result, ResultQuery(fields=frozenset({"status"})))
    by_row = query_result(
        result,
        ResultQuery(source_rows=frozenset({SourceRow("left", 3)})),
    )

    assert [(item.kind, item.key) for item in by_kind_and_key.issues] == [
        (IssueKind.FIELD_DIFFERENCE, ("A",)),
        (IssueKind.MISSING_FROM_LEFT, ("C",)),
    ]
    assert [item.kind for item in by_field.issues] == [IssueKind.VALIDATION_VIOLATION]
    assert [item.kind for item in by_row.issues] == [
        IssueKind.VALIDATION_VIOLATION,
        IssueKind.MISSING_FROM_RIGHT,
    ]


def test_side_filter_matches_source_context_and_excludes_schema_only_issues() -> None:
    result = make_result()

    left = query_result(result, ResultQuery(sides=frozenset({"left"})))
    right = query_result(result, ResultQuery(sides=frozenset({"right"})))
    either = query_result(result, ResultQuery(sides=frozenset({"left", "right"})))

    assert [item.kind for item in left.issues] == [
        IssueKind.FIELD_DIFFERENCE,
        IssueKind.VALIDATION_VIOLATION,
        IssueKind.MISSING_FROM_RIGHT,
        IssueKind.DUPLICATE_KEY_LEFT,
    ]
    assert [item.kind for item in right.issues] == [
        IssueKind.FIELD_DIFFERENCE,
        IssueKind.MISSING_FROM_LEFT,
        IssueKind.DUPLICATE_KEY_RIGHT,
    ]
    assert [item.kind for item in either.issues] == [
        IssueKind.FIELD_DIFFERENCE,
        IssueKind.VALIDATION_VIOLATION,
        IssueKind.MISSING_FROM_LEFT,
        IssueKind.MISSING_FROM_RIGHT,
        IssueKind.DUPLICATE_KEY_LEFT,
        IssueKind.DUPLICATE_KEY_RIGHT,
    ]


def test_source_row_ranges_are_inclusive_and_combine_with_exact_rows() -> None:
    result = make_result()

    ranged = query_result(
        result,
        ResultQuery(source_row_ranges=(SourceRowRange("right", 3, 4),)),
    )
    combined = query_result(
        result,
        ResultQuery(
            source_rows=frozenset({SourceRow("left", 3)}),
            source_row_ranges=(SourceRowRange("right", 4, 5),),
        ),
    )

    assert [item.kind for item in ranged.issues] == [
        IssueKind.MISSING_FROM_LEFT,
        IssueKind.DUPLICATE_KEY_RIGHT,
    ]
    assert [item.kind for item in combined.issues] == [
        IssueKind.VALIDATION_VIOLATION,
        IssueKind.MISSING_FROM_RIGHT,
        IssueKind.DUPLICATE_KEY_RIGHT,
    ]
    assert result_page_to_dict(ranged)["filters"]["source_row_ranges"] == [
        {"side": "right", "start": 3, "end": 4}
    ]


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        (
            IssueSort.KIND,
            [
                IssueKind.DUPLICATE_KEY_LEFT,
                IssueKind.DUPLICATE_KEY_RIGHT,
                IssueKind.FIELD_DIFFERENCE,
                IssueKind.FIELD_ONLY_LEFT,
                IssueKind.FIELD_ONLY_RIGHT,
                IssueKind.MISSING_FROM_LEFT,
                IssueKind.MISSING_FROM_RIGHT,
                IssueKind.VALIDATION_VIOLATION,
            ],
        ),
        (
            IssueSort.KEY,
            [
                IssueKind.FIELD_DIFFERENCE,
                IssueKind.MISSING_FROM_RIGHT,
                IssueKind.MISSING_FROM_LEFT,
                IssueKind.DUPLICATE_KEY_LEFT,
                IssueKind.DUPLICATE_KEY_RIGHT,
                IssueKind.VALIDATION_VIOLATION,
                IssueKind.FIELD_ONLY_LEFT,
                IssueKind.FIELD_ONLY_RIGHT,
            ],
        ),
        (
            IssueSort.FIELD,
            [
                IssueKind.FIELD_ONLY_LEFT,
                IssueKind.FIELD_ONLY_RIGHT,
                IssueKind.VALIDATION_VIOLATION,
                IssueKind.FIELD_DIFFERENCE,
                IssueKind.MISSING_FROM_LEFT,
                IssueKind.MISSING_FROM_RIGHT,
                IssueKind.DUPLICATE_KEY_LEFT,
                IssueKind.DUPLICATE_KEY_RIGHT,
            ],
        ),
        (
            IssueSort.SOURCE_ROW,
            [
                IssueKind.FIELD_DIFFERENCE,
                IssueKind.VALIDATION_VIOLATION,
                IssueKind.MISSING_FROM_RIGHT,
                IssueKind.DUPLICATE_KEY_LEFT,
                IssueKind.MISSING_FROM_LEFT,
                IssueKind.DUPLICATE_KEY_RIGHT,
                IssueKind.FIELD_ONLY_LEFT,
                IssueKind.FIELD_ONLY_RIGHT,
            ],
        ),
    ],
)
def test_sort_modes_are_deterministic_and_keep_natural_tie_order(
    sort: IssueSort, expected: list[IssueKind]
) -> None:
    page = query_result(make_result(), ResultQuery(sort=sort))

    assert [issue.kind for issue in page.issues] == expected
    assert result_page_to_dict(page)["filters"]["sort"] == sort.value


def test_pagination_counts_the_full_result_and_filtered_match_set() -> None:
    page = query_result(make_result(), ResultQuery(offset=2, limit=3))
    payload = result_page_to_dict(page)

    assert [item.kind for item in page.issues] == [
        IssueKind.MISSING_FROM_LEFT,
        IssueKind.MISSING_FROM_RIGHT,
        IssueKind.DUPLICATE_KEY_LEFT,
    ]
    assert page.total_issues == 8
    assert page.matched_issues == 8
    assert page.has_more
    assert page.next_offset == 5
    assert payload["page"] == {
        "offset": 2,
        "limit": 3,
        "total_issues": 8,
        "matched_issues": 8,
        "returned_issues": 3,
        "has_more": True,
        "next_offset": 5,
    }


def test_query_never_changes_the_underlying_reconciliation_result() -> None:
    result = make_result()
    before = result_to_dict(result)

    query_result(
        result,
        ResultQuery(
            kinds=frozenset({IssueKind.FIELD_DIFFERENCE}),
            offset=10,
            limit=2,
        ),
    )

    assert result_to_dict(result) == before
    assert result.issue_count == 8


def test_serialization_is_stable_and_keeps_source_context() -> None:
    page = query_result(
        make_result(),
        ResultQuery(kinds=frozenset({IssueKind.FIELD_DIFFERENCE})),
    )

    assert result_page_to_dict(page) == result_page_to_dict(page)
    assert result_page_to_dict(page)["issues"] == [
        {
            "kind": "field_difference",
            "key": ["A"],
            "field": "value",
            "source_rows": [
                {"side": "left", "row_number": 2},
                {"side": "right", "row_number": 2},
            ],
            "details": {"left_value": "one", "right_value": "changed"},
        }
    ]
    with pytest.raises(TypeError):
        page.issues[0].details["changed"] = True  # type: ignore[index]


@pytest.mark.parametrize(
    ("query", "message"),
    [
        (ResultQuery(keys=frozenset({("A", "extra")})), "must contain 1 component"),
        (ResultQuery(fields=frozenset({"unknown"})), "do not occur in this result"),
    ],
)
def test_result_specific_invalid_filters_are_actionable(query: ResultQuery, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        query_result(make_result(), query)


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: ResultQuery(offset=-1), "offset must be a non-negative"),
        (lambda: ResultQuery(limit=0), "limit must be an integer from 1 to 1000"),
        (lambda: SourceRow("middle", 2), "side must be 'left' or 'right'"),
        (lambda: SourceRow("left", 0), "row number must be a positive"),
        (lambda: ResultQuery(sides=frozenset({"center"})), "only 'left' or 'right'"),
        (lambda: SourceRowRange("middle", 1, 2), "side must be 'left' or 'right'"),
        (lambda: SourceRowRange("left", 0, 2), "start must be a positive"),
        (lambda: SourceRowRange("left", 3, 2), "start must not exceed end"),
    ],
)
def test_query_objects_validate_public_bounds(factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_unknown_issue_kind_lists_every_supported_choice() -> None:
    with pytest.raises(ValueError, match="unknown issue kind.*field_difference"):
        IssueKind.named("amount")


def test_sort_mode_requires_public_enum_and_lists_supported_choices() -> None:
    with pytest.raises(TypeError, match="sort must be an IssueSort"):
        ResultQuery(sort="field")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown issue sort.*source_row"):
        IssueSort.named("priority")
