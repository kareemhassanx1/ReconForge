from __future__ import annotations

import json
from pathlib import Path

import pytest

from reconforge.normalization import NormalizerKind
from reconforge.policies import ComparisonPolicyKind
from reconforge.project_config import (
    PROJECT_CONFIG_VERSION,
    ProjectConfigError,
    load_project_config,
)
from reconforge.readers import DatasetFormat
from reconforge.validation import (
    AllowedValuesRule,
    NumericRangeRule,
    RequiredRule,
    TextPatternRule,
)


def write_config(tmp_path: Path, payload: object, name: str = "reconforge.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_loads_a_complete_version_one_configuration(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {
            "version": 1,
            "inputs": {
                "format": "csv",
                "left_format": "csv",
                "right_format": "jsonl",
                "delimiter": ";",
            },
            "reconciliation": {
                "key_fields": [" invoice_id ", "period"],
                "compare_fields": ["amount", "settled_on", "customer"],
                "ignore_fields": ["exported_at"],
                "case_sensitive_keys": False,
                "field_normalizers": {
                    "invoice_id": "integer",
                    "settled_on": "date",
                },
                "comparison_policies": {
                    "amount": "decimal:0.01",
                    "customer": "text",
                },
                "validation_rules": {
                    "status": [
                        {"type": "required"},
                        {"type": "allowed", "values": ["paid", "pending"]},
                    ],
                    "amount": [
                        {
                            "type": "numeric_range",
                            "minimum": "0",
                            "maximum": "10000",
                            "maximum_inclusive": False,
                        }
                    ],
                    "reference": [{"type": "pattern", "pattern": "INV-[0-9]{3}"}],
                },
            },
        },
    )

    config = load_project_config(path)

    assert config.version == PROJECT_CONFIG_VERSION
    assert config.format is DatasetFormat.CSV
    assert config.left_format is DatasetFormat.CSV
    assert config.right_format is DatasetFormat.NDJSON
    assert config.delimiter == ";"
    assert config.key_fields == ("invoice_id", "period")
    assert config.compare_fields == ("amount", "settled_on", "customer")
    assert config.ignore_fields == ("exported_at",)
    assert not config.case_sensitive_keys
    assert config.field_normalizers["invoice_id"].kind is NormalizerKind.INTEGER
    assert config.comparison_policies["amount"].kind is ComparisonPolicyKind.DECIMAL
    assert config.comparison_policies["amount"].descriptor == "decimal:tolerance=0.01"
    assert isinstance(config.validation_rules["status"][0], RequiredRule)
    assert isinstance(config.validation_rules["status"][1], AllowedValuesRule)
    assert isinstance(config.validation_rules["amount"][0], NumericRangeRule)
    assert config.validation_rules["amount"][0].descriptor == "numeric_range:[0,10000)"
    assert isinstance(config.validation_rules["reference"][0], TextPatternRule)


def test_minimal_configuration_uses_safe_defaults(tmp_path: Path) -> None:
    config = load_project_config(write_config(tmp_path, {"version": 1}))

    assert config.key_fields is None
    assert config.compare_fields is None
    assert config.ignore_fields == ()
    assert config.case_sensitive_keys
    assert dict(config.field_normalizers) == {}
    assert dict(config.comparison_policies) == {}
    assert dict(config.validation_rules) == {}


def test_loaded_policy_mappings_are_immutable(tmp_path: Path) -> None:
    config = load_project_config(
        write_config(
            tmp_path,
            {
                "version": 1,
                "reconciliation": {"field_normalizers": {"amount": "decimal"}},
            },
        )
    )

    with pytest.raises(TypeError):
        config.field_normalizers["amount"] = config.field_normalizers["amount"]  # type: ignore[index]


@pytest.mark.parametrize("version", [2, 0, -1])
def test_rejects_unsupported_versions(tmp_path: Path, version: int) -> None:
    path = write_config(tmp_path, {"version": version})

    with pytest.raises(ProjectConfigError, match=f"unsupported.*version {version}"):
        load_project_config(path)


@pytest.mark.parametrize("version", [True, "1", 1.0, None])
def test_version_must_be_an_integer(tmp_path: Path, version: object) -> None:
    path = write_config(tmp_path, {"version": version})

    with pytest.raises(ProjectConfigError, match="'version' must be an integer"):
        load_project_config(path)


def test_version_is_required(tmp_path: Path) -> None:
    with pytest.raises(ProjectConfigError, match="missing required.*'version'"):
        load_project_config(write_config(tmp_path, {}))


def test_top_level_value_must_be_an_object(tmp_path: Path) -> None:
    with pytest.raises(ProjectConfigError, match="top-level JSON value must be an object"):
        load_project_config(write_config(tmp_path, ["not", "an", "object"]))


def test_malformed_json_reports_line_and_column(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text('{\n  "version": 1,\n  "inputs":', encoding="utf-8")

    with pytest.raises(ProjectConfigError, match=r"line 3, column 12"):
        load_project_config(path)


def test_duplicate_object_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"version": 1, "version": 1}', encoding="utf-8")

    with pytest.raises(ProjectConfigError, match="duplicate key 'version'"):
        load_project_config(path)


@pytest.mark.parametrize(
    ("payload", "location", "unknown"),
    [
        ({"version": 1, "output": {}}, "top level", "output"),
        ({"version": 1, "inputs": {"encoding": "utf-8"}}, "inputs", "encoding"),
        (
            {"version": 1, "reconciliation": {"keys": ["id"]}},
            "reconciliation",
            "keys",
        ),
    ],
)
def test_unknown_keys_are_rejected_at_every_level(
    tmp_path: Path,
    payload: object,
    location: str,
    unknown: str,
) -> None:
    path = write_config(tmp_path, payload)

    with pytest.raises(ProjectConfigError, match=rf"unknown key.*{location}.*'{unknown}'"):
        load_project_config(path)


