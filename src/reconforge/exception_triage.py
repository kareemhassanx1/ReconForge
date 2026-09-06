"""Deterministic triage for reconciliation exceptions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum


class ExceptionKind(StrEnum):
    MISSING_LEFT = "missing_left"
    MISSING_RIGHT = "missing_right"
    DUPLICATE_KEY = "duplicate_key"
    VALUE_DIFFERENCE = "value_difference"
    SCHEMA_DRIFT = "schema_drift"


class TriageBand(StrEnum):
    URGENT = "urgent"
    HIGH = "high"
    ROUTINE = "routine"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class ReconException:
    exception_id: str
    kind: ExceptionKind
    business_key: tuple[str, ...]
    detected_at: datetime
    amount_exposure: Decimal = Decimal("0")
    affected_fields: tuple[str, ...] = ()
    repeated_count: int = 1

    def __post_init__(self) -> None:
        if not self.exception_id.strip():
            raise ValueError("exception_id must not be blank")
        if not self.business_key or any(not part.strip() for part in self.business_key):
            raise ValueError("business_key must contain non-blank parts")
        if self.detected_at.tzinfo is None:
            raise ValueError("detected_at must be timezone-aware")
        if self.amount_exposure < 0:
            raise ValueError("amount_exposure must not be negative")
        if self.repeated_count < 1:
            raise ValueError("repeated_count must be positive")


@dataclass(frozen=True, slots=True)
class TriagePolicy:
    material_amount: Decimal = Decimal("1000")
    urgent_after: timedelta = timedelta(days=3)
    high_after: timedelta = timedelta(days=1)
    repeat_threshold: int = 3
    field_weights: Mapping[str, int] | None = None

    def __post_init__(self) -> None:
        if self.material_amount < 0:
            raise ValueError("material_amount must not be negative")
        if self.high_after <= timedelta(0) or self.urgent_after < self.high_after:
            raise ValueError("triage durations are invalid")
        if self.repeat_threshold < 1:
            raise ValueError("repeat_threshold must be positive")
        if self.field_weights and any(value < 0 for value in self.field_weights.values()):
            raise ValueError("field weights must not be negative")


DEFAULT_TRIAGE_POLICY = TriagePolicy()


@dataclass(frozen=True, slots=True)
class TriagedException:
    exception: ReconException
    band: TriageBand
    score: int
    age: timedelta
    reasons: tuple[str, ...]


_KIND_WEIGHTS = {
    ExceptionKind.MISSING_LEFT: 25,
    ExceptionKind.MISSING_RIGHT: 25,
    ExceptionKind.DUPLICATE_KEY: 30,
    ExceptionKind.VALUE_DIFFERENCE: 10,
    ExceptionKind.SCHEMA_DRIFT: 35,
}


def triage_exception(
    exception: ReconException,
    *,
    policy: TriagePolicy = DEFAULT_TRIAGE_POLICY,
    now: datetime | None = None,
) -> TriagedException:
    clock = now or datetime.now(UTC)
    if clock.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    age = max(timedelta(0), clock.astimezone(UTC) - exception.detected_at.astimezone(UTC))
    score = _KIND_WEIGHTS[exception.kind]
    reasons = [f"kind:{exception.kind.value}"]
    if exception.amount_exposure >= policy.material_amount:
        score += 25
        reasons.append("material-exposure")
    if exception.repeated_count >= policy.repeat_threshold:
        score += min(20, exception.repeated_count * 3)
        reasons.append("repeated-exception")
    weights = policy.field_weights or {}
    field_score = sum(weights.get(field, 0) for field in set(exception.affected_fields))
    if field_score:
        score += min(20, field_score)
        reasons.append("sensitive-fields")
    if age >= policy.urgent_after:
        score += 30
        reasons.append("age-urgent")
    elif age >= policy.high_after:
        score += 15
        reasons.append("age-high")
    if score >= 75:
        band = TriageBand.URGENT
    elif score >= 50:
        band = TriageBand.HIGH
    elif score >= 20:
        band = TriageBand.ROUTINE
    else:
        band = TriageBand.DEFERRED
    return TriagedException(exception, band, score, age, tuple(reasons))


def build_triage_queue(
    exceptions: Iterable[ReconException],
    *,
    policy: TriagePolicy = DEFAULT_TRIAGE_POLICY,
    now: datetime | None = None,
) -> tuple[TriagedException, ...]:
    unique: dict[str, ReconException] = {}
    for exception in exceptions:
        prior = unique.get(exception.exception_id)
        if prior is not None and prior != exception:
            raise ValueError(f"conflicting exception id: {exception.exception_id}")
        unique[exception.exception_id] = exception
    queue = [triage_exception(item, policy=policy, now=now) for item in unique.values()]
    queue.sort(
        key=lambda item: (
            -item.score,
            item.exception.detected_at,
            item.exception.exception_id,
        )
    )
    return tuple(queue)


def triage_counts(queue: Iterable[TriagedException]) -> dict[str, int]:
    counts = {band.value: 0 for band in TriageBand}
    counts["total"] = 0
    for item in queue:
        counts[item.band.value] += 1
        counts["total"] += 1
    return counts


def top_exception_ids(queue: Iterable[TriagedException], *, limit: int = 10) -> tuple[str, ...]:
    """Return a bounded operator shortlist while preserving queue order."""

    if limit < 0:
        raise ValueError("limit must not be negative")
    return tuple(item.exception.exception_id for item in queue)[:limit]
