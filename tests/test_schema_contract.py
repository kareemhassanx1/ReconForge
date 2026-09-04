from __future__ import annotations

from decimal import Decimal

import pytest

from reconforge.model import Dataset, Record
from reconforge.profiling import ValueKind, classify_value
from reconforge.schema_contract import (
    ContractResult,
    ContractValueType,
    ContractViolation,
    ContractViolationCode,
    DatasetContract,
    FieldContract,
    format_decimal,
    validate_dataset_contract,
)


def make_dataset(
    rows: tuple[dict[str, str | None], ...],
    *,
    headers: tuple[str, ...] = ("id", "amount", "state"),
    source: str = "orders.csv",
) -> Dataset:
    return Dataset(
        source=source,
        headers=headers,
        records=tuple(Record(source, index + 2, row) for index, row in enumerate(rows)),
    )


def make_contract(*, allow_extra_fields: bool = False) -> DatasetContract:
    return DatasetContract(
        {
            "id": FieldContract(
                nullable=False,
                unique=True,
                allowed_types=(ContractValueType.INTEGER,),
            ),
            "amount": FieldContract(
                nullable=False,
                allowed_types=(ContractValueType.INTEGER, ContractValueType.DECIMAL),
                minimum="0",
                maximum="1000",
            ),
            "state": FieldContract(
                required=False,
                nullable=True,
                allowed_types=(ContractValueType.TEXT,),
                allowed_values=("new", "paid"),
                pattern=r"[a-z]+",
                min_length=3,
                max_length=4,
            ),
        },
        allow_extra_fields=allow_extra_fields,
    )


@pytest.mark.parametrize(
    ("value", "kind", "contract_type"),
    [
        ("TRUE", ValueKind.BOOLEAN, ContractValueType.BOOLEAN),
        ("+001", ValueKind.INTEGER, ContractValueType.INTEGER),
        (".50", ValueKind.DECIMAL, ContractValueType.DECIMAL),
        ("2026-09-04", ValueKind.DATE, ContractValueType.DATE),
        ("2026-02-30", ValueKind.TEXT, ContractValueType.TEXT),
    ],
)
def test_contract_value_types_share_profile_classification(
    value: str,
    kind: ValueKind,
    contract_type: ContractValueType,
) -> None:
    assert classify_value(value) is kind
    assert contract_type.accepts(value)
    assert all(other.accepts(value) is (other.value == kind.value) for other in ContractValueType)


def test_contract_value_type_named_has_stable_diagnostics() -> None:
    assert ContractValueType.named(" DATE ") is ContractValueType.DATE
    with pytest.raises(ValueError, match="boolean, integer, decimal, date, text"):
        ContractValueType.named("timestamp")
    with pytest.raises(TypeError, match="must be a string"):
        ContractValueType.named(1)  # type: ignore[arg-type]


def test_field_and_dataset_contracts_are_deeply_immutable_and_canonical() -> None:
    source_fields = {
        "z": FieldContract(allowed_values=("z", "a")),
        "a": FieldContract(allowed_types=(ContractValueType.TEXT, ContractValueType.BOOLEAN)),
    }
    contract = DatasetContract(source_fields)
    source_fields.clear()

    assert tuple(contract.fields) == ("a", "z")
    assert contract.field("z").allowed_values == ("a", "z")
    assert contract.field("a").allowed_types == (
        ContractValueType.BOOLEAN,
        ContractValueType.TEXT,
    )
    with pytest.raises(TypeError):
        contract.fields["other"] = FieldContract()  # type: ignore[index]
    with pytest.raises(KeyError, match="not declared"):
        contract.field("other")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"required": 1}, "required must be a boolean"),
        ({"nullable": 0}, "nullable must be a boolean"),
        ({"unique": "yes"}, "unique must be a boolean"),
        ({"allowed_types": ()}, "at least one allowed type"),
        (
            {"allowed_types": (ContractValueType.TEXT, ContractValueType.TEXT)},
            "must be unique",
        ),
        ({"allowed_values": ()}, "cannot be empty"),
        ({"allowed_values": ("x", "x")}, "must be unique"),
        ({"min_length": True}, "non-negative integer"),
        ({"min_length": 5, "max_length": 4}, "cannot exceed"),
        ({"minimum": "NaN", "allowed_types": (ContractValueType.DECIMAL,)}, "finite"),
        (
            {
                "minimum": "2",
                "maximum": "1",
                "allowed_types": (ContractValueType.INTEGER,),
            },
            "minimum cannot exceed",
        ),
        (
            {"minimum": "0", "allowed_types": (ContractValueType.TEXT,)},
            "only integer or decimal",
        ),
        (
            {"pattern": "[", "allowed_types": (ContractValueType.TEXT,)},
            "invalid field contract pattern",
        ),
        (
            {"pattern": ".+", "allowed_types": (ContractValueType.DATE,)},
            "require the text value type",
        ),
        (
            {"allowed_values": ("x",), "allowed_types": (ContractValueType.INTEGER,)},
            "does not match an allowed type",
        ),
        (
            {"allowed_values": ("xx",), "pattern": "x"},
            "does not match pattern",
        ),
    ],
)
def test_field_contract_rejects_invalid_or_contradictory_definitions(
    arguments: dict[str, object],
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        FieldContract(**arguments)  # type: ignore[arg-type]


def test_dataset_contract_rejects_invalid_fields_and_flags() -> None:
    with pytest.raises(ValueError, match="at least one field"):
        DatasetContract({})
    with pytest.raises(ValueError, match="without surrounding whitespace"):
        DatasetContract({" id ": FieldContract()})
    with pytest.raises(TypeError, match="must be a FieldContract"):
        DatasetContract({"id": object()})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="must be a boolean"):
        DatasetContract({"id": FieldContract()}, allow_extra_fields=1)  # type: ignore[arg-type]


