from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

import reconforge
import reconforge.cli as cli_module
from reconforge.cli import main


def test_data_assurance_api_is_exported_from_the_package_root() -> None:
    expected = {
        "CONTRACT_REPORT_VERSION",
        "CONTRACT_VERSION",
        "PROFILE_VERSION",
        "ContractInferenceOptions",
        "ContractResult",
        "ContractValueType",
        "ContractViolation",
        "ContractViolationCode",
        "DatasetContract",
        "DatasetProfile",
        "FieldContract",
        "FieldProfile",
        "ProfileOptions",
        "ValueFrequency",
        "ValueKind",
        "classify_value",
        "contract_from_dict",
        "contract_result_fingerprint",
        "contract_result_from_dict",
        "contract_result_to_dict",
        "contract_to_dict",
        "infer_contract_from_profile",
        "infer_dataset_contract",
        "load_json_contract",
        "load_json_contract_report",
        "load_json_profile",
        "profile_dataset",
        "profile_fingerprint",
        "profile_from_dict",
        "profile_to_dict",
        "render_contract_report",
        "render_profile_report",
        "validate_dataset_contract",
        "write_contract_report",
        "write_json_contract",
        "write_profile_report",
    }

    assert expected <= set(reconforge.__all__)
    assert all(hasattr(reconforge, name) for name in expected)


