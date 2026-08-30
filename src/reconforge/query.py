"""Read-only filtering and pagination over reconciliation issues."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from enum import StrEnum
from types import MappingProxyType

from reconforge.errors import ConfigurationError
from reconforge.model import CellValue, ReconciliationResult, RecordKey

_CURSOR_VERSION = 1


class IssueKind(StrEnum):
    """Stable categories exposed by result investigation queries."""

    FIELD_DIFFERENCE = "field_difference"
    VALIDATION_VIOLATION = "validation_violation"
    MISSING_FROM_LEFT = "missing_from_left"
    MISSING_FROM_RIGHT = "missing_from_right"
    DUPLICATE_KEY_LEFT = "duplicate_key_left"
    DUPLICATE_KEY_RIGHT = "duplicate_key_right"
    FIELD_ONLY_LEFT = "field_only_left"
    FIELD_ONLY_RIGHT = "field_only_right"

    @classmethod
    def named(cls, name: str) -> IssueKind:
        """Resolve a public issue kind with a deterministic error message."""

        try:
            return cls(name.strip().lower())
        except ValueError as exc:
            choices = ", ".join(item.value for item in cls)
            raise ValueError(f"unknown issue kind {name!r}; expected one of: {choices}") from exc


class IssueSort(StrEnum):
    """Deterministic orderings available to result investigation queries."""

    NATURAL = "natural"
    KIND = "kind"
    KEY = "key"
    FIELD = "field"
    SOURCE_ROW = "source_row"

    @classmethod
    def named(cls, name: str) -> IssueSort:
        """Resolve a public sort mode with a deterministic error message."""

        try:
            return cls(name.strip().lower())
        except ValueError as exc:
            choices = ", ".join(item.value for item in cls)
            raise ValueError(f"unknown issue sort {name!r}; expected one of: {choices}") from exc


@dataclass(frozen=True, slots=True, order=True)
class SourceRow:
    """One side and source-row location used by issue filters."""

    side: str
    row_number: int

    def __post_init__(self) -> None:
        if self.side not in {"left", "right"}:
            raise ValueError("source row side must be 'left' or 'right'")
        if type(self.row_number) is not int or self.row_number < 1:
            raise ValueError("source row number must be a positive integer")


@dataclass(frozen=True, slots=True, order=True)
class SourceRowRange:
    """Inclusive source-row interval used by issue filters."""

    side: str
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.side not in {"left", "right"}:
            raise ValueError("source row range side must be 'left' or 'right'")
        if type(self.start) is not int or self.start < 1:
            raise ValueError("source row range start must be a positive integer")
        if type(self.end) is not int or self.end < 1:
            raise ValueError("source row range end must be a positive integer")
        if self.start > self.end:
            raise ValueError("source row range start must not exceed end")

    def contains(self, row: SourceRow) -> bool:
        """Return whether a source row belongs to this inclusive interval."""

        return row.side == self.side and self.start <= row.row_number <= self.end


@dataclass(frozen=True, slots=True)
class ResultQuery:
    """Immutable filters and page bounds for one result investigation."""

    kinds: frozenset[IssueKind] = dataclass_field(default_factory=frozenset)
    keys: frozenset[RecordKey] = dataclass_field(default_factory=frozenset)
    fields: frozenset[str] = dataclass_field(default_factory=frozenset)
    source_rows: frozenset[SourceRow] = dataclass_field(default_factory=frozenset)
    source_row_ranges: tuple[SourceRowRange, ...] = ()
    sides: frozenset[str] = dataclass_field(default_factory=frozenset)
    sort: IssueSort = IssueSort.NATURAL
    offset: int = 0
    limit: int = 50

    def __post_init__(self) -> None:
        kinds = frozenset(self.kinds)
        if any(not isinstance(kind, IssueKind) for kind in kinds):
            raise TypeError("query kinds must be IssueKind instances")
        keys = frozenset(tuple(key) for key in self.keys)
        for key in keys:
            if any(component is not None and not isinstance(component, str) for component in key):
                raise TypeError("query key components must be strings or null")
        fields = frozenset(self.fields)
        if any(not isinstance(name, str) or not name for name in fields):
            raise ValueError("query fields must be non-empty strings")
        source_rows = frozenset(self.source_rows)
        if any(not isinstance(row, SourceRow) for row in source_rows):
            raise TypeError("query source rows must be SourceRow instances")
        source_row_ranges = tuple(self.source_row_ranges)
        if any(not isinstance(row_range, SourceRowRange) for row_range in source_row_ranges):
            raise TypeError("query source row ranges must be SourceRowRange instances")
        sides = frozenset(self.sides)
        invalid_sides = sorted(side for side in sides if side not in {"left", "right"})
        if invalid_sides:
            raise ValueError(
                f"query sides must contain only 'left' or 'right'; received: {invalid_sides!r}"
            )
        if not isinstance(self.sort, IssueSort):
            raise TypeError("query sort must be an IssueSort instance")
        if type(self.offset) is not int or self.offset < 0:
            raise ValueError("query offset must be a non-negative integer")
        if type(self.limit) is not int or not 1 <= self.limit <= 1000:
            raise ValueError("query limit must be an integer from 1 to 1000")
        object.__setattr__(self, "kinds", kinds)
        object.__setattr__(self, "keys", keys)
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "source_rows", source_rows)
        object.__setattr__(self, "source_row_ranges", source_row_ranges)
        object.__setattr__(self, "sides", sides)


@dataclass(frozen=True, slots=True)
class ResultIssue:
    """One stable issue reference produced from an immutable result."""

    kind: IssueKind
    key: RecordKey | None = None
    field: str | None = None
    source_rows: tuple[SourceRow, ...] = ()
    details: Mapping[str, object] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.key is not None:
            object.__setattr__(self, "key", tuple(self.key))
        object.__setattr__(self, "source_rows", tuple(self.source_rows))
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True, slots=True)
class ResultFacets:
    """Counts across the complete filtered issue set, before pagination."""

    by_kind: Mapping[IssueKind, int] = dataclass_field(default_factory=dict)
    by_field: Mapping[str, int] = dataclass_field(default_factory=dict)
    by_side: Mapping[str, int] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_kind", MappingProxyType(dict(self.by_kind)))
        object.__setattr__(self, "by_field", MappingProxyType(dict(self.by_field)))
        object.__setattr__(self, "by_side", MappingProxyType(dict(self.by_side)))


@dataclass(frozen=True, slots=True)
class ResultPage:
    """A deterministic page and counts without modifying the source result."""

    query: ResultQuery
    total_issues: int
    matched_issues: int
    issues: tuple[ResultIssue, ...]
    facets: ResultFacets

    @property
    def returned_issues(self) -> int:
        return len(self.issues)

    @property
    def has_more(self) -> bool:
        return self.query.offset + self.returned_issues < self.matched_issues

    @property
    def next_offset(self) -> int | None:
        if not self.has_more:
            return None
        return self.query.offset + self.returned_issues


def query_result(result: ReconciliationResult, query: ResultQuery | None = None) -> ResultPage:
    """Filter and paginate issue references while leaving the result untouched."""

    selected = query or ResultQuery()
    _validate_query_scope(result, selected)
    all_issues = _collect_issues(result)
    matched = tuple(issue for issue in all_issues if _matches(issue, selected))
    matched = _sort_issues(matched, selected.sort)
    start = selected.offset
    stop = start + selected.limit
    return ResultPage(
        query=selected,
        total_issues=len(all_issues),
        matched_issues=len(matched),
        issues=matched[start:stop],
        facets=_build_facets(matched),
    )


def result_page_to_dict(page: ResultPage) -> dict[str, object]:
    """Serialize a result page into a stable JSON-compatible structure."""

    query = page.query
    return {
        "query_version": 1,
        "filters": _query_filters_to_dict(query),
        "page": {
            "offset": query.offset,
            "limit": query.limit,
            "total_issues": page.total_issues,
            "matched_issues": page.matched_issues,
            "returned_issues": page.returned_issues,
            "has_more": page.has_more,
            "next_offset": page.next_offset,
            "next_cursor": next_page_cursor(page),
        },
        "facets": {
            "kinds": {
                kind.value: page.facets.by_kind[kind]
                for kind in sorted(page.facets.by_kind, key=lambda item: item.value)
            },
            "fields": dict(sorted(page.facets.by_field.items())),
            "sides": dict(sorted(page.facets.by_side.items())),
        },
        "issues": [
            {
                "kind": issue.kind.value,
                "key": None if issue.key is None else _key_to_list(issue.key),
                "field": issue.field,
                "source_rows": [
                    {"side": row.side, "row_number": row.row_number} for row in issue.source_rows
                ],
                "details": dict(issue.details),
            }
            for issue in page.issues
        ],
    }


def next_page_cursor(page: ResultPage) -> str | None:
    """Create an opaque cursor for the next page, if one exists."""

    if page.next_offset is None:
        return None
    payload = {
        "version": _CURSOR_VERSION,
        "offset": page.next_offset,
        "query": _query_fingerprint(page.query),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def apply_page_cursor(query: ResultQuery, cursor: str) -> ResultQuery:
    """Apply a validated page cursor to an otherwise identical query."""

    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        payload = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid result page cursor") from exc
    if not isinstance(payload, dict) or payload.get("version") != _CURSOR_VERSION:
        raise ValueError("unsupported result page cursor version")
    offset = payload.get("offset")
    if type(offset) is not int or offset < 0:
        raise ValueError("invalid result page cursor offset")
    fingerprint = payload.get("query")
    if not isinstance(fingerprint, str) or not hmac.compare_digest(
        fingerprint, _query_fingerprint(query)
    ):
        raise ValueError("result page cursor does not match the current query")
    return replace(query, offset=offset)


def _query_filters_to_dict(query: ResultQuery) -> dict[str, object]:
    return {
        "kinds": sorted(kind.value for kind in query.kinds),
        "keys": [_key_to_list(key) for key in sorted(query.keys, key=_sortable_key)],
        "fields": sorted(query.fields),
        "source_rows": [
            {"side": row.side, "row_number": row.row_number} for row in sorted(query.source_rows)
        ],
        "source_row_ranges": [
            {"side": row_range.side, "start": row_range.start, "end": row_range.end}
            for row_range in sorted(query.source_row_ranges)
        ],
        "sides": sorted(query.sides),
        "sort": query.sort.value,
    }


def _query_fingerprint(query: ResultQuery) -> str:
    identity = {
        "filters": _query_filters_to_dict(query),
        "limit": query.limit,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_query_scope(result: ReconciliationResult, query: ResultQuery) -> None:
    wrong_arity = sorted({len(key) for key in query.keys if len(key) != len(result.key_fields)})
    if wrong_arity:
        raise ConfigurationError(
            f"query keys must contain {len(result.key_fields)} component(s); "
            f"received lengths: {wrong_arity!r}"
        )
    known_fields = {
        *result.compared_fields,
        *(name for name, _ in result.validation_rules),
        *result.fields_only_left,
        *result.fields_only_right,
    }
    unknown_fields = sorted(query.fields - known_fields)
    if unknown_fields:
        raise ConfigurationError(f"query field(s) do not occur in this result: {unknown_fields!r}")


def _matches(issue: ResultIssue, query: ResultQuery) -> bool:
    if query.kinds and issue.kind not in query.kinds:
        return False
    if query.keys and (issue.key is None or issue.key not in query.keys):
        return False
    if query.fields and (issue.field is None or issue.field not in query.fields):
        return False
    if query.sides and not any(row.side in query.sides for row in issue.source_rows):
        return False
    if not query.source_rows and not query.source_row_ranges:
        return True
    return any(
        row in query.source_rows
        or any(row_range.contains(row) for row_range in query.source_row_ranges)
        for row in issue.source_rows
    )


def _collect_issues(result: ReconciliationResult) -> tuple[ResultIssue, ...]:
    issues: list[ResultIssue] = []
    issues.extend(
        ResultIssue(
            IssueKind.FIELD_DIFFERENCE,
            key=item.key,
            field=item.field,
            source_rows=(
                SourceRow("left", item.left_row),
                SourceRow("right", item.right_row),
            ),
            details={
                "left_value": item.left_value,
                "right_value": item.right_value,
            },
        )
        for item in result.differences
    )
    issues.extend(
        ResultIssue(
            IssueKind.VALIDATION_VIOLATION,
            field=item.field,
            source_rows=(SourceRow(item.side, item.row_number),),
            details={
                "source": item.source,
                "value": item.value,
                "rule": item.rule,
                "message": item.message,
            },
        )
        for item in result.validation_violations
    )
    issues.extend(
        ResultIssue(
            IssueKind.MISSING_FROM_LEFT,
            key=item.key,
            source_rows=(SourceRow("right", item.row_number),),
        )
        for item in result.missing_from_left
    )
    issues.extend(
        ResultIssue(
            IssueKind.MISSING_FROM_RIGHT,
            key=item.key,
            source_rows=(SourceRow("left", item.row_number),),
        )
        for item in result.missing_from_right
    )
    issues.extend(
        ResultIssue(
            IssueKind.DUPLICATE_KEY_LEFT,
            key=item.key,
            source_rows=tuple(SourceRow("left", row) for row in item.row_numbers),
        )
        for item in result.duplicate_left
    )
    issues.extend(
        ResultIssue(
            IssueKind.DUPLICATE_KEY_RIGHT,
            key=item.key,
            source_rows=tuple(SourceRow("right", row) for row in item.row_numbers),
        )
        for item in result.duplicate_right
    )
    issues.extend(
        ResultIssue(IssueKind.FIELD_ONLY_LEFT, field=name) for name in result.fields_only_left
    )
    issues.extend(
        ResultIssue(IssueKind.FIELD_ONLY_RIGHT, field=name) for name in result.fields_only_right
    )
    return tuple(issues)


def _build_facets(issues: tuple[ResultIssue, ...]) -> ResultFacets:
    kinds = Counter(issue.kind for issue in issues)
    fields = Counter(issue.field for issue in issues if issue.field is not None)
    sides: Counter[str] = Counter()
    for issue in issues:
        sides.update({row.side for row in issue.source_rows})
    return ResultFacets(by_kind=kinds, by_field=fields, by_side=sides)


def _sort_issues(
    issues: tuple[ResultIssue, ...], sort: IssueSort
) -> tuple[ResultIssue, ...]:
    if sort is IssueSort.NATURAL:
        return issues
    indexed = sorted(
        enumerate(issues),
        key=lambda item: _issue_sort_key(item[1], item[0], sort),
    )
    return tuple(issue for _, issue in indexed)


def _issue_sort_key(issue: ResultIssue, natural_index: int, sort: IssueSort) -> tuple[object, ...]:
    if sort is IssueSort.KIND:
        return (issue.kind.value, natural_index)
    if sort is IssueSort.KEY:
        return (
            issue.key is None,
            () if issue.key is None else _sortable_key(issue.key),
            natural_index,
        )
    if sort is IssueSort.FIELD:
        return (
            issue.field is None,
            "" if issue.field is None else issue.field.casefold(),
            "" if issue.field is None else issue.field,
            natural_index,
        )
    return (
        not issue.source_rows,
        min(issue.source_rows) if issue.source_rows else SourceRow("right", 1),
        natural_index,
    )


def _key_to_list(key: RecordKey) -> list[CellValue]:
    return list(key)


def _sortable_key(key: RecordKey) -> tuple[str, ...]:
    return tuple("" if value is None else value for value in key)
