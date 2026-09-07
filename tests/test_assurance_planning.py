from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from reconforge.assurance.planning import (
    AnalysisLaneCapacityPlanner,
    CapacityWindow,
    Demand,
    UnscheduledReason,
    allocations_by_demand,
    capacity_pressure,
    unscheduled_by_reason,
)


class CapacityPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 7, 12, tzinfo=UTC)
        self.windows = (
            CapacityWindow(
                "lane-a",
                self.now,
                self.now + timedelta(hours=2),
                3,
                frozenset({"review", "priority"}),
            ),
            CapacityWindow(
                "lane-b",
                self.now + timedelta(hours=1),
                self.now + timedelta(hours=4),
                4,
                frozenset({"review"}),
            ),
        )

    def test_allocates_by_priority_and_splits_demand(self) -> None:
        planner = AnalysisLaneCapacityPlanner(self.windows)
        plan = planner.plan(
            (
                Demand(
                    "normal",
                    self.now,
                    self.now + timedelta(hours=4),
                    3,
                    1,
                    frozenset({"review"}),
                ),
                Demand(
                    "urgent",
                    self.now,
                    self.now + timedelta(hours=3),
                    5,
                    9,
                    frozenset({"review"}),
                ),
            ),
            generated_at=self.now,
        )
        grouped = allocations_by_demand(plan)
        self.assertEqual([item.units for item in grouped["urgent"]], [3, 2])
        self.assertEqual(plan.scheduled_units, 7)
        self.assertEqual(plan.unscheduled_units, 1)
        self.assertEqual(plan.unscheduled[0].reason, UnscheduledReason.CAPACITY_EXHAUSTED)
        self.assertEqual(capacity_pressure(plan), ("lane-a", "lane-b"))
        self.assertEqual(plan.utilization("lane-a"), 1.0)

    def test_reports_capability_and_horizon_failures(self) -> None:
        planner = AnalysisLaneCapacityPlanner(self.windows)
        plan = planner.plan(
            (
                Demand(
                    "specialist",
                    self.now,
                    self.now + timedelta(hours=1),
                    1,
                    2,
                    frozenset({"medical"}),
                ),
                Demand(
                    "expired",
                    self.now - timedelta(hours=3),
                    self.now - timedelta(hours=1),
                    2,
                    1,
                ),
            ),
            generated_at=self.now,
        )
        self.assertEqual(
            tuple(item.reason for item in plan.unscheduled),
            (
                UnscheduledReason.NO_COMPATIBLE_WINDOW,
                UnscheduledReason.OUTSIDE_HORIZON,
            ),
        )
        summary = unscheduled_by_reason(plan)
        self.assertEqual(summary[UnscheduledReason.NO_COMPATIBLE_WINDOW], 1)
        self.assertEqual(summary[UnscheduledReason.OUTSIDE_HORIZON], 2)
        self.assertIn("has passed", plan.unscheduled[1].explanation)

    def test_rejects_invalid_windows_demands_and_duplicates(self) -> None:
        with self.assertRaises(ValueError):
            CapacityWindow("", self.now, self.now + timedelta(hours=1), 1)
        with self.assertRaises(ValueError):
            CapacityWindow("x", self.now, self.now, 1)
        with self.assertRaises(ValueError):
            CapacityWindow("x", self.now, self.now + timedelta(hours=1), 0)
        with self.assertRaises(ValueError):
            CapacityWindow("x", datetime(2026, 1, 1), self.now, 1)
        with self.assertRaises(ValueError):
            Demand("", self.now, self.now + timedelta(hours=1), 1, 1)
        with self.assertRaises(ValueError):
            Demand("x", self.now, self.now, 1, 1)
        with self.assertRaises(ValueError):
            Demand("x", self.now, self.now + timedelta(hours=1), 0, 1)
        with self.assertRaises(ValueError):
            Demand("x", self.now, self.now + timedelta(hours=1), 1, -1)
        with self.assertRaises(ValueError):
            AnalysisLaneCapacityPlanner(())
        with self.assertRaises(ValueError):
            AnalysisLaneCapacityPlanner((self.windows[0], self.windows[0]))

        planner = AnalysisLaneCapacityPlanner(self.windows)
        demand = Demand("dup", self.now, self.now + timedelta(hours=1), 1, 1)
        with self.assertRaises(ValueError):
            planner.plan((demand, demand), generated_at=self.now)

    def test_unknown_utilization_window_raises_key_error(self) -> None:
        plan = AnalysisLaneCapacityPlanner(self.windows).plan((), generated_at=self.now)
        with self.assertRaises(KeyError):
            plan.utilization("missing")


if __name__ == "__main__":
    unittest.main()