def test_valid_dataset_collects_no_issues() -> None:
    dataset = make_dataset(
        (
            {"id": "1", "amount": "10", "state": "new"},
            {"id": "2", "amount": "10.50", "state": None},
        )
    )

    result = validate_dataset_contract(dataset, make_contract())

    assert result == ContractResult(
        source="orders.csv",
        row_count=2,
        checked_fields=("amount", "id", "state"),
        missing_fields=(),
        extra_fields=(),
        violations=(),
    )
    assert result.is_valid
    assert result.issue_count == 0


def test_validation_reports_required_missing_and_disallowed_extra_fields() -> None:
    dataset = make_dataset(
        ({"state": "new", "memo": "ok"},),
        headers=("state", "memo"),
    )

    strict = validate_dataset_contract(dataset, make_contract())
    permissive = validate_dataset_contract(dataset, make_contract(allow_extra_fields=True))

    assert strict.missing_fields == ("amount", "id")
    assert strict.extra_fields == ("memo",)
    assert strict.issue_count == 3
    assert not strict.is_valid
    assert permissive.extra_fields == ()
    assert permissive.issue_count == 2


def test_validation_collects_every_applicable_row_constraint() -> None:
    contract = DatasetContract(
        {
            "name": FieldContract(
                nullable=False,
                allowed_values=("abc", "def"),
                pattern="[a-z]+",
                min_length=3,
                max_length=3,
            ),
            "score": FieldContract(
                nullable=False,
                allowed_types=(ContractValueType.INTEGER, ContractValueType.DECIMAL),
                minimum="0",
                maximum="10",
            ),
        }
    )
    dataset = make_dataset(
        (
            {"name": None, "score": "not-a-number"},
            {"name": "A", "score": "-1"},
            {"name": "abcd", "score": "11.5"},
        ),
        headers=("name", "score"),
    )

    result = validate_dataset_contract(dataset, contract)

    assert [(item.row_number, item.field, item.code) for item in result.violations] == [
        (2, "name", ContractViolationCode.NULL_NOT_ALLOWED),
        (2, "score", ContractViolationCode.TYPE_MISMATCH),
        (3, "name", ContractViolationCode.LENGTH_BELOW_MINIMUM),
        (3, "name", ContractViolationCode.PATTERN_MISMATCH),
        (3, "name", ContractViolationCode.VALUE_NOT_ALLOWED),
        (3, "score", ContractViolationCode.VALUE_BELOW_MINIMUM),
        (4, "name", ContractViolationCode.LENGTH_ABOVE_MAXIMUM),
        (4, "name", ContractViolationCode.VALUE_NOT_ALLOWED),
        (4, "score", ContractViolationCode.VALUE_ABOVE_MAXIMUM),
    ]


