"""Content-addressed durable checkpoints for resumable batch execution."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path

from reconforge.batch import BatchPair, BatchPairResult, BatchPlan, BatchResult
from reconforge.errors import BatchCheckpointError
from reconforge.model import ReconciliationResult
from reconforge.readers import DatasetReadOptions, read_dataset
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.report import result_from_dict, result_to_dict

BATCH_CHECKPOINT_VERSION = 1

PairRunner = Callable[
    [BatchPair, ReconciliationConfig, DatasetReadOptions, DatasetReadOptions],
    ReconciliationResult,
]


@dataclass(frozen=True, slots=True)
class ResumableBatchResult:
    """A batch result plus evidence about executed and reused pairs."""

    result: BatchResult
    checkpoint: Path
    reused_pairs: tuple[str, ...]
    executed_pairs: tuple[str, ...]


def reconcile_batch_resumable(
    plan: BatchPlan,
    config: ReconciliationConfig,
    checkpoint: str | Path,
    *,
    left_options: DatasetReadOptions | None = None,
    right_options: DatasetReadOptions | None = None,
    pair_runner: PairRunner | None = None,
) -> ResumableBatchResult:
    """Reconcile a batch, atomically checkpointing each stable completed pair."""

    checkpoint_path = Path(checkpoint)
    selected_left = left_options or DatasetReadOptions()
    selected_right = right_options or DatasetReadOptions()
    runner = pair_runner or _reconcile_pair
    stored = _load_checkpoint(checkpoint_path)
    entries = {
        name: entry for name, entry in stored.items() if name in {pair.name for pair in plan.pairs}
    }
    results: list[BatchPairResult] = []
    reused: list[str] = []
    executed: list[str] = []

    for pair in plan.pairs:
        fingerprint = pair_fingerprint(
            pair,
            config,
            left_options=selected_left,
            right_options=selected_right,
        )
        entry = entries.get(pair.name)
        if entry is not None and entry["fingerprint"] == fingerprint:
            try:
                result = result_from_dict(_mapping(entry["result"], f"pair {pair.name!r} result"))
            except (TypeError, ValueError) as exc:
                raise BatchCheckpointError(
                    f"checkpoint {checkpoint_path} contains an invalid result "
                    f"for pair {pair.name!r}: {exc}"
                ) from exc
            reused.append(pair.name)
        else:
            result = runner(pair, config, selected_left, selected_right)
            stable_fingerprint = pair_fingerprint(
                pair,
                config,
                left_options=selected_left,
                right_options=selected_right,
            )
            if stable_fingerprint != fingerprint:
                raise BatchCheckpointError(
                    f"batch inputs changed while pair {pair.name!r} was being reconciled; "
                    "no checkpoint was written for that pair"
                )
            entries[pair.name] = {
                "fingerprint": fingerprint,
                "left": str(pair.left.resolve()),
                "right": str(pair.right.resolve()),
                "result": result_to_dict(result),
            }
            _write_checkpoint(checkpoint_path, entries)
            executed.append(pair.name)
        results.append(BatchPairResult(pair, result))

    _write_checkpoint(checkpoint_path, entries)
    return ResumableBatchResult(
        result=BatchResult(tuple(results), plan.unmatched_left, plan.unmatched_right),
        checkpoint=checkpoint_path.resolve(),
        reused_pairs=tuple(reused),
        executed_pairs=tuple(executed),
    )


def pair_fingerprint(
    pair: BatchPair,
    config: ReconciliationConfig,
    *,
    left_options: DatasetReadOptions | None = None,
    right_options: DatasetReadOptions | None = None,
) -> str:
    """Hash input identities, contents, and every effective comparison setting."""

    selected_left = left_options or DatasetReadOptions()
    selected_right = right_options or DatasetReadOptions()
    payload = {
        "fingerprint_version": 1,
        "pair": {
            "name": pair.name,
            "left": str(pair.left.resolve()),
            "left_suffix": pair.left.suffix.lower(),
            "right": str(pair.right.resolve()),
            "right_suffix": pair.right.suffix.lower(),
        },
        "configuration": _configuration_model(config),
        "read_options": {
            "left": _read_options_model(selected_left),
            "right": _read_options_model(selected_right),
        },
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )
    _update_file_digest(digest, pair.left, "left")
    _update_file_digest(digest, pair.right, "right")
    return f"sha256:{digest.hexdigest()}"


def _reconcile_pair(
    pair: BatchPair,
    config: ReconciliationConfig,
    left_options: DatasetReadOptions,
    right_options: DatasetReadOptions,
) -> ReconciliationResult:
    return reconcile(
        read_dataset(pair.left, left_options),
        read_dataset(pair.right, right_options),
        config,
    )


def _configuration_model(config: ReconciliationConfig) -> dict[str, object]:
    return {
        "key_fields": list(config.key_fields),
        "compare_fields": None if config.compare_fields is None else list(config.compare_fields),
        "ignore_fields": list(config.ignore_fields),
        "case_sensitive_keys": config.case_sensitive_keys,
        "field_normalizers": {
            name: normalizer.kind.value
            for name, normalizer in sorted(config.field_normalizers.items())
        },
        "comparison_policies": {
            name: policy.descriptor for name, policy in sorted(config.comparison_policies.items())
        },
        "validation_rules": {
            name: [rule.descriptor for rule in rules]
            for name, rules in sorted(config.validation_rules.items())
        },
    }


def _read_options_model(options: DatasetReadOptions) -> dict[str, object]:
    return {
        "format": None if options.format is None else options.format.value,
        "encoding": options.encoding,
        "delimiter": options.delimiter,
        "trim_headers": options.trim_headers,
        "trim_values": options.trim_values,
        "empty_as_none": options.empty_as_none,
    }


def _update_file_digest(digest: object, path: Path, side: str) -> None:
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)  # type: ignore[attr-defined]
    except FileNotFoundError as exc:
        raise BatchCheckpointError(f"{side} batch input does not exist: {path}") from exc
    except OSError as exc:
        raise BatchCheckpointError(f"cannot fingerprint {side} batch input {path}: {exc}") from exc


def _load_checkpoint(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeError as exc:
        raise BatchCheckpointError(f"batch checkpoint is not valid UTF-8: {path}") from exc
    except JSONDecodeError as exc:
        raise BatchCheckpointError(
            f"{path}: invalid checkpoint JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except OSError as exc:
        raise BatchCheckpointError(f"cannot read batch checkpoint {path}: {exc}") from exc
    document = _mapping(payload, "checkpoint")
    if document.get("version") != BATCH_CHECKPOINT_VERSION:
        raise BatchCheckpointError(
            f"unsupported batch checkpoint version {document.get('version')!r} in {path}; "
            f"expected {BATCH_CHECKPOINT_VERSION}"
        )
    raw_entries = _mapping(document.get("pairs"), "checkpoint.pairs")
    entries: dict[str, dict[str, object]] = {}
    for name, raw_entry in raw_entries.items():
        entry = _mapping(raw_entry, f"checkpoint.pairs[{name!r}]")
        fingerprint = entry.get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint.startswith("sha256:"):
            raise BatchCheckpointError(
                f"checkpoint pair {name!r} has an invalid content fingerprint"
            )
        if not isinstance(entry.get("result"), Mapping):
            raise BatchCheckpointError(f"checkpoint pair {name!r} has no valid result object")
        entries[name] = entry
    return entries


def _write_checkpoint(path: Path, entries: Mapping[str, Mapping[str, object]]) -> None:
    document = {
        "version": BATCH_CHECKPOINT_VERSION,
        "pairs": {
            name: entries[name]
            for name in sorted(entries, key=lambda item: (item.casefold(), item))
        },
    }
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="\n") as destination:
            json.dump(document, destination, indent=2, ensure_ascii=False)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(path)
    except OSError as exc:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise BatchCheckpointError(f"cannot write batch checkpoint {path}: {exc}") from exc


def _mapping(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise BatchCheckpointError(f"{location} must be an object with string keys")
    return dict(value)
