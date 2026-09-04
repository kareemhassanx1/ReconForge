from __future__ import annotations

import csv
import io
import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

from reconforge.contract_report import (
    CONTRACT_REPORT_VERSION,
    contract_result_fingerprint,
    contract_result_from_dict,
    contract_result_to_dict,
    load_json_contract_report,
    render_contract_report,
    write_contract_report,
)
from reconforge.errors import InputError
from reconforge.exporters import ReportFormat
from reconforge.model import Dataset, Record
from reconforge.schema_contract import (
    ContractResult,
    ContractValueType,
    DatasetContract,
    FieldContract,
    validate_dataset_contract,
)


def make_result() -> ContractResult:
    source = "=orders<&>.csv"
    dataset = Dataset(
        source,
        ("id", "note", "unexpected"),
        (
            Record(source, 2, {"id": None, "note": "<script>", "unexpected": "x"}),
            Record(source, 3, {"id": "1", "note": "=cmd", "unexpected": "y"}),
        ),
    )
    contract = DatasetContract(
        {
            "amount": FieldContract(
                nullable=False,
                allowed_types=(ContractValueType.DECIMAL,),
            ),
            "id": FieldContract(
                nullable=False,
                allowed_types=(ContractValueType.INTEGER,),
            ),
            "note": FieldContract(allowed_values=("ok",)),
        }
    )
    return validate_dataset_contract(dataset, contract)


def test_contract_report_round_trips_all_semantic_content() -> None:
    result = make_result()

    payload = contract_result_to_dict(result)
    restored = contract_result_from_dict(payload)

    assert restored == result
    assert payload["contract_report_version"] == CONTRACT_REPORT_VERSION
    assert payload["status"] == "issues_found"
    assert payload["dataset"] == {"source": "=orders<&>.csv", "row_count": 2}
    assert payload["summary"] == {
        "checked_fields": 2,
        "missing_fields": 1,
        "extra_fields": 1,
        "violations": 3,
        "issues": 5,
    }
    assert payload["schema"] == {
        "checked_fields": ["id", "note"],
        "missing_fields": ["amount"],
        "extra_fields": ["unexpected"],
    }


def test_clean_result_has_explicit_clean_status() -> None:
    result = ContractResult("empty.csv", 0, ("id",), (), (), ())

    assert result.is_valid
    assert contract_result_to_dict(result)["status"] == "clean"


def test_report_serialization_returns_detached_containers() -> None:
    result = make_result()
    payload = contract_result_to_dict(result)

    payload["schema"]["checked_fields"].clear()
    payload["violations"][0]["message"] = "changed"

    assert result.checked_fields == ("id", "note")
    assert result.violations[0].message != "changed"


def test_fingerprint_is_stable_sensitive_and_well_formed() -> None:
    result = make_result()
    first = contract_result_fingerprint(result)
    restored = contract_result_from_dict(contract_result_to_dict(result))
    changed = ContractResult(
        source=result.source,
        row_count=result.row_count + 1,
        checked_fields=result.checked_fields,
        missing_fields=result.missing_fields,
        extra_fields=result.extra_fields,
        violations=result.violations,
    )

    assert first == contract_result_fingerprint(restored)
    assert first != contract_result_fingerprint(changed)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", first)
    with pytest.raises(TypeError, match="ContractResult"):
        contract_result_fingerprint({})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.pop("schema"), "missing required field.*schema"),
        (lambda value: value.update(extra=True), "unknown field.*extra"),
        (
            lambda value: value.update(contract_report_version=2),
            "unsupported contract report version 2",
        ),
        (
            lambda value: value.update(contract_report_version=True),
            "unsupported contract report version True",
        ),
        (lambda value: value.update(status="clean"), "expected 'issues_found'"),
        (lambda value: value["summary"].update(issues=99), "issues is 99; expected 5"),
        (
            lambda value: value["summary"].update(checked_fields=True),
            "checked_fields must be a non-negative integer",
        ),
        (
            lambda value: value["dataset"].update(row_count=True),
            "row_count must be a non-negative integer",
        ),
        (
            lambda value: value["schema"].update(checked_fields=["note", "id"]),
            "checked_fields must be sorted",
        ),
        (
            lambda value: value["schema"].update(extra_fields=["unexpected", "unexpected"]),
            "extra_fields must contain unique",
        ),
        (
            lambda value: value["violations"][0].update(code="other"),
            r"violations\[0\]\.code is unknown",
        ),
        (
            lambda value: value["violations"][0].update(row_number=0),
            "row_number must be a positive integer",
        ),
        (
            lambda value: value["violations"][0].update(field="unexpected"),
            "is not a checked field",
        ),
        (
            lambda value: value["violations"][0].update(value=1),
            r"violations\[0\]\.value must be a string or null",
        ),
    ],
)
def test_report_parser_rejects_structural_and_semantic_inconsistency(
    mutate,
    message: str,
) -> None:
    payload = contract_result_to_dict(make_result())
    mutate(payload)

    with pytest.raises((TypeError, ValueError), match=message):
        contract_result_from_dict(payload)


