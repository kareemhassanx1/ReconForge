"""Conservative dataset-contract inference from deterministic profiles."""

from __future__ import annotations

from dataclasses import dataclass

from reconforge.model import Dataset
from reconforge.profiling import (
    DatasetProfile,
    FieldProfile,
    ProfileOptions,
    ValueKind,
    profile_dataset,
)
from reconforge.schema_contract import ContractValueType, DatasetContract, FieldContract

_MAX_ALLOWED_VALUES_LIMIT = 1_000
_VALUE_TYPE_BY_KIND = {
    ValueKind.BOOLEAN: ContractValueType.BOOLEAN,
    ValueKind.INTEGER: ContractValueType.INTEGER,
    ValueKind.DECIMAL: ContractValueType.DECIMAL,
    ValueKind.DATE: ContractValueType.DATE,
    ValueKind.TEXT: ContractValueType.TEXT,
}


@dataclass(frozen=True, slots=True)
class ContractInferenceOptions:
    """Controls which observed properties become enforceable constraints.

    Type and field-presence inference are always enabled.  Other constraints
    default to conservative settings because uniqueness, observed ranges, and
    small value sets can easily be accidental properties of a sample.
    """

    allow_extra_fields: bool = False
    required_fields: bool = True
    infer_nullability: bool = True
    infer_uniqueness: bool = False
    infer_numeric_bounds: bool = False
    infer_text_lengths: bool = False
    allowed_values_limit: int = 0

    def __post_init__(self) -> None:
        for value, label in (
            (self.allow_extra_fields, "allow_extra_fields"),
            (self.required_fields, "required_fields"),
            (self.infer_nullability, "infer_nullability"),
            (self.infer_uniqueness, "infer_uniqueness"),
            (self.infer_numeric_bounds, "infer_numeric_bounds"),
            (self.infer_text_lengths, "infer_text_lengths"),
        ):
            if type(value) is not bool:
                raise TypeError(f"contract inference {label} must be a boolean")
        if type(self.allowed_values_limit) is not int:
            raise TypeError("contract inference allowed_values_limit must be an integer")
        if not 0 <= self.allowed_values_limit <= _MAX_ALLOWED_VALUES_LIMIT:
            raise ValueError(
                "contract inference allowed_values_limit must be between "
                f"0 and {_MAX_ALLOWED_VALUES_LIMIT}"
            )


def infer_contract_from_profile(
    profile: DatasetProfile,
    options: ContractInferenceOptions | None = None,
) -> DatasetContract:
    """Infer a contract without making unrequested sample-specific promises."""

    if not isinstance(profile, DatasetProfile):
        raise TypeError("profile must be a DatasetProfile instance")
    selected = ContractInferenceOptions() if options is None else options
    if not isinstance(selected, ContractInferenceOptions):
        raise TypeError("options must be a ContractInferenceOptions instance")
    return DatasetContract(
        fields={field.name: _infer_field(field, selected) for field in profile.fields},
        allow_extra_fields=selected.allow_extra_fields,
    )


def infer_dataset_contract(
    dataset: Dataset,
    options: ContractInferenceOptions | None = None,
) -> DatasetContract:
    """Profile ``dataset`` with sufficient samples and infer its contract."""

    if not isinstance(dataset, Dataset):
        raise TypeError("dataset must be a Dataset instance")
    selected = ContractInferenceOptions() if options is None else options
    if not isinstance(selected, ContractInferenceOptions):
        raise TypeError("options must be a ContractInferenceOptions instance")
    profile_options = ProfileOptions(
        top_values_limit=selected.allowed_values_limit,
        examples_limit=0,
    )
    return infer_contract_from_profile(profile_dataset(dataset, profile_options), selected)


def _infer_field(
    profile: FieldProfile,
    options: ContractInferenceOptions,
) -> FieldContract:
    value_types = _observed_value_types(profile)
    numeric_only = bool(value_types) and set(value_types) <= {
        ContractValueType.INTEGER,
        ContractValueType.DECIMAL,
    }
    text_only = value_types == (ContractValueType.TEXT,)

    minimum: str | None = None
    maximum: str | None = None
    if options.infer_numeric_bounds and numeric_only and profile.numeric_range is not None:
        minimum, maximum = profile.numeric_range

    min_length: int | None = None
    max_length: int | None = None
    if options.infer_text_lengths and text_only and profile.non_null_count:
        min_length = profile.min_length
        max_length = profile.max_length

    allowed_values: tuple[str, ...] | None = None
    limit = options.allowed_values_limit
    if (
        limit
        and profile.distinct_count
        and profile.distinct_count <= limit
        and len(profile.top_values) == profile.distinct_count
    ):
        allowed_values = tuple(sorted(item.value for item in profile.top_values))

    return FieldContract(
        required=options.required_fields,
        nullable=(
            profile.total_count == 0 or profile.null_count > 0
            if options.infer_nullability
            else True
        ),
        unique=(
            options.infer_uniqueness
            and profile.non_null_count > 0
            and profile.distinct_count == profile.non_null_count
        ),
        allowed_types=value_types or (ContractValueType.TEXT,),
        allowed_values=allowed_values,
        min_length=min_length,
        max_length=max_length,
        minimum=minimum,
        maximum=maximum,
    )


def _observed_value_types(profile: FieldProfile) -> tuple[ContractValueType, ...]:
    return tuple(
        value_type for kind, value_type in _VALUE_TYPE_BY_KIND.items() if profile.count(kind) > 0
    )
