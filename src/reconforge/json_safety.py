"""Shared safety checks for decoded JSON values."""

from __future__ import annotations

from collections.abc import Mapping

MAX_JSON_NESTING_DEPTH = 100


class JsonNestingTooDeepError(ValueError):
    """Signal that a decoded JSON value exceeds the supported nesting depth."""


def validate_json_nesting(
    value: object,
    *,
    max_depth: int = MAX_JSON_NESTING_DEPTH,
) -> None:
    """Reject JSON containers nested beyond ``max_depth`` without recursion."""

    if type(max_depth) is not int or max_depth < 0:
        raise ValueError("max_depth must be a non-negative integer")

    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        item, parent_depth = pending.pop()
        if isinstance(item, Mapping):
            depth = parent_depth + 1
            if depth > max_depth:
                raise JsonNestingTooDeepError(
                    f"JSON exceeds the maximum nesting depth of {max_depth}"
                )
            pending.extend((nested, depth) for nested in item.values())
        elif isinstance(item, list):
            depth = parent_depth + 1
            if depth > max_depth:
                raise JsonNestingTooDeepError(
                    f"JSON exceeds the maximum nesting depth of {max_depth}"
                )
            pending.extend((nested, depth) for nested in item)
