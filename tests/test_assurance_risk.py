from __future__ import annotations

import unittest

from reconforge.assurance.risk import (
    ControlRule,
    Direction,
    Disposition,
    ReconciliationBatchRiskEngine,
    Severity,
    Signal,
    prioritize_reviews,
    summarize_assessments,
)


class RiskEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ReconciliationBatchRiskEngine(
            (
                ControlRule(
                    "latency",
                    "Decision latency",
                    30,
                    Direction.MAXIMUM,
                    Severity.MATERIAL,
                    2,
                ),
                ControlRule(
                    "evidence",
                    "Evidence coverage",
                    90,
                    Direction.MINIMUM,
                    Severity.BLOCKING,
                    3,
                ),
                ControlRule(
                    "exceptions",
                    "Open exceptions",
                    2,
                    Direction.MAXIMUM,
                    Severity.ADVISORY,
                ),
            )
        )

    def test_accepts_complete_compliant_subject(self) -> None:
        result = self.engine.assess(
            "subject-1",
            (
                Signal("latency", 12, ("clock-1",)),
                Signal("evidence", 98, ("proof-1",)),
                Signal("exceptions", 0),
            ),
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.disposition, Disposition.ACCEPT)
        self.assertEqual(result.risk_ratio, 0.0)
        self.assertEqual(result.failed_findings, ())
        self.assertIn("at most 30", result.findings[2].explanation)

    def test_missing_and_blocking_controls_hold_subject(self) -> None:
        missing = self.engine.assess("subject-2", (Signal("latency", 10),))
        blocked = self.engine.assess(
            "subject-3",
            (
                Signal("latency", 10),
                Signal("evidence", 40),
                Signal("exceptions", 0),
            ),
        )
        self.assertEqual(missing.disposition, Disposition.HOLD)
        self.assertEqual(missing.missing_controls, ("evidence", "exceptions"))
        self.assertEqual(blocked.disposition, Disposition.HOLD)
        self.assertEqual(blocked.risk_points, 3)

    def test_material_failure_routes_to_review(self) -> None:
        result = self.engine.assess(
            "subject-4",
            (
                Signal("latency", 31),
                Signal("evidence", 95),
                Signal("exceptions", 0),
            ),
        )
        self.assertEqual(result.disposition, Disposition.REVIEW)
        self.assertAlmostEqual(result.risk_ratio, 2 / 6)
        self.assertEqual(result.failed_findings[0].control_code, "latency")

    def test_batch_summary_and_priority_are_deterministic(self) -> None:
        results = self.engine.assess_many(
            {
                "z-last": (Signal("latency", 10),),
                "a-first": (
                    Signal("latency", 31),
                    Signal("evidence", 99),
                    Signal("exceptions", 0),
                ),
            }
        )
        self.assertEqual(tuple(item.subject_id for item in results), ("a-first", "z-last"))
        self.assertEqual(summarize_assessments(results), {"accept": 0, "review": 1, "hold": 1})
        self.assertEqual(
            tuple(item.subject_id for item in prioritize_reviews(results)), ("z-last", "a-first")
        )

    def test_rejects_invalid_policy_and_signals(self) -> None:
        with self.assertRaises(ValueError):
            ControlRule("", "label", 1, Direction.MAXIMUM, Severity.ADVISORY)
        with self.assertRaises(ValueError):
            ControlRule("x", "", 1, Direction.MAXIMUM, Severity.ADVISORY)
        with self.assertRaises(ValueError):
            ControlRule("x", "label", 1, Direction.MAXIMUM, Severity.ADVISORY, 0)
        with self.assertRaises(ValueError):
            Signal("", 1)
        with self.assertRaises(ValueError):
            Signal("x", 1, ("same", "same"))
        with self.assertRaises(ValueError):
            ReconciliationBatchRiskEngine(())
        with self.assertRaises(ValueError):
            ReconciliationBatchRiskEngine(
                (
                    ControlRule("x", "one", 1, Direction.MAXIMUM, Severity.ADVISORY),
                    ControlRule("x", "two", 2, Direction.MAXIMUM, Severity.ADVISORY),
                )
            )
        with self.assertRaises(ValueError):
            self.engine.assess("", ())
        with self.assertRaises(ValueError):
            self.engine.assess("id", (Signal("missing", 1),))
        with self.assertRaises(ValueError):
            self.engine.assess("id", (Signal("latency", 1), Signal("latency", 2)))


if __name__ == "__main__":
    unittest.main()
