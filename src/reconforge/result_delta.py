"""Deterministic comparisons between reconciliation results."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from enum import StrEnum

from reconforge.model import ReconciliationResult
from reconforge.query import ResultIssue, issue_fingerprint, result_issues


class DeltaStatus(StrEnum):
    """How one issue changed between a baseline and current result."""

    ADDED = "added"
    RESOLVED = "resolved"
    UNCHANGED = "unchanged"


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
    def unchanged(self) -> tuple[DeltaIssue, ...]:
        return tuple(item for item in self.issues if item.status is DeltaStatus.UNCHANGED)

    @property
    def changed_count(self) -> int:
        return len(self.added) + len(self.resolved)


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
    delta_issues: list[DeltaIssue] = []
    for issue in current_issues:
        matches = buckets[issue_fingerprint(issue)]
        if matches:
            index, baseline_issue = matches.popleft()
            consumed.add(index)
            delta_issues.append(DeltaIssue(DeltaStatus.UNCHANGED, baseline_issue, issue))
        else:
            delta_issues.append(DeltaIssue(DeltaStatus.ADDED, None, issue))
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