def write_text(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def write_contract_from(
    tmp_path: Path,
    capsys,
    dataset: Path,
    *extra_arguments: str,
) -> Path:
    contract = tmp_path / "contract.json"
    status = main(
        [
            "contract",
            "infer",
            str(dataset),
            *extra_arguments,
            "--output",
            str(contract),
        ]
    )
    captured = capsys.readouterr()
    assert status == 0
    assert captured.err == ""
    assert contract.exists()
    return contract


def test_profile_prints_canonical_json_with_requested_sample_limits(
    tmp_path: Path,
    capsys,
) -> None:
    dataset = write_text(
        tmp_path,
        "ledger.csv",
        "id,amount,status\n001,10.50,paid\n002,12,pending\n003,,paid\n",
    )

    status = main(
        [
            "profile",
            str(dataset),
            "--top-values",
            "2",
            "--examples",
            "1",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert status == 0
    assert captured.err == ""
    assert payload["profile_version"] == 1
    assert payload["fingerprint"].startswith("sha256:")
    assert payload["record_count"] == 3
    assert payload["options"] == {"top_values_limit": 2, "examples_limit": 1}
    assert [field["name"] for field in payload["fields"]] == ["id", "amount", "status"]
    assert len(payload["fields"][2]["top_values"]) == 2
    assert len(payload["fields"][2]["examples"]) == 1


@pytest.mark.parametrize(
    ("suffix", "expected_prefix"),
    [
        ("json", "{"),
        ("csv", "source,record_count,field,"),
        ("html", "<!doctype html>"),
    ],
)
def test_profile_infers_each_file_report_format(
    tmp_path: Path,
    capsys,
    suffix: str,
    expected_prefix: str,
) -> None:
    dataset = write_text(tmp_path, "input.csv", "id,value\nA,10\n")
    output = tmp_path / "nested" / f"profile.{suffix}"

    status = main(["profile", str(dataset), "--output", str(output)])

    captured = capsys.readouterr()
    assert status == 0
    assert captured.err == ""
    assert "profiled 1 record(s) across 2 field(s)" in captured.out
    assert f"report: {output.resolve()}" in captured.out
    assert output.read_text(encoding="utf-8").startswith(expected_prefix)


def test_profile_explicit_format_overrides_unknown_output_suffix(
    tmp_path: Path,
    capsys,
) -> None:
    dataset = write_text(tmp_path, "input.csv", "id,value\nA,10\n")
    output = tmp_path / "profile.artifact"

    status = main(
        [
            "profile",
            str(dataset),
            "--output",
            str(output),
            "--report-format",
            "csv",
        ]
    )

    assert status == 0
    assert output.read_text(encoding="utf-8").startswith("source,record_count,field,")
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--top-values", "-1"), "top_values_limit must be an integer between 0 and 1000"),
        (("--examples", "1001"), "examples_limit must be an integer between 0 and 1000"),
        (("--report-format", "yaml"), "unknown report format"),
        (("--delimiter", "::"), "delimiter must be exactly one character"),
    ],
)
def test_profile_reports_invalid_options_without_a_traceback(
    tmp_path: Path,
    capsys,
    arguments: tuple[str, ...],
    message: str,
) -> None:
    dataset = write_text(tmp_path, "input.csv", "id,value\nA,10\n")

    status = main(["profile", str(dataset), *arguments])

    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert message in captured.err
    assert "Traceback" not in captured.err


def test_profile_reports_missing_input_and_output_failures(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = main(["profile", str(tmp_path / "missing.csv")])
    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert "does not exist" in captured.err

    dataset = write_text(tmp_path, "input.csv", "id,value\nA,10\n")

    def fail_write(*args, **kwargs):
        raise OSError("simulated profile disk failure")

    monkeypatch.setattr(cli_module, "write_profile_report", fail_write)
    status = main(["profile", str(dataset), "--output", str(tmp_path / "profile.json")])
    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert captured.err == "reconforge: error: simulated profile disk failure\n"


def test_contract_infer_prints_a_self_describing_contract_and_honors_options(
    tmp_path: Path,
    capsys,
) -> None:
    dataset = write_text(
        tmp_path,
        "ledger.csv",
        "id,amount,status,note\n001,10.50,paid,ok\n002,12,pending,review\n",
    )

    status = main(
        [
            "contract",
            "infer",
            str(dataset),
            "--allow-extra-fields",
            "--optional-fields",
            "--permissive-nulls",
            "--infer-unique",
            "--infer-numeric-bounds",
            "--infer-text-lengths",
            "--allowed-values-limit",
            "2",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert status == 0
    assert captured.err == ""
    assert payload["contract_version"] == 1
    assert payload["allow_extra_fields"] is True
    assert list(payload["fields"]) == ["amount", "id", "note", "status"]
    assert payload["fields"]["id"]["required"] is False
    assert payload["fields"]["id"]["nullable"] is True
    assert payload["fields"]["id"]["unique"] is True
    assert payload["fields"]["amount"]["minimum"] == "10.5"
    assert payload["fields"]["amount"]["maximum"] == "12"
    assert payload["fields"]["status"]["allowed_values"] == ["paid", "pending"]
    assert payload["fields"]["note"]["min_length"] == 2
    assert payload["fields"]["note"]["max_length"] == 6


def test_contract_inference_supports_csv_delimiters_and_atomic_output(
    tmp_path: Path,
    capsys,
) -> None:
    dataset = write_text(
        tmp_path,
        "ledger.data",
        "id;amount;status\n001;10.5;paid\n002;12;pending\n",
    )
    contract = write_contract_from(
        tmp_path,
        capsys,
        dataset,
        "--format",
        "csv",
        "--delimiter",
        ";",
    )

    payload = json.loads(contract.read_text(encoding="utf-8"))
    assert payload["contract_version"] == 1
    assert list(payload["fields"]) == ["amount", "id", "status"]
    assert not list(contract.parent.glob(f".{contract.name}.*.tmp"))


def test_contract_inference_handles_a_header_only_dataset_conservatively(
    tmp_path: Path,
    capsys,
) -> None:
    dataset = write_text(tmp_path, "empty.csv", "id,amount\n")

    status = main(["contract", "infer", str(dataset)])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert status == 0
    assert captured.err == ""
    assert list(payload["fields"]) == ["amount", "id"]
    for field in payload["fields"].values():
        assert field["nullable"] is True
        assert field["types"] == ["text"]
        assert field["allowed_values"] is None
        assert field["minimum"] is None
        assert field["maximum"] is None


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--allowed-values-limit", "-1"), "must be between 0 and 1000"),
        (("--allowed-values-limit", "1001"), "must be between 0 and 1000"),
        (("--delimiter", "::"), "delimiter must be exactly one character"),
        (("--format", "xml"), "unknown dataset format"),
    ],
)
def test_contract_infer_returns_two_for_bad_options(
    tmp_path: Path,
    capsys,
    arguments: tuple[str, ...],
    message: str,
) -> None:
    dataset = write_text(tmp_path, "input.csv", "id,value\nA,10\n")

    status = main(["contract", "infer", str(dataset), *arguments])

    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert message in captured.err


def test_contract_check_returns_zero_and_prints_clean_json(
    tmp_path: Path,
    capsys,
) -> None:
    dataset = write_text(
        tmp_path,
        "baseline.csv",
        "id,amount,status\n001,10.5,paid\n002,12,pending\n",
    )
    contract = write_contract_from(tmp_path, capsys, dataset)

    status = main(["contract", "check", str(dataset), str(contract)])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert status == 0
    assert captured.err == ""
    assert payload["contract_report_version"] == 1
    assert payload["status"] == "clean"
    assert payload["summary"]["issues"] == 0
    assert payload["dataset"]["row_count"] == 2


def test_contract_check_returns_one_and_writes_inferred_csv_report(
    tmp_path: Path,
    capsys,
) -> None:
    baseline = write_text(
        tmp_path,
        "baseline.csv",
        "id,amount,status\n001,10.5,paid\n002,12,pending\n",
    )
    contract = write_contract_from(tmp_path, capsys, baseline)
    candidate = write_text(
        tmp_path,
        "candidate.csv",
        "id,amount,status,unexpected\n001,not-a-number,paid,x\n",
    )
    output = tmp_path / "nested" / "contract-report.csv"

    status = main(
        [
            "contract",
            "check",
            str(candidate),
            str(contract),
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    rows = list(csv.DictReader(io.StringIO(output.read_text(encoding="utf-8"))))
    assert status == 1
    assert captured.err == ""
    assert captured.out.startswith("issues_found: ")
    assert f"report: {output.resolve()}" in captured.out
    assert rows[0]["status"] == "issues_found"
    assert int(json.loads(rows[0]["message"])["issues"]) >= 1
    assert len(rows) >= 2


def test_contract_check_accepts_explicit_format_and_delimiter(
    tmp_path: Path,
    capsys,
) -> None:
    baseline = write_text(tmp_path, "baseline.csv", "id,amount\n001,10.5\n002,12\n")
    contract = write_contract_from(tmp_path, capsys, baseline)
    candidate = write_text(tmp_path, "candidate.data", "id;amount\n003;9.25\n")

    status = main(
        [
            "contract",
            "check",
            str(candidate),
            str(contract),
            "--format",
            "csv",
            "--delimiter",
            ";",
            "--report-format",
            "html",
        ]
    )

    captured = capsys.readouterr()
    assert status == 0
    assert captured.err == ""
    assert captured.out.startswith("<!doctype html>")
    assert "ReconForge Dataset Contract Report" in captured.out


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{", "invalid contract JSON"),
        ("{}", "missing required field"),
        (
            '{"contract_version":1,"allow_extra_fields":false,"fields":{}}',
            "must contain at least one field",
        ),
        (
            '{"contract_version":1,"contract_version":1,"allow_extra_fields":false,"fields":{}}',
            "duplicate object key",
        ),
    ],
)
def test_contract_check_returns_two_for_bad_contracts(
    tmp_path: Path,
    capsys,
    content: str,
    message: str,
) -> None:
    dataset = write_text(tmp_path, "input.csv", "id,value\nA,10\n")
    contract = write_text(tmp_path, "bad-contract.json", content)

    status = main(["contract", "check", str(dataset), str(contract)])

    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert message in captured.err
    assert "Traceback" not in captured.err


def test_contract_check_reports_missing_contract_and_output_failure(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = write_text(tmp_path, "input.csv", "id,value\nA,10\n")
    status = main(["contract", "check", str(dataset), str(tmp_path / "missing-contract.json")])
    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert "does not exist" in captured.err

    contract = write_contract_from(tmp_path, capsys, dataset)

    def fail_write(*args, **kwargs):
        raise OSError("simulated contract disk failure")

    monkeypatch.setattr(cli_module, "write_contract_report", fail_write)
    status = main(
        [
            "contract",
            "check",
            str(dataset),
            str(contract),
            "--output",
            str(tmp_path / "result.json"),
        ]
    )
    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert captured.err == "reconforge: error: simulated contract disk failure\n"
