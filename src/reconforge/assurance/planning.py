"""Capacity planning for a analysis lane.

Plans are deterministic: demand is ordered by priority and due time, capacity
is consumed in stable window order, and every unallocated unit is explained.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


class UnscheduledReason(StrEnum):
    """Why demand could not be placed into a compatible window."""

    NO_COMPATIBLE_WINDOW = "no-compatible-window"
    CAPACITY_EXHAUSTED = "capacity-exhausted"
    OUTSIDE_HORIZON = "outside-horizon"


@dataclass(frozen=True, slots=True)
class CapacityWindow:
    """Available capacity for one analysis lane."""

    window_id: str
    starts_at: datetime
    ends_at: datetime
    units: int
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.window_id.strip():
            raise ValueError("window id must not be blank")
        if _utc(self.ends_at) <= _utc(self.starts_at):
            raise ValueError("window end must be after its start")
        if self.units < 1:
            raise ValueError("window units must be positive")


@dataclass(frozen=True, slots=True)
class Demand:
    """A divisible, prioritized request for analysis lane capacity."""

    demand_id: str
    earliest_start: datetime
    due_at: datetime
    units: int
    priority: int
    required_capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.demand_id.strip():
            raise ValueError("demand id must not be blank")
        if _utc(self.due_at) <= _utc(self.earliest_start):
            raise ValueError("demand due time must follow its earliest start")
        if self.units < 1:
            raise ValueError("demand units must be positive")
        if self.priority < 0:
            raise ValueError("demand priority must not be negative")


@dataclass(frozen=True, slots=True)
class Allocation:
    demand_id: str
    window_id: str
    units: int
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class Unscheduled:
    demand_id: str
    units: int
    reason: UnscheduledReason
    explanation: str


@dataclass(frozen=True, slots=True)
class CapacityPlan:
    generated_at: datetime
    allocations: tuple[Allocation, ...]
    unscheduled: tuple[Unscheduled, ...]
    window_capacity: Mapping[str, int]

    @property
    def scheduled_units(self) -> int:
        return sum(allocation.units for allocation in self.allocations)

    @property
    def unscheduled_units(self) -> int:
        return sum(item.units for item in self.unscheduled)

    def utilization(self, window_id: str) -> float:
        if window_id not in self.window_capacity:
            raise KeyError(window_id)
        capacity = self.window_capacity[window_id]
        allocated = sum(item.units for item in self.allocations if item.window_id == window_id)
        return allocated / capacity


class AnalysisLaneCapacityPlanner:
    """Allocate demand without overbooking or hidden prioritization."""

    def __init__(self, windows: Sequence[CapacityWindow]) -> None:
        if not windows:
            raise ValueError("at least one capacity window is required")
        ids = [window.window_id for window in windows]
        if len(set(ids)) != len(ids):
            raise ValueError("capacity window ids must be unique")
        self._windows = tuple(
            sorted(windows, key=lambda window: (_utc(window.starts_at), window.window_id))
        )

    def plan(self, demands: Iterable[Demand], *, generated_at: datetime) -> CapacityPlan:
        now = _utc(generated_at)
        materialized = tuple(demands)
        ids = [demand.demand_id for demand in materialized]
        if len(set(ids)) != len(ids):
            raise ValueError("demand ids must be unique")
        remaining = {window.window_id: window.units for window in self._windows}
        allocations: list[Allocation] = []
        unscheduled: list[Unscheduled] = []

        ordered = sorted(
            materialized,
            key=lambda demand: (-demand.priority, _utc(demand.due_at), demand.demand_id),
        )
        for demand in ordered:
            unplaced = demand.units
            candidates = self._compatible_windows(demand, now)
            for window in candidates:
                available = remaining[window.window_id]
                if available == 0:
                    continue
                assigned = min(available, unplaced)
                allocations.append(
                    Allocation(
                        demand_id=demand.demand_id,
                        window_id=window.window_id,
                        units=assigned,
                        starts_at=max(_utc(window.starts_at), _utc(demand.earliest_start)),
                        ends_at=min(_utc(window.ends_at), _utc(demand.due_at)),
                    )
                )
                remaining[window.window_id] -= assigned
                unplaced -= assigned
                if unplaced == 0:
                    break
            if unplaced:
                reason = self._reason(demand, candidates, now)
                unscheduled.append(
                    Unscheduled(
                        demand_id=demand.demand_id,
                        units=unplaced,
                        reason=reason,
                        explanation=self._explanation(reason, unplaced),
                    )
                )

        return CapacityPlan(
            generated_at=now,
            allocations=tuple(allocations),
            unscheduled=tuple(unscheduled),
            window_capacity={window.window_id: window.units for window in self._windows},
        )

    def _compatible_windows(
        self,
        demand: Demand,
        generated_at: datetime,
    ) -> tuple[CapacityWindow, ...]:
        return tuple(
            window
            for window in self._windows
            if _utc(window.ends_at) > max(generated_at, _utc(demand.earliest_start))
            and _utc(window.starts_at) < _utc(demand.due_at)
            and demand.required_capabilities.issubset(window.capabilities)
        )

    def _reason(
        self,
        demand: Demand,
        candidates: Sequence[CapacityWindow],
        generated_at: datetime,
    ) -> UnscheduledReason:
        if _utc(demand.due_at) <= generated_at:
            return UnscheduledReason.OUTSIDE_HORIZON
        if not candidates:
            return UnscheduledReason.NO_COMPATIBLE_WINDOW
        return UnscheduledReason.CAPACITY_EXHAUSTED

    @staticmethod
    def _explanation(reason: UnscheduledReason, units: int) -> str:
        descriptions = {
            UnscheduledReason.NO_COMPATIBLE_WINDOW: "no compatible capacity window",
            UnscheduledReason.CAPACITY_EXHAUSTED: "compatible capacity is exhausted",
            UnscheduledReason.OUTSIDE_HORIZON: "the demand due time has passed",
        }
        return f"{units} unit(s) remain: {descriptions[reason]}."


def allocations_by_demand(plan: CapacityPlan) -> dict[str, tuple[Allocation, ...]]:
    grouped: dict[str, list[Allocation]] = {}
    for allocation in plan.allocations:
        grouped.setdefault(allocation.demand_id, []).append(allocation)
    return {key: tuple(value) for key, value in sorted(grouped.items())}


def capacity_pressure(plan: CapacityPlan) -> tuple[str, ...]:
    """Return fully consumed windows in deterministic order."""

    return tuple(
        window_id
        for window_id in sorted(plan.window_capacity)
        if plan.utilization(window_id) == 1.0
    )


def unscheduled_by_reason(plan: CapacityPlan) -> dict[UnscheduledReason, int]:
    summary = {reason: 0 for reason in UnscheduledReason}
    for item in plan.unscheduled:
        summary[item.reason] += item.units
    return summary
