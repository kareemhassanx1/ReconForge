"""Deterministic comparisons between reconciliation results."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum

from reconforge.model import ReconciliationResult, RecordKey
from reconforge.query import IssueKind, ResultIssue, issue_fingerprint, result_issues


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
        object.__setattr__(self, "statuses", statuses)
        object.__setattr__(self, "kinds", kinds)
        object.__setattr__(self, "keys", keys)
        object.__setattr__(self, "fields", fields)


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


def _matches_delta(item: DeltaIssue, query: DeltaQuery) -> bool:
    issue = item.issue
    if query.statuses and item.status not in query.statuses:
        return False
    if query.kinds and issue.kind not in query.kinds:
        return False
    if query.keys and (issue.key is None or issue.key not in query.keys):
        return False
    return not query.fields or (issue.field is not None and issue.field in query.fields)
