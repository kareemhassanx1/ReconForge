from decimal import Decimal

from reconforge.audit_sampling import (
    ResultClass,
    SamplingPolicy,
    SamplingRecord,
    build_sampling_plan,
    sampling_coverage,
)


def test_sampling_is_stable_and_includes_material_records() -> None:
    records = [
        SamplingRecord(str(index), ResultClass.MATCHED, "erp", Decimal(index * 100))
        for index in range(1, 11)
    ]
    policy = SamplingPolicy(sample_rate=Decimal("0.1"), material_threshold=Decimal("900"))
    first = build_sampling_plan(records, policy=policy)
    second = build_sampling_plan(reversed(records), policy=policy)
    assert first.selected == second.selected
    assert {item.record.record_id for item in first.selected} >= {"9", "10"}
    assert sampling_coverage(first)["matched:erp"] >= Decimal("0.2")
