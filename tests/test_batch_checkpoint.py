from __future__ import annotations

import json
from pathlib import Path

import pytest

from reconforge.batch import BatchPair, BatchPlan
from reconforge.batch_checkpoint import (
    BATCH_CHECKPOINT_VERSION,
    pair_fingerprint,
    reconcile_batch_resumable,
)
from reconforge.cli import main
from reconforge.errors import BatchCheckpointError
from reconforge.readers import DatasetReadOptions, read_dataset
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.report import result_from_dict, result_to_dict


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
    assert not checkpoint.with_name(f".{checkpoint.name}.tmp").exists()


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
        ('{"version": 2, "pairs": {}}', "unsupported batch checkpoint version 2"),
        (
            '{"version": 1, "pairs": {"a": {"fingerprint": "bad", "result": {}}}}',
            "invalid content fingerprint",
        ),
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