@pytest.mark.parametrize("section", ["inputs", "reconciliation"])
def test_sections_must_be_objects(tmp_path: Path, section: str) -> None:
    path = write_config(tmp_path, {"version": 1, section: []})

    with pytest.raises(ProjectConfigError, match=rf"'{section}' must be an object"):
        load_project_config(path)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("key_fields", "id", "must be an array"),
        ("key_fields", [], "at least one field"),
        ("key_fields", ["id", " id "], "duplicate fields"),
        ("compare_fields", [""], "non-empty string"),
        ("ignore_fields", [1], "non-empty string"),
    ],
)
def test_field_lists_are_strictly_validated(
    tmp_path: Path,
    key: str,
    value: object,
    message: str,
) -> None:
    path = write_config(tmp_path, {"version": 1, "reconciliation": {key: value}})

    with pytest.raises(ProjectConfigError, match=message):
        load_project_config(path)


def test_compare_and_ignore_lists_may_be_explicitly_empty(tmp_path: Path) -> None:
    config = load_project_config(
        write_config(
            tmp_path,
            {
                "version": 1,
                "reconciliation": {"compare_fields": [], "ignore_fields": []},
            },
        )
    )

    assert config.compare_fields == ()
    assert config.ignore_fields == ()


def test_case_sensitive_keys_must_be_boolean(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {"version": 1, "reconciliation": {"case_sensitive_keys": "false"}},
    )

    with pytest.raises(ProjectConfigError, match="case_sensitive_keys must be a boolean"):
        load_project_config(path)


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({"format": "xml"}, "unknown dataset format"),
        ({"left_format": 42}, "left_format must be a string"),
        ({"delimiter": "::"}, "delimiter must be exactly one character"),
    ],
)
def test_input_settings_are_validated(
    tmp_path: Path,
    inputs: dict[str, object],
    message: str,
) -> None:
    path = write_config(tmp_path, {"version": 1, "inputs": inputs})

    with pytest.raises(ProjectConfigError, match=message):
        load_project_config(path)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("field_normalizers", [], "must be an object"),
        ("field_normalizers", {"amount": "money"}, "unknown normalizer type"),
        ("comparison_policies", {"amount": "decimal:-1"}, "finite non-negative"),
        ("comparison_policies", {"amount": 1}, "must be a non-empty string"),
    ],
)
def test_field_policy_maps_are_validated(
    tmp_path: Path,
    key: str,
    value: object,
    message: str,
) -> None:
    path = write_config(tmp_path, {"version": 1, "reconciliation": {key: value}})

    with pytest.raises(ProjectConfigError, match=message):
        load_project_config(path)


def test_missing_configuration_has_a_stable_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"

    with pytest.raises(ProjectConfigError, match="project configuration does not exist"):
        load_project_config(missing)


def test_configuration_must_be_utf8(tmp_path: Path) -> None:
    path = tmp_path / "invalid-encoding.json"
    path.write_bytes(b'{"version": 1, "note": "\xff"}')

    with pytest.raises(ProjectConfigError, match="project configuration is not valid UTF-8"):
        load_project_config(path)


@pytest.mark.parametrize(
    ("validation_rules", "message"),
    [
        ([], "validation_rules must be an object"),
        ({"status": []}, "must be a non-empty array"),
        ({"status": [{"type": "required"}, "required"]}, "must be an object"),
        ({"status": [{}]}, "type must be a non-empty string"),
        ({"status": [{"type": "unique"}]}, "unknown validation rule type"),
        (
            {"status": [{"type": "required", "message": "needed"}]},
            "unknown key.*message",
        ),
        ({"status": [{"type": "allowed"}]}, "values must be an array"),
        (
            {"status": [{"type": "allowed", "values": []}]},
            "requires at least one value",
        ),
        (
            {"status": [{"type": "allowed", "values": ["paid", "paid"]}]},
            "must be unique",
        ),
        (
            {"amount": [{"type": "numeric_range"}]},
            "requires a minimum or maximum",
        ),
        (
            {"amount": [{"type": "numeric_range", "minimum": "10", "maximum": "0"}]},
            "minimum cannot exceed maximum",
        ),
        (
            {
                "amount": [
                    {
                        "type": "numeric_range",
                        "minimum": "0",
                        "minimum_inclusive": "yes",
                    }
                ]
            },
            "inclusivity settings must be booleans",
        ),
        ({"reference": [{"type": "pattern"}]}, "pattern must be a string"),
        (
            {"reference": [{"type": "pattern", "pattern": "["}]},
            "invalid text pattern",
        ),
    ],
)
def test_validation_rule_configuration_is_strictly_validated(
    tmp_path: Path,
    validation_rules: object,
    message: str,
) -> None:
    path = write_config(
        tmp_path,
        {
            "version": 1,
            "reconciliation": {"validation_rules": validation_rules},
        },
    )

    with pytest.raises(ProjectConfigError, match=message):
        load_project_config(path)


def test_validation_rule_fields_are_trimmed_and_must_be_unique(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {
            "version": 1,
            "reconciliation": {
                "validation_rules": {
                    " status ": [{"type": "required"}],
                    "status": [{"type": "required"}],
                }
            },
        },
    )

    with pytest.raises(ProjectConfigError, match="contains duplicate field 'status'"):
        load_project_config(path)
