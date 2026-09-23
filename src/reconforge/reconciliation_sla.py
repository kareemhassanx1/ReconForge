"""Service-level objectives for scheduled reconciliation jobs."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class RunState(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RUNNING = "running"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ReconciliationRun:
    run_id: str
    job: str
    scheduled_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    state: RunState
    records_compared: int = 0
    exceptions_found: int = 0

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.job.strip():
            raise ValueError("run_id and job must not be blank")
        values = (self.scheduled_at, self.started_at, self.finished_at)
        if any(value is not None and value.tzinfo is None for value in values):
            raise ValueError("run timestamps must be timezone-aware")
        if self.started_at and self.started_at < self.scheduled_at:
            raise ValueError("started_at must not precede scheduled_at")
        if self.finished_at and not self.started_at:
            raise ValueError("finished_at requires started_at")
        if self.finished_at and self.started_at and self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.records_compared < 0 or self.exceptions_found < 0:
            raise ValueError("run counts must not be negative")


@dataclass(frozen=True, slots=True)
class ServiceObjective:
    maximum_start_delay: timedelta = timedelta(minutes=5)
    maximum_duration: timedelta = timedelta(hours=1)
    freshness_window: timedelta = timedelta(days=1)
    minimum_success_rate: float = 0.99

    def __post_init__(self) -> None:
        durations = (self.maximum_start_delay, self.maximum_duration, self.freshness_window)
        if any(value <= timedelta(0) for value in durations):
            raise ValueError("objective durations must be positive")
        if not 0 <= self.minimum_success_rate <= 1:
            raise ValueError("minimum_success_rate must be between zero and one")


DEFAULT_SERVICE_OBJECTIVE = ServiceObjective()


@dataclass(frozen=True, slots=True)
class RunAssessment:
    run: ReconciliationRun
    compliant: bool
    reasons: tuple[str, ...]
    start_delay: timedelta | None
    duration: timedelta | None


@dataclass(frozen=True, slots=True)
class JobAssessment:
    job: str
    runs: tuple[RunAssessment, ...]
    compliant: bool
    success_rate: float
    last_success_at: datetime | None
    reasons: tuple[str, ...]


def assess_run(
    run: ReconciliationRun,
    *,
    objective: ServiceObjective = DEFAULT_SERVICE_OBJECTIVE,
    now: datetime | None = None,
) -> RunAssessment:
    clock = now or datetime.now(UTC)
    if clock.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    reasons: list[str] = []
    start_delay = run.started_at - run.scheduled_at if run.started_at else None
    duration = None
    if start_delay is None:
        if run.state is not RunState.SKIPPED:
            reasons.append("run-not-started")
    elif start_delay > objective.maximum_start_delay:
        reasons.append("start-delay-exceeded")
    if run.started_at:
        effective_end = run.finished_at or clock
        duration = max(timedelta(0), effective_end - run.started_at)
        if duration > objective.maximum_duration:
            reasons.append("duration-exceeded")
    if run.state is RunState.FAILED:
        reasons.append("run-failed")
    elif run.state is RunState.SKIPPED:
        reasons.append("run-skipped")
    return RunAssessment(run, not reasons, tuple(reasons), start_delay, duration)


def assess_job(
    runs: Iterable[ReconciliationRun],
    *,
    objective: ServiceObjective = DEFAULT_SERVICE_OBJECTIVE,
    now: datetime | None = None,
) -> JobAssessment:
    """Assess run health, success rate, and latest successful freshness."""

    clock = now or datetime.now(UTC)
    materialized = tuple(runs)
    if not materialized:
        raise ValueError("at least one run is required")
    jobs = {run.job for run in materialized}
    if len(jobs) != 1:
        raise ValueError("all runs must belong to one job")
    seen: dict[str, ReconciliationRun] = {}
    for run in materialized:
        prior = seen.get(run.run_id)
        if prior is not None and prior != run:
            raise ValueError(f"conflicting run id: {run.run_id}")
        seen[run.run_id] = run
    ordered = sorted(seen.values(), key=lambda run: (run.scheduled_at, run.run_id))
    assessments = tuple(assess_run(run, objective=objective, now=clock) for run in ordered)
    terminal = [run for run in ordered if run.state in {RunState.SUCCEEDED, RunState.FAILED}]
    successes = [run for run in terminal if run.state is RunState.SUCCEEDED]
    success_rate = len(successes) / len(terminal) if terminal else 0.0
    last_success = max((run.finished_at for run in successes if run.finished_at), default=None)
    reasons: list[str] = []
    if success_rate < objective.minimum_success_rate:
        reasons.append("success-rate-below-objective")
    if last_success is None or clock - last_success > objective.freshness_window:
        reasons.append("successful-result-stale")
    if any(not assessment.compliant for assessment in assessments):
        reasons.append("one-or-more-runs-noncompliant")
    return JobAssessment(
        job=next(iter(jobs)),
        runs=assessments,
        compliant=not reasons,
        success_rate=success_rate,
        last_success_at=last_success,
        reasons=tuple(reasons),
    )


def next_expected_run(last_scheduled_at: datetime, interval: timedelta) -> datetime:
    if last_scheduled_at.tzinfo is None:
        raise ValueError("last_scheduled_at must be timezone-aware")
    if interval <= timedelta(0):
        raise ValueError("interval must be positive")
    return last_scheduled_at + interval


def breached_run_ids(assessment: JobAssessment) -> tuple[str, ...]:
    """Return the stable identifiers of individually non-compliant runs."""

    return tuple(item.run.run_id for item in assessment.runs if not item.compliant)
