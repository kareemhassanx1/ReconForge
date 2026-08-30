"""Deterministic comparisons between reconciliation results."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from types import MappingProxyType

from reconforge.model import ReconciliationResult, RecordKey
from reconforge.query import (
    IssueKind,
    ResultIssue,
    issue_fingerprint,
    result_issue_to_dict,
    result_issues,
)

DELTA_QUERY_VERSION = 1


class DeltaStatus(StrEnum):
    """How one issue changed between a baseline and current result."""

    ADDED = "added"
    MOVED = "moved"
    RESOLVED = "resolved"
    UNCHANGED = "unchanged"

    @classmethod
    def named(cls, name: str) -> DeltaStatus:
        """Resolve a public delta status with a deterministic error message."""

        try:
            return cls(name.strip().lower())
        except ValueError as exc:
            choices = ", ".join(item.value for item in cls)
            raise ValueError(f"unknown delta status {name!r}; expected one of: {choices}") from exc


@dataclass(frozen=True, slots=True)
class DeltaIssue:
    """One issue's presence in a baseline and current result."""

    status: DeltaStatus
    baseline: ResultIssue | None
    current: ResultIssue | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, DeltaStatus):
            raise TypeError("delta issue status must be a DeltaStatus instance")
        if self.status is DeltaStatus.ADDED and (self.baseline is not None or self.current is None):
            raise ValueError("added delta issues require only a current issue")
        if self.status is DeltaStatus.RESOLVED and (
            self.baseline is None or self.current is not None
        ):
            raise ValueError("resolved delta issues require only a baseline issue")
        if self.status is DeltaStatus.MOVED:
            if self.baseline is None or self.current is None:
                raise ValueError("moved delta issues require baseline and current issues")
            if issue_fingerprint(self.baseline) == issue_fingerprint(self.current):
                raise ValueError("moved delta issues must have different source locations")
            if issue_fingerprint(self.baseline, include_source_rows=False) != issue_fingerprint(
                self.current, include_source_rows=False
            ):
                raise ValueError("moved delta issues must have identical non-location content")
        if self.status is DeltaStatus.UNCHANGED:
            if self.baseline is None or self.current is None:
                raise ValueError("unchanged delta issues require baseline and current issues")
            if issue_fingerprint(self.baseline) != issue_fingerprint(self.current):
                raise ValueError("unchanged delta issues must have identical fingerprints")

    @property
    def issue(self) -> ResultIssue:
        """Return the current issue, or the baseline issue when resolved."""

        selected = self.current if self.current is not None else self.baseline
        if selected is None:  # pragma: no cover - protected by constructor validation
            raise RuntimeError("delta issue has no issue value")
        return selected


@dataclass(frozen=True, slots=True)
class ResultDelta:
    """Deterministic issue-level difference between two results."""

    baseline_total: int
    current_total: int
    issues: tuple[DeltaIssue, ...]

    @property
    def added(self) -> tuple[DeltaIssue, ...]:
        return tuple(item for item in self.issues if item.status is DeltaStatus.ADDED)

    @property
    def resolved(self) -> tuple[DeltaIssue, ...]:
        return tuple(item for item in self.issues if item.status is DeltaStatus.RESOLVED)

    @property
    def moved(self) -> tuple[DeltaIssue, ...]:
        return tuple(item for item in self.issues if item.status is DeltaStatus.MOVED)

    @property
    def unchanged(self) -> tuple[DeltaIssue, ...]:
        return tuple(item for item in self.issues if item.status is DeltaStatus.UNCHANGED)

    @property
    def changed_count(self) -> int:
        return len(self.added) + len(self.moved) + len(self.resolved)