def test_unique_constraint_ignores_null_and_marks_every_duplicate_row() -> None:
    dataset = make_dataset(
        (
            {"id": None},
            {"id": None},
            {"id": "A"},
            {"id": "A"},
            {"id": "B"},
        ),
        headers=("id",),
    )
    contract = DatasetContract({"id": FieldContract(required=True, nullable=True, unique=True)})

    result = validate_dataset_contract(dataset, contract)

    duplicates = [
        item for item in result.violations if item.code is ContractViolationCode.DUPLICATE_VALUE
    ]
    assert [(item.row_number, item.value) for item in duplicates] == [(4, "A"), (5, "A")]
    assert all("rows 4, 5" in item.message for item in duplicates)


def test_missing_record_field_is_a_row_violation() -> None:
    dataset = make_dataset(({"id": "1"},), headers=("id", "state"))
    contract = DatasetContract(
        {
            "id": FieldContract(allowed_types=(ContractValueType.INTEGER,)),
            "state": FieldContract(),
        }
    )

    result = validate_dataset_contract(dataset, contract)

    assert result.missing_fields == ()
    assert result.violations[0].code is ContractViolationCode.FIELD_MISSING
    assert result.violations[0].field == "state"


def test_validation_is_deterministic_across_record_and_mapping_order() -> None:
    first_records = (
        Record("data.csv", 4, {"b": "x", "a": None}),
        Record("data.csv", 2, {"a": None, "b": "x"}),
    )
    second_records = tuple(reversed(first_records))
    contract = DatasetContract(
        {
            "b": FieldContract(unique=True),
            "a": FieldContract(nullable=False),
        }
    )

    first = validate_dataset_contract(Dataset("data.csv", ("b", "a"), first_records), contract)
    second = validate_dataset_contract(Dataset("data.csv", ("a", "b"), second_records), contract)

    assert first == second


def test_validation_rejects_ambiguous_programmatic_dataset_values() -> None:
    bad_record = Record("data.csv", 2, {"id": ["not", "a", "cell"]})  # type: ignore[dict-item]
    dataset = Dataset("data.csv", ("id",), (bad_record,))

    with pytest.raises(TypeError, match=r"row 2 field 'id'.*string or null"):
        validate_dataset_contract(dataset, DatasetContract({"id": FieldContract()}))


def test_validation_never_mutates_dataset_values() -> None:
    values = {"id": "001", "amount": "1.50", "state": "new"}
    dataset = make_dataset((values,))
    before = dict(dataset.records[0].values)

    validate_dataset_contract(dataset, make_contract())

    assert dict(dataset.records[0].values) == before


def test_text_constraints_apply_only_to_profile_classified_text_union_members() -> None:
    contract = DatasetContract(
        {
            "value": FieldContract(
                allowed_types=(ContractValueType.BOOLEAN, ContractValueType.TEXT),
                pattern="[a-z]+",
                min_length=3,
            )
        }
    )
    dataset = make_dataset(
        ({"value": "TRUE"}, {"value": "AB"}),
        headers=("value",),
    )

    result = validate_dataset_contract(dataset, contract)

    assert [(item.row_number, item.code) for item in result.violations] == [
        (3, ContractViolationCode.LENGTH_BELOW_MINIMUM),
        (3, ContractViolationCode.PATTERN_MISMATCH),
    ]


def test_decimal_format_is_canonical_and_bounded_for_extreme_exponents() -> None:
    assert format_decimal(Decimal("1.2300")) == "1.23"
    assert format_decimal(Decimal("-0")) == "0"
    assert format_decimal(Decimal("0.0000001")) == "0.0000001"
    rendered = format_decimal(Decimal("1E+100000000"))
    assert rendered == "1E+100000000"
    assert len(rendered) < 20


def test_contract_result_rejects_impossible_semantic_state() -> None:
    violation = ContractViolation(
        source="data.csv",
        row_number=2,
        field="id",
        value=None,
        code=ContractViolationCode.NULL_NOT_ALLOWED,
        message="null value is not allowed",
    )
    with pytest.raises(ValueError, match="not a checked field"):
        ContractResult("data.csv", 2, (), ("id",), (), (violation,))


def test_contract_result_accepts_physical_row_numbers_larger_than_record_count() -> None:
    violation = ContractViolation(
        source="data.csv",
        row_number=101,
        field="id",
        value=None,
        code=ContractViolationCode.NULL_NOT_ALLOWED,
        message="null value is not allowed",
    )

    result = ContractResult("data.csv", 1, ("id",), (), (), (violation,))

    assert result.violations[0].row_number == 101
