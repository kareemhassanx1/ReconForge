from dataclasses import replace

import pytest

from reconforge.model import Dataset, Record
from reconforge.query import IssueSort, ResultQuery, query_result, result_page_to_dict
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.result_delta import DeltaQuery, compare_results, delta_page_to_dict, query_delta

KEYS = ((None, "z"), ("", "a"))


def table(source, keys, value):
    return Dataset(
        source,
        ("account", "reference", "value"),
        tuple(
            Record(source, index, {"account": key[0], "reference": key[1], "value": value})
            for index, key in enumerate(keys, 2)
        ),
    )


@pytest.mark.parametrize(
    ("left_keys", "right_keys", "section"),
    [
        (KEYS, KEYS, "differences"),
        (KEYS, (), "missing_from_right"),
        ((), KEYS, "missing_from_left"),
        (KEYS * 2, (), "duplicate_left"),
        ((), KEYS * 2, "duplicate_right"),
    ],
)
def test_nullable_composite_keys_have_a_total_order(left_keys, right_keys, section):
    result = reconcile(
        table("left", left_keys, "old"),
        table("right", right_keys, "new"),
        ReconciliationConfig(key_fields=("account", "reference")),
    )
    assert tuple(item.key for item in getattr(result, section)) == KEYS


def test_query_order_and_filter_serialization_distinguish_null_from_empty_text():
    result = reconcile(
        table("left", KEYS, "old"),
        table("right", KEYS, "new"),
        ReconciliationConfig(key_fields=("account", "reference")),
    )
    # Saved reports need the same order even when their natural issue order differs.
    result = replace(result, differences=tuple(reversed(result.differences)))
    page = query_result(result, ResultQuery(keys=frozenset(KEYS), sort=IssueSort.KEY))
    assert tuple(issue.key for issue in page.issues) == KEYS
    assert result_page_to_dict(page)["filters"]["keys"] == [list(key) for key in KEYS]


def test_delta_query_serializes_nullable_key_filters_canonically():
    result = reconcile(
        table("left", KEYS, "old"),
        table("right", KEYS, "new"),
        ReconciliationConfig(key_fields=("account", "reference")),
    )
    page = query_delta(compare_results(result, result), DeltaQuery(keys=frozenset(KEYS)))
    assert delta_page_to_dict(page)["filters"]["keys"] == [list(key) for key in KEYS]
