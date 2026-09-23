from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

import reconforge.atomic_io as atomic_io_module
from reconforge.batch import BatchPair, BatchPlan
from reconforge.batch_checkpoint import (
    BATCH_CHECKPOINT_VERSION,
    pair_fingerprint,
    reconcile_batch_resumable,
)
from reconforge.cli import main
from reconforge.errors import BatchCheckpointError
from reconforge.normalization import FieldNormalizer
from reconforge.policies import ComparisonPolicy
from reconforge.readers import DatasetReadOptions, read_dataset
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.report import result_from_dict, result_to_dict
from reconforge.validation import RequiredRule


@pytest.mark.parametrize(
    "changed_settings",
    [
        {"key_fields": ("value",)},
        {"compare_fields": ()},
        {"ignore_fields": ("memo",)},
        {"case_sensitive_keys": False},
        {"field_normalizers": {"value": FieldNormalizer.text()}},
        {"comparison_policies": {"value": ComparisonPolicy.text()}},
        {"validation_rules": {"value": (RequiredRule(),)}},
    ],
)
@pytest.mark.parametrize("cached", [False, True])
def test_checkpoint_rejects_results_from_other_comparison_settings(
    tmp_path: Path, changed_settings, cached: bool
) -> None:
    pair = make_plan(tmp_path).pairs[0]
    plan = BatchPlan((pair,))
    config = ReconciliationConfig(key_fields=("id",), compare_fields=("value",))
    # Changing the key also requires selecting a non-key comparison column.
    if "key_fields" in changed_settings:
        changed_settings = {**changed_settings, "compare_fields": ("id",)}
    other = replace(config, **changed_settings)
    foreign_result = reconcile(read_dataset(pair.left), read_dataset(pair.right), other)
    checkpoint = tmp_path / "checkpoint.json"
    if cached:
        reconcile_batch_resumable(plan, config, checkpoint)
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        payload["pairs"][pair.name]["result"] = result_to_dict(foreign_result)
        write_checkpoint_document(checkpoint, payload)
    before = checkpoint.read_bytes() if checkpoint.exists() else None

    with pytest.raises(BatchCheckpointError, match="result configuration"):
        reconcile_batch_resumable(plan, config, checkpoint, pair_runner=lambda *_: foreign_result)

    assert (checkpoint.read_bytes() if checkpoint.exists() else None) == before