@dataclass(frozen=True, slots=True)
class DeltaQuery:
    """Immutable filters for selecting entries from a result delta."""

    statuses: frozenset[DeltaStatus] = dataclass_field(default_factory=frozenset)
    kinds: frozenset[IssueKind] = dataclass_field(default_factory=frozenset)
    keys: frozenset[RecordKey] = dataclass_field(default_factory=frozenset)
    fields: frozenset[str] = dataclass_field(default_factory=frozenset)
    offset: int = 0
    limit: int = 50

    def __post_init__(self) -> None:
        statuses = frozenset(self.statuses)
        if any(not isinstance(status, DeltaStatus) for status in statuses):
            raise TypeError("delta query statuses must be DeltaStatus instances")
        kinds = frozenset(self.kinds)
        if any(not isinstance(kind, IssueKind) for kind in kinds):
            raise TypeError("delta query kinds must be IssueKind instances")
        keys = frozenset(tuple(key) for key in self.keys)
        for key in keys:
            if any(component is not None and not isinstance(component, str) for component in key):
                raise TypeError("delta query key components must be strings or null")
        fields = frozenset(self.fields)
        if any(not isinstance(name, str) or not name for name in fields):
            raise ValueError("delta query fields must be non-empty strings")
        if type(self.offset) is not int or self.offset < 0:
            raise ValueError("delta query offset must be a non-negative integer")
        if type(self.limit) is not int or not 1 <= self.limit <= 1000:
            raise ValueError("delta query limit must be an integer from 1 to 1000")
        object.__setattr__(self, "statuses", statuses)
        object.__setattr__(self, "kinds", kinds)
        object.__setattr__(self, "keys", keys)
        object.__setattr__(self, "fields", fields)


@dataclass(frozen=True, slots=True)
class DeltaPage:
    """A deterministic page over filtered result-delta entries."""

    query: DeltaQuery
    baseline_total: int
    current_total: int
    total_entries: int
    matched_entries: int
    issues: tuple[DeltaIssue, ...]
    facets: DeltaFacets

    @property
    def returned_entries(self) -> int:
        return len(self.issues)

    @property
    def has_more(self) -> bool:
        return self.query.offset + self.returned_entries < self.matched_entries

    @property
    def next_offset(self) -> int | None:
        if not self.has_more:
            return None
        return self.query.offset + self.returned_entries


@dataclass(frozen=True, slots=True)
class DeltaFacets:
    """Counts across a complete filtered delta set, before pagination."""

    by_status: Mapping[DeltaStatus, int] = dataclass_field(default_factory=dict)
    by_kind: Mapping[IssueKind, int] = dataclass_field(default_factory=dict)
    by_field: Mapping[str, int] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_status", MappingProxyType(dict(self.by_status)))
        object.__setattr__(self, "by_kind", MappingProxyType(dict(self.by_kind)))
        object.__setattr__(self, "by_field", MappingProxyType(dict(self.by_field)))


def compare_results(
    baseline: ReconciliationResult,
    current: ReconciliationResult,
) -> ResultDelta:
    """Compare two results using stable, multiplicity-aware issue fingerprints."""

    if not isinstance(baseline, ReconciliationResult):
        raise TypeError("baseline must be a ReconciliationResult instance")
    if not isinstance(current, ReconciliationResult):
        raise TypeError("current must be a ReconciliationResult instance")
    baseline_issues = result_issues(baseline)
    current_issues = result_issues(current)
    buckets: dict[str, deque[tuple[int, ResultIssue]]] = defaultdict(deque)
    for index, issue in enumerate(baseline_issues):
        buckets[issue_fingerprint(issue)].append((index, issue))

    consumed: set[int] = set()
    current_delta: dict[int, DeltaIssue] = {}
    unmatched_current: list[tuple[int, ResultIssue]] = []
    for current_index, issue in enumerate(current_issues):
        matches = buckets[issue_fingerprint(issue)]
        if matches:
            index, baseline_issue = matches.popleft()
            consumed.add(index)
            current_delta[current_index] = DeltaIssue(DeltaStatus.UNCHANGED, baseline_issue, issue)
        else:
            unmatched_current.append((current_index, issue))

    location_buckets: dict[str, deque[tuple[int, ResultIssue]]] = defaultdict(deque)
    for baseline_index, issue in enumerate(baseline_issues):
        if baseline_index not in consumed:
            location_buckets[issue_fingerprint(issue, include_source_rows=False)].append(
                (baseline_index, issue)
            )
    for current_index, issue in unmatched_current:
        matches = location_buckets[issue_fingerprint(issue, include_source_rows=False)]
        if matches:
            baseline_index, baseline_issue = matches.popleft()
            consumed.add(baseline_index)
            current_delta[current_index] = DeltaIssue(DeltaStatus.MOVED, baseline_issue, issue)
        else:
            current_delta[current_index] = DeltaIssue(DeltaStatus.ADDED, None, issue)

    delta_issues = [current_delta[index] for index in range(len(current_issues))]
    delta_issues.extend(
        DeltaIssue(DeltaStatus.RESOLVED, issue, None)
        for index, issue in enumerate(baseline_issues)
        if index not in consumed
    )
    return ResultDelta(
        baseline_total=len(baseline_issues),
        current_total=len(current_issues),
        issues=tuple(delta_issues),
    )