def test_report_parser_rejects_noncanonical_violation_order() -> None:
    payload = contract_result_to_dict(make_result())
    payload["violations"].reverse()

    with pytest.raises(ValueError, match="violations must use canonical"):
        contract_result_from_dict(payload)


def test_report_parser_does_not_modify_the_input_mapping() -> None:
    payload = contract_result_to_dict(make_result())
    original = deepcopy(payload)

    contract_result_from_dict(payload)

    assert payload == original


def test_json_renderer_is_canonical_pretty_utf8() -> None:
    result = make_result()

    rendered = render_contract_report(result, ReportFormat.JSON)

    assert json.loads(rendered) == contract_result_to_dict(result)
    assert rendered.endswith("\n")
    assert "orders<&>" in rendered
    assert rendered == render_contract_report(result, "json")


def test_csv_renderer_has_schema_rows_and_protects_formulas() -> None:
    result = make_result()

    rendered = render_contract_report(result, "csv")
    rows = list(csv.DictReader(io.StringIO(rendered)))

    assert rendered.startswith("status,issue_type,source,row_number,field,value,code,message\n")
    assert [row["issue_type"] for row in rows] == [
        "summary",
        "missing_field",
        "extra_field",
        "row_violation",
        "row_violation",
        "row_violation",
    ]
    assert all(row["source"] == "'=orders<&>.csv" for row in rows)
    formula_row = next(row for row in rows if row["value"] == "'=cmd")
    assert formula_row["code"] == "value_not_allowed"


def test_html_renderer_is_self_contained_and_escapes_untrusted_content() -> None:
    rendered = render_contract_report(make_result(), "html")

    assert rendered.startswith('<!doctype html>\n<html lang="en">')
    assert "<style>" in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "orders&lt;&amp;&gt;.csv" in rendered
    assert "https://" not in rendered


def test_renderer_rejects_unknown_format_and_argument_types() -> None:
    with pytest.raises(ValueError, match="expected one of: json, csv, html"):
        render_contract_report(make_result(), "pdf")
    with pytest.raises(TypeError, match="ContractResult"):
        render_contract_report({}, "json")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ReportFormat"):
        render_contract_report(make_result(), object())  # type: ignore[arg-type]


def test_write_contract_report_is_atomic(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "report.html"

    returned = write_contract_report(make_result(), destination, ReportFormat.HTML)

    assert returned == destination.resolve()
    assert "Dataset Contract Report" in destination.read_text(encoding="utf-8")
    assert list(destination.parent.glob(f".{destination.name}.*.tmp")) == []


def test_json_loader_accepts_bom_and_reordered_objects(tmp_path: Path) -> None:
    payload = contract_result_to_dict(make_result())
    source = tmp_path / "report.json"
    source.write_text(
        json.dumps(dict(reversed(tuple(payload.items()))), ensure_ascii=False),
        encoding="utf-8-sig",
    )

    loaded = load_json_contract_report(source)

    assert loaded == make_result()


def test_json_loader_rejects_duplicate_keys_at_any_depth(tmp_path: Path) -> None:
    text = json.dumps(contract_result_to_dict(make_result())).replace(
        '"issues": 5',
        '"issues": 5, "issues": 5',
        1,
    )
    source = tmp_path / "duplicate.json"
    source.write_text(text, encoding="utf-8")

    with pytest.raises(InputError, match="duplicate object key 'issues'"):
        load_json_contract_report(source)


def test_json_loader_has_clean_io_and_syntax_diagnostics(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="does not exist"):
        load_json_contract_report(tmp_path / "missing.json")

    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"contract_report_version":', encoding="utf-8")
    with pytest.raises(InputError, match=r"line 1, column \d+"):
        load_json_contract_report(malformed)

    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xff\xfe\xfd")
    with pytest.raises(InputError, match="not valid UTF-8"):
        load_json_contract_report(binary)


def test_json_loader_rejects_non_object_nonfinite_and_depth(tmp_path: Path) -> None:
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(InputError, match="top-level.*must be an object"):
        load_json_contract_report(array)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"contract_report_version": NaN}', encoding="utf-8")
    with pytest.raises(InputError, match="non-finite JSON number"):
        load_json_contract_report(nonfinite)

    deep = tmp_path / "deep.json"
    deep.write_text("[" * 101 + "0" + "]" * 101, encoding="utf-8")
    with pytest.raises(InputError, match="maximum nesting depth"):
        load_json_contract_report(deep)


def test_report_from_dict_rejects_wrong_public_types_and_depth() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        contract_result_from_dict([])  # type: ignore[arg-type]

    nested: object = 0
    for _ in range(101):
        nested = [nested]
    with pytest.raises(ValueError, match="maximum nesting depth"):
        contract_result_from_dict({"unexpected": nested})


def test_load_wraps_semantic_errors_as_input_error(tmp_path: Path) -> None:
    payload = contract_result_to_dict(make_result())
    payload["status"] = "clean"
    source = tmp_path / "semantic.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InputError, match=r"semantic\.json.*expected 'issues_found'"):
        load_json_contract_report(source)