def test_explicit_key_and_ignored_comparison_fields_resume_safely(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    config = ReconciliationConfig(
        key_fields=("id",), compare_fields=("id", "value"), ignore_fields=("value",)
    )
    checkpoint = tmp_path / "checkpoint.json"
    first = reconcile_batch_resumable(plan, config, checkpoint)
    second = reconcile_batch_resumable(plan, config, checkpoint)
    assert second.result == first.result
    assert second.reused_pairs == ("alpha", "beta")


def write_file(root: Path, name: str, content: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def make_plan(tmp_path: Path) -> BatchPlan:
    return BatchPlan(
        (
            BatchPair(
                "alpha",
                write_file(tmp_path, "alpha-left.csv", "id,value\n1,A\n"),
                write_file(tmp_path, "alpha-right.csv", "id,value\n1,A\n"),
            ),
            BatchPair(
                "beta",
                write_file(tmp_path, "beta-left.csv", "id,value\n2,B\n"),
                write_file(tmp_path, "beta-right.csv", "id,value\n2,C\n"),
            ),
        )
    )


def counting_runner(calls: list[str]):
    def run(pair, config, left_options, right_options):
        calls.append(pair.name)
        return reconcile(
            read_dataset(pair.left, left_options),
            read_dataset(pair.right, right_options),
            config,
        )

    return run


def populated_checkpoint(
    tmp_path: Path,
) -> tuple[BatchPlan, ReconciliationConfig, Path, dict[str, object]]:
    plan = make_plan(tmp_path)
    config = ReconciliationConfig(key_fields=("id",))
    checkpoint = tmp_path / "batch.checkpoint.json"
    reconcile_batch_resumable(plan, config, checkpoint)
    document = json.loads(checkpoint.read_text(encoding="utf-8"))
    return plan, config, checkpoint, document


def write_checkpoint_document(path: Path, document: object) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def test_canonical_report_round_trips_a_reconciliation_result(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    pair = plan.pairs[1]
    original = reconcile(
        read_dataset(pair.left),
        read_dataset(pair.right),
        ReconciliationConfig(key_fields=("id",)),
    )

    restored = result_from_dict(result_to_dict(original))

    assert restored == original


def test_unchanged_pairs_are_reused_without_calling_the_runner(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    config = ReconciliationConfig(key_fields=("id",))
    checkpoint = tmp_path / "state" / "batch.checkpoint.json"
    first_calls: list[str] = []
    first = reconcile_batch_resumable(
        plan,
        config,
        checkpoint,
        pair_runner=counting_runner(first_calls),
    )
    second_calls: list[str] = []
    second = reconcile_batch_resumable(
        plan,
        config,
        checkpoint,
        pair_runner=counting_runner(second_calls),
    )

    assert first_calls == ["alpha", "beta"]
    assert first.executed_pairs == ("alpha", "beta")
    assert first.reused_pairs == ()
    assert second_calls == []
    assert second.executed_pairs == ()
    assert second.reused_pairs == ("alpha", "beta")
    assert second.result == first.result
    document = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert document["version"] == BATCH_CHECKPOINT_VERSION
    assert list(document["pairs"]) == ["alpha", "beta"]
    assert not list(checkpoint.parent.glob(f".{checkpoint.name}.*.tmp"))


def test_checkpoint_records_the_complete_reuse_context(tmp_path: Path) -> None:
    plan, config, checkpoint, document = populated_checkpoint(tmp_path)

    alpha = document["pairs"]["alpha"]

    assert set(alpha) == {
        "fingerprint",
        "sources",
        "configuration",
        "read_options",
        "result",
    }
    assert alpha["sources"] == {
        "left": {
            "path": str(plan.pairs[0].left.resolve()),
            "suffix": ".csv",
        },
        "right": {
            "path": str(plan.pairs[0].right.resolve()),
            "suffix": ".csv",
        },
    }
    assert alpha["configuration"]["key_fields"] == list(config.key_fields)
    assert alpha["read_options"]["left"]["encoding"] == "utf-8-sig"
    assert checkpoint.read_bytes().endswith(b"\n")


def test_changed_input_invalidates_only_its_pair(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    config = ReconciliationConfig(key_fields=("id",))
    checkpoint = tmp_path / "batch.checkpoint.json"
    reconcile_batch_resumable(plan, config, checkpoint)
    plan.pairs[1].right.write_text("id,value\n2,B\n", encoding="utf-8")
    calls: list[str] = []

    resumed = reconcile_batch_resumable(
        plan,
        config,
        checkpoint,
        pair_runner=counting_runner(calls),
    )

    assert calls == ["beta"]
    assert resumed.reused_pairs == ("alpha",)
    assert resumed.executed_pairs == ("beta",)
    assert resumed.result.is_clean


def test_effective_configuration_and_read_options_invalidate_results(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    checkpoint = tmp_path / "batch.checkpoint.json"
    original = ReconciliationConfig(key_fields=("id",))
    changed = ReconciliationConfig(key_fields=("id",), compare_fields=())
    base_fingerprint = pair_fingerprint(plan.pairs[0], original)

    assert pair_fingerprint(plan.pairs[0], changed) != base_fingerprint
    assert (
        pair_fingerprint(
            plan.pairs[0],
            original,
            left_options=DatasetReadOptions(delimiter=";"),
        )
        != base_fingerprint
    )
    reconcile_batch_resumable(plan, original, checkpoint)
    calls: list[str] = []

    resumed = reconcile_batch_resumable(
        plan,
        changed,
        checkpoint,
        pair_runner=counting_runner(calls),
    )

    assert calls == ["alpha", "beta"]
    assert resumed.reused_pairs == ()


def test_fingerprint_frames_left_and_right_file_content(tmp_path: Path) -> None:
    left = write_file(tmp_path, "left.csv", "a")
    right = write_file(tmp_path, "right.csv", "bc")
    pair = BatchPair("framing", left, right)
    config = ReconciliationConfig(key_fields=("id",))

    first = pair_fingerprint(pair, config)
    left.write_text("ab", encoding="utf-8")
    right.write_text("c", encoding="utf-8")
    second = pair_fingerprint(pair, config)

    assert first != second
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64


def test_interrupted_batch_keeps_completed_pairs_and_resumes_remaining_work(
    tmp_path: Path,
) -> None:
    plan = make_plan(tmp_path)
    config = ReconciliationConfig(key_fields=("id",))
    checkpoint = tmp_path / "batch.checkpoint.json"
    attempted: list[str] = []

    def interrupted(pair, current_config, left_options, right_options):
        attempted.append(pair.name)
        if pair.name == "beta":
            raise RuntimeError("simulated interruption")
        return counting_runner([])(pair, current_config, left_options, right_options)

    with pytest.raises(RuntimeError, match="simulated interruption"):
        reconcile_batch_resumable(
            plan,
            config,
            checkpoint,
            pair_runner=interrupted,
        )

    document = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert attempted == ["alpha", "beta"]
    assert list(document["pairs"]) == ["alpha"]
    recovery_calls: list[str] = []

    recovered = reconcile_batch_resumable(
        plan,
        config,
        checkpoint,
        pair_runner=counting_runner(recovery_calls),
    )

    assert recovery_calls == ["beta"]
    assert recovered.reused_pairs == ("alpha",)
    assert recovered.executed_pairs == ("beta",)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not-json", "invalid checkpoint JSON"),
        ('{"version": 1, "pairs": {}}', "version 1 checkpoints cannot be resumed safely"),
        ('{"version": 3, "pairs": {}}', "unsupported batch checkpoint version 3"),
        ('{"version": true, "pairs": {}}', "unsupported batch checkpoint version True"),
        ('{"version": 2}', "missing required key.*'pairs'"),
        ('{"version": 2, "pairs": {}, "extra": 1}', "unknown key.*'extra'"),
        ('{"version": NaN, "pairs": {}}', "non-standard numeric value 'NaN'"),
    ],
)
def test_invalid_checkpoints_fail_safely(tmp_path: Path, content: str, message: str) -> None:
    checkpoint = tmp_path / "batch.checkpoint.json"
    checkpoint.write_text(content, encoding="utf-8")

    with pytest.raises(BatchCheckpointError, match=message):
        reconcile_batch_resumable(
            make_plan(tmp_path),
            ReconciliationConfig(key_fields=("id",)),
            checkpoint,
        )


@pytest.mark.parametrize(
    "content",
    [
        '{"version": 2, "version": 2, "pairs": {}}',
        ('{"version": 2, "pairs": {"alpha": {"fingerprint": "one", "fingerprint": "two"}}}'),
        ('{"version": 2, "pairs": {"alpha": {"result": {"version": 1, "version": 1}}}}'),
    ],
)
def test_duplicate_json_keys_are_rejected_at_every_depth(tmp_path: Path, content: str) -> None:
    checkpoint = tmp_path / "batch.checkpoint.json"
    checkpoint.write_text(content, encoding="utf-8")

    with pytest.raises(BatchCheckpointError, match="duplicate key"):
        reconcile_batch_resumable(
            make_plan(tmp_path),
            ReconciliationConfig(key_fields=("id",)),
            checkpoint,
        )


def test_excessively_nested_checkpoint_json_is_reported_safely(tmp_path: Path) -> None:
    checkpoint = tmp_path / "batch.checkpoint.json"
    nested = "[" * 2_000 + "0" + "]" * 2_000
    checkpoint.write_text(f'{{"version": 2, "pairs": {nested}}}', encoding="utf-8")

    with pytest.raises(BatchCheckpointError, match="nesting is too deep"):
        reconcile_batch_resumable(
            make_plan(tmp_path),
            ReconciliationConfig(key_fields=("id",)),
            checkpoint,
        )


@pytest.mark.parametrize(
    "fingerprint",
    [
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "sha256:" + "A" * 64,
        "SHA256:" + "a" * 64,
        "sha256:" + "g" * 64,
    ],
)
def test_checkpoint_fingerprint_must_be_exact_lowercase_sha256(
    tmp_path: Path, fingerprint: str
) -> None:
    plan, config, checkpoint, document = populated_checkpoint(tmp_path)
    document["pairs"]["alpha"]["fingerprint"] = fingerprint
    write_checkpoint_document(checkpoint, document)

    with pytest.raises(BatchCheckpointError, match="invalid content fingerprint"):
        reconcile_batch_resumable(plan, config, checkpoint)


@pytest.mark.parametrize(
    ("section", "key", "message"),
    [
        ("entry", "result", "missing required key.*'result'"),
        ("entry", "unexpected", "unknown key.*'unexpected'"),
        ("sources", "right", "missing required key.*'right'"),
        ("sources", "unexpected", "unknown key.*'unexpected'"),
        ("configuration", "key_fields", "missing required key.*'key_fields'"),
        ("configuration", "unexpected", "unknown key.*'unexpected'"),
        ("read_options", "right", "missing required key.*'right'"),
        ("read_options", "unexpected", "unknown key.*'unexpected'"),
        ("left_options", "encoding", "missing required key.*'encoding'"),
        ("left_options", "unexpected", "unknown key.*'unexpected'"),
    ],
)
def test_checkpoint_objects_reject_missing_and_unknown_fields(
    tmp_path: Path, section: str, key: str, message: str
) -> None:
    plan, config, checkpoint, original = populated_checkpoint(tmp_path)
    document = copy.deepcopy(original)
    entry = document["pairs"]["alpha"]
    target = {
        "entry": entry,
        "sources": entry["sources"],
        "configuration": entry["configuration"],
        "read_options": entry["read_options"],
        "left_options": entry["read_options"]["left"],
    }[section]
    if key == "unexpected":
        target[key] = "value"
    else:
        target.pop(key)
    write_checkpoint_document(checkpoint, document)

    with pytest.raises(BatchCheckpointError, match=message):
        reconcile_batch_resumable(plan, config, checkpoint)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (("sources", "left", "path", "relative.csv"), "absolute non-empty path"),
        (("sources", "left", "suffix", ".CSV"), "lowercase file suffix"),
        (("configuration", "key_fields", None, []), "key_fields must not be empty"),
        (
            ("configuration", "case_sensitive_keys", None, 1),
            "case_sensitive_keys must be a boolean",
        ),
        (
            ("configuration", "validation_rules", "id", "required"),
            r"validation_rules\['id'\] must be an array",
        ),
        (("read_options", "left", "format", "yaml"), "format must be null or one of"),
        (("read_options", "left", "trim_values", 1), "trim_values must be a boolean"),
    ],
)
def test_checkpoint_context_values_are_strictly_validated(
    tmp_path: Path, mutation: tuple[str, str, str | None, object], message: str
) -> None:
    plan, config, checkpoint, document = populated_checkpoint(tmp_path)
    first, second, third, replacement = mutation
    target = document["pairs"]["alpha"][first]
    if third is None:
        target[second] = replacement
    else:
        target[second][third] = replacement
    write_checkpoint_document(checkpoint, document)

    with pytest.raises(BatchCheckpointError, match=message):
        reconcile_batch_resumable(plan, config, checkpoint)


def test_invalid_stored_result_is_rejected_even_when_pair_is_not_reused(tmp_path: Path) -> None:
    plan, config, checkpoint, document = populated_checkpoint(tmp_path)
    document["pairs"]["alpha"]["fingerprint"] = "sha256:" + "0" * 64
    document["pairs"]["alpha"]["result"] = {}
    write_checkpoint_document(checkpoint, document)

    with pytest.raises(BatchCheckpointError, match="contains an invalid result"):
        reconcile_batch_resumable(plan, config, checkpoint)


@pytest.mark.parametrize("context", ["sources", "configuration", "read_options"])
def test_tampered_reuse_context_forces_pair_reexecution(tmp_path: Path, context: str) -> None:
    plan, config, checkpoint, document = populated_checkpoint(tmp_path)
    entry = document["pairs"]["alpha"]
    if context == "sources":
        entry["sources"]["left"]["path"] = str(plan.pairs[1].left.resolve())
    elif context == "configuration":
        entry["configuration"]["key_fields"] = ["alternate_id"]
    else:
        entry["read_options"]["left"]["encoding"] = "utf-8"
    write_checkpoint_document(checkpoint, document)
    calls: list[str] = []

    resumed = reconcile_batch_resumable(
        plan,
        config,
        checkpoint,
        pair_runner=counting_runner(calls),
    )

    assert calls == ["alpha"]
    assert resumed.executed_pairs == ("alpha",)
    assert resumed.reused_pairs == ("beta",)


def test_unique_temporary_file_does_not_clobber_a_stale_legacy_temp(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    checkpoint = tmp_path / "batch.checkpoint.json"
    legacy_temporary = checkpoint.with_name(f".{checkpoint.name}.tmp")
    legacy_temporary.write_text("do not overwrite", encoding="utf-8")

    reconcile_batch_resumable(
        plan,
        ReconciliationConfig(key_fields=("id",)),
        checkpoint,
    )

    assert legacy_temporary.read_text(encoding="utf-8") == "do not overwrite"
    assert not list(checkpoint.parent.glob(f".{checkpoint.name}.*.tmp"))


def test_failed_atomic_replace_preserves_checkpoint_and_cleans_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, config, checkpoint, _ = populated_checkpoint(tmp_path)
    original = checkpoint.read_bytes()
    plan.pairs[0].left.write_text("id,value\n1,changed\n", encoding="utf-8")

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(atomic_io_module.os, "replace", fail_replace)

    with pytest.raises(BatchCheckpointError, match="simulated replace failure"):
        reconcile_batch_resumable(plan, config, checkpoint)

    assert checkpoint.read_bytes() == original
    assert not list(checkpoint.parent.glob(f".{checkpoint.name}.*.tmp"))


def test_cli_checkpoint_reports_executed_then_reused_pairs(tmp_path: Path, capsys) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    write_file(left, "orders.csv", "id,value\n1,A\n")
    write_file(right, "orders.csv", "id,value\n1,A\n")
    checkpoint = tmp_path / "batch.checkpoint.json"
    command = [
        "batch",
        str(left),
        str(right),
        "--key",
        "id",
        "--checkpoint",
        str(checkpoint),
    ]

    assert main(command) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["execution"]["executed_pairs"] == ["orders"]
    assert first["execution"]["reused_pairs"] == []

    assert main(command) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["execution"]["executed_pairs"] == []
    assert second["execution"]["reused_pairs"] == ["orders"]
