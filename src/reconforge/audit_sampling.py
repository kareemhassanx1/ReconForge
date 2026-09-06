"""Stratified, reproducible sampling of reconciliation results."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum


class ResultClass(StrEnum):
    MATCHED = "matched"
    DIFFERENT = "different"
    MISSING = "missing"
    DUPLICATE = "duplicate"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class SamplingRecord:
    record_id: str
    result_class: ResultClass
    source: str
    value_exposure: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not self.record_id.strip() or not self.source.strip():
            raise ValueError("record_id and source must not be blank")
        if self.value_exposure < 0:
            raise ValueError("value_exposure must not be negative")


@dataclass(frozen=True, slots=True)
class SamplingPolicy:
    sample_rate: Decimal = Decimal("0.05")
    minimum_per_stratum: int = 1
    maximum_total: int = 500
    material_threshold: Decimal = Decimal("1000")
    include_all_material: bool = True
    seed: str = "reconforge-v1"

    def __post_init__(self) -> None:
        if not Decimal("0") <= self.sample_rate <= Decimal("1"):
            raise ValueError("sample_rate must be between zero and one")
        if self.minimum_per_stratum < 0 or self.maximum_total < 1:
            raise ValueError("sample limits are invalid")
        if self.material_threshold < 0:
            raise ValueError("material_threshold must not be negative")
        if not self.seed:
            raise ValueError("seed must not be blank")


DEFAULT_SAMPLING_POLICY = SamplingPolicy()


@dataclass(frozen=True, slots=True)
class SampledRecord:
    record: SamplingRecord
    stratum: str
    selection_reason: str
    rank: str


@dataclass(frozen=True, slots=True)
class SamplingPlan:
    selected: tuple[SampledRecord, ...]
    population: int
    strata_population: Mapping[str, int]
    truncated: bool


def _stratum(record: SamplingRecord) -> str:
    return f"{record.result_class.value}:{record.source}"


def _rank(record: SamplingRecord, seed: str) -> str:
    material = f"{seed}\0{record.result_class.value}\0{record.source}\0{record.record_id}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_sampling_plan(
    records: Iterable[SamplingRecord],
    *,
    policy: SamplingPolicy = DEFAULT_SAMPLING_POLICY,
) -> SamplingPlan:
    """Select material and stratified records independent of input order."""

    unique: dict[str, SamplingRecord] = {}
    for record in records:
        prior = unique.get(record.record_id)
        if prior is not None and prior != record:
            raise ValueError(f"conflicting record id: {record.record_id}")
        unique[record.record_id] = record
    grouped: dict[str, list[SamplingRecord]] = {}
    for record in unique.values():
        grouped.setdefault(_stratum(record), []).append(record)
    selected: dict[str, SampledRecord] = {}
    if policy.include_all_material:
        for record in unique.values():
            if record.value_exposure >= policy.material_threshold:
                selected[record.record_id] = SampledRecord(
                    record,
                    _stratum(record),
                    "material-exposure",
                    _rank(record, policy.seed),
                )
    for stratum in sorted(grouped):
        population = grouped[stratum]
        target = max(
            policy.minimum_per_stratum,
            int(
                (Decimal(len(population)) * policy.sample_rate).to_integral_value(
                    rounding=ROUND_CEILING
                )
            ),
        )
        ranked = sorted(
            population,
            key=lambda record: (_rank(record, policy.seed), record.record_id),
        )
        for record in ranked[:target]:
            selected.setdefault(
                record.record_id,
                SampledRecord(record, stratum, "stratified-sample", _rank(record, policy.seed)),
            )
    ordered = sorted(
        selected.values(),
        key=lambda item: (
            item.selection_reason != "material-exposure",
            item.stratum,
            item.rank,
            item.record.record_id,
        ),
    )
    truncated = len(ordered) > policy.maximum_total
    return SamplingPlan(
        selected=tuple(ordered[: policy.maximum_total]),
        population=len(unique),
        strata_population={key: len(grouped[key]) for key in sorted(grouped)},
        truncated=truncated,
    )


def sampling_coverage(plan: SamplingPlan) -> Mapping[str, Decimal]:
    """Return selected-to-population ratios per stratum."""

    selected: dict[str, int] = {}
    for item in plan.selected:
        selected[item.stratum] = selected.get(item.stratum, 0) + 1
    return {
        stratum: (Decimal(selected.get(stratum, 0)) / Decimal(population)).quantize(
            Decimal("0.0001")
        )
        for stratum, population in plan.strata_population.items()
    }


def sample_manifest(plan: SamplingPlan) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "record_id": item.record.record_id,
            "result_class": item.record.result_class.value,
            "source": item.record.source,
            "selection_reason": item.selection_reason,
            "rank": item.rank,
        }
        for item in plan.selected
    )


def sampling_fingerprint(plan: SamplingPlan) -> str:
    """Hash the selected record identities for later audit comparison."""

    material = "\n".join(
        f"{item.record.record_id}:{item.rank}" for item in plan.selected
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