def filter_delta_issues(
    delta: ResultDelta,
    query: DeltaQuery | None = None,
) -> tuple[DeltaIssue, ...]:
    """Filter delta entries with AND across dimensions and OR within each one."""

    if not isinstance(delta, ResultDelta):
        raise TypeError("delta must be a ResultDelta instance")
    selected = query or DeltaQuery()
    if not isinstance(selected, DeltaQuery):
        raise TypeError("query must be a DeltaQuery instance")
    return tuple(item for item in delta.issues if _matches_delta(item, selected))


def query_delta(delta: ResultDelta, query: DeltaQuery | None = None) -> DeltaPage:
    """Filter and paginate one result delta without modifying it."""

    selected = query or DeltaQuery()
    matched = filter_delta_issues(delta, selected)
    start = selected.offset
    stop = start + selected.limit
    return DeltaPage(
        query=selected,
        baseline_total=delta.baseline_total,
        current_total=delta.current_total,
        total_entries=len(delta.issues),
        matched_entries=len(matched),
        issues=matched[start:stop],
        facets=_build_delta_facets(matched),
    )


def delta_page_to_dict(page: DeltaPage) -> dict[str, object]:
    """Serialize a delta page into a stable JSON-compatible structure."""

    if not isinstance(page, DeltaPage):
        raise TypeError("page must be a DeltaPage instance")
    query = page.query
    return {
        "delta_query_version": DELTA_QUERY_VERSION,
        "filters": {
            "statuses": sorted(status.value for status in query.statuses),
            "kinds": sorted(kind.value for kind in query.kinds),
            "keys": [list(key) for key in sorted(query.keys, key=_sortable_key)],
            "fields": sorted(query.fields),
        },
        "page": {
            "offset": query.offset,
            "limit": query.limit,
            "baseline_total": page.baseline_total,
            "current_total": page.current_total,
            "total_entries": page.total_entries,
            "matched_entries": page.matched_entries,
            "returned_entries": page.returned_entries,
            "has_more": page.has_more,
            "next_offset": page.next_offset,
        },
        "facets": {
            "statuses": {
                status.value: page.facets.by_status[status]
                for status in sorted(page.facets.by_status, key=lambda item: item.value)
            },
            "kinds": {
                kind.value: page.facets.by_kind[kind]
                for kind in sorted(page.facets.by_kind, key=lambda item: item.value)
            },
            "fields": dict(sorted(page.facets.by_field.items())),
        },
        "issues": [delta_issue_to_dict(item) for item in page.issues],
    }


def delta_issue_to_dict(item: DeltaIssue) -> dict[str, object]:
    """Serialize one delta entry with explicit baseline and current values."""

    if not isinstance(item, DeltaIssue):
        raise TypeError("item must be a DeltaIssue instance")
    return {
        "status": item.status.value,
        "baseline": None if item.baseline is None else result_issue_to_dict(item.baseline),
        "current": None if item.current is None else result_issue_to_dict(item.current),
    }


def _matches_delta(item: DeltaIssue, query: DeltaQuery) -> bool:
    issue = item.issue
    if query.statuses and item.status not in query.statuses:
        return False
    if query.kinds and issue.kind not in query.kinds:
        return False
    if query.keys and (issue.key is None or issue.key not in query.keys):
        return False
    return not query.fields or (issue.field is not None and issue.field in query.fields)


def _build_delta_facets(issues: tuple[DeltaIssue, ...]) -> DeltaFacets:
    return DeltaFacets(
        by_status=Counter(item.status for item in issues),
        by_kind=Counter(item.issue.kind for item in issues),
        by_field=Counter(item.issue.field for item in issues if item.issue.field is not None),
    )


def _sortable_key(key: RecordKey) -> tuple[str, ...]:
    return tuple("" if component is None else component for component in key)
