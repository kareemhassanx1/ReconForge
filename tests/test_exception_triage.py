from datetime import UTC, datetime, timedelta
from decimal import Decimal

from reconforge.exception_triage import (
    ExceptionKind,
    ReconException,
    TriagePolicy,
    build_triage_queue,
    triage_counts,
)


def test_triage_orders_material_old_schema_exception_first() -> None:
    now = datetime(2026, 9, 6, tzinfo=UTC)
    queue = build_triage_queue(
        [
            ReconException("a", ExceptionKind.VALUE_DIFFERENCE, ("1",), now),
            ReconException(
                "b",
                ExceptionKind.SCHEMA_DRIFT,
                ("2",),
                now - timedelta(days=4),
                Decimal("5000"),
            ),
        ],
        policy=TriagePolicy(field_weights={"account": 10}),
        now=now,
    )
    assert queue[0].exception.exception_id == "b"
    assert triage_counts(queue)["total"] == 2
