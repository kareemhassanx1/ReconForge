from datetime import UTC, datetime, timedelta

from reconforge.reconciliation_sla import ReconciliationRun, RunState, ServiceObjective, assess_job


def test_job_assessment_reports_late_failed_run() -> None:
    now = datetime(2026, 9, 6, 12, tzinfo=UTC)
    runs = [
        ReconciliationRun(
            "1",
            "daily",
            now - timedelta(days=1),
            now - timedelta(days=1),
            now - timedelta(days=1) + timedelta(minutes=5),
            RunState.SUCCEEDED,
        ),
        ReconciliationRun(
            "2",
            "daily",
            now - timedelta(hours=1),
            now - timedelta(minutes=40),
            now - timedelta(minutes=20),
            RunState.FAILED,
        ),
    ]
    result = assess_job(runs, objective=ServiceObjective(minimum_success_rate=0.75), now=now)
    assert not result.compliant
    assert result.success_rate == 0.5
    assert "success-rate-below-objective" in result.reasons
