"""Content-addressed durable checkpoints for resumable batch execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path

from reconforge.batch import BatchPair, BatchPairResult, BatchPlan, BatchResult
from reconforge.errors import BatchCheckpointError
from reconforge.model import ReconciliationResult
from reconforge.readers import DatasetFormat, DatasetReadOptions, read_dataset
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.report import result_from_dict, result_to_dict

BATCH_CHECKPOINT_VERSION = 2
_FINGERPRINT_VERSION = 2
_CHECKPOINT_KEYS = frozenset({"version", "pairs"})
_ENTRY_KEYS = frozenset({"fingerprint", "sources", "configuration", "read_options", "result"})
_SOURCE_SIDES = frozenset({"left", "right"})
_SOURCE_KEYS = frozenset({"path", "suffix"})
_CONFIGURATION_KEYS = frozenset(
    {
        "key_fields",
        "compare_fields",
        "ignore_fields",
        "case_sensitive_keys",
        "field_normalizers",
        "comparison_policies",
        "validation_rules",
    }
)
_READ_OPTION_KEYS = frozenset(
    {"format", "encoding", "delimiter", "trim_headers", "trim_values", "empty_as_none"}
)
_SHA256_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_JSON_DEPTH = 100

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
        context = _checkpoint_context(
            pair,
            config,
            left_options=selected_left,
            right_options=selected_right,
        )
        fingerprint = pair_fingerprint(
            pair,
            config,
            left_options=selected_left,
            right_options=selected_right,
        )
        entry = entries.get(pair.name)
        if entry is not None and _entry_matches(entry, fingerprint, context):
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
                **context,
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
        "fingerprint_version": _FINGERPRINT_VERSION,
        "pair": {
            "name": pair.name,
            "sources": _source_identities(pair),
        },
        "configuration": _configuration_model(config),
        "read_options": {
            "left": _read_options_model(selected_left),
            "right": _read_options_model(selected_right),
        },
        "content": {
            "left": _file_content_identity(pair.left, "left"),
            "right": _file_content_identity(pair.right, "right"),
        },
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8"))
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


def _checkpoint_context(
    pair: BatchPair,
    config: ReconciliationConfig,
    *,
    left_options: DatasetReadOptions,
    right_options: DatasetReadOptions,
) -> dict[str, object]:
    return {
        "sources": _source_identities(pair),
        "configuration": _configuration_model(config),
        "read_options": {
            "left": _read_options_model(left_options),
            "right": _read_options_model(right_options),
        },
    }


def _source_identities(pair: BatchPair) -> dict[str, object]:
    return {
        "left": _source_identity(pair.left),
        "right": _source_identity(pair.right),
    }


def _source_identity(path: Path) -> dict[str, str]:
    return {
        "path": str(path.resolve()),
        "suffix": path.suffix.lower(),
    }


def _entry_matches(
    entry: Mapping[str, object],
    fingerprint: str,
    context: Mapping[str, object],
) -> bool:
    return entry["fingerprint"] == fingerprint and all(
        entry[key] == value for key, value in context.items()
    )


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _file_content_identity(path: Path, side: str) -> dict[str, object]:
    digest = hashlib.sha256()
    byte_length = 0
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                byte_length += len(chunk)
    except FileNotFoundError as exc:
        raise BatchCheckpointError(f"{side} batch input does not exist: {path}") from exc
    except OSError as exc:
        raise BatchCheckpointError(f"cannot fingerprint {side} batch input {path}: {exc}") from exc
    return {
        "byte_length": byte_length,
        "sha256": f"sha256:{digest.hexdigest()}",
    }


class _DuplicateObjectKey(ValueError):
    pass


class _InvalidJsonConstant(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateObjectKey(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise _InvalidJsonConstant(f"non-standard numeric value {value!r}")


def _load_checkpoint(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except UnicodeError as exc:
        raise BatchCheckpointError(f"batch checkpoint is not valid UTF-8: {path}") from exc
    except (_DuplicateObjectKey, _InvalidJsonConstant) as exc:
        raise BatchCheckpointError(f"{path}: invalid checkpoint JSON: {exc}") from exc
    except JSONDecodeError as exc:
        raise BatchCheckpointError(
            f"{path}: invalid checkpoint JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except RecursionError as exc:
        raise BatchCheckpointError(f"{path}: checkpoint JSON nesting is too deep") from exc
    except OSError as exc:
        raise BatchCheckpointError(f"cannot read batch checkpoint {path}: {exc}") from exc
    _validate_json_nesting(payload, path)
    document = _mapping(payload, "checkpoint")
    _require_exact_keys(document, _CHECKPOINT_KEYS, "checkpoint")
    version = document["version"]
    if type(version) is not int or version != BATCH_CHECKPOINT_VERSION:
        migration = (
            "; version 1 checkpoints cannot be resumed safely and must be recreated"
            if version == 1 and type(version) is int
            else ""
        )
        raise BatchCheckpointError(
            f"unsupported batch checkpoint version {version!r} in {path}; "
            f"expected {BATCH_CHECKPOINT_VERSION}{migration}"
        )
    raw_entries = _mapping(document["pairs"], "checkpoint.pairs")
    entries: dict[str, dict[str, object]] = {}
    seen_names: set[str] = set()
    for name, raw_entry in raw_entries.items():
        if not name or name != name.strip():
            raise BatchCheckpointError(
                "checkpoint pair names must be non-empty strings without surrounding whitespace"
            )
        folded_name = name.casefold()
        if folded_name in seen_names:
            raise BatchCheckpointError(f"checkpoint contains ambiguous pair name {name!r}")
        seen_names.add(folded_name)
        entries[name] = _validated_entry(raw_entry, name)
    return entries


def _write_checkpoint(path: Path, entries: Mapping[str, Mapping[str, object]]) -> None:
    document = {
        "version": BATCH_CHECKPOINT_VERSION,
        "pairs": {
            name: entries[name]
            for name in sorted(entries, key=lambda item: (item.casefold(), item))
        },
    }
    try:
        serialized = json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise BatchCheckpointError(f"cannot serialize batch checkpoint {path}: {exc}") from exc

    temporary: Path | None = None
    descriptor: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        destination = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        descriptor = None
        with destination:
            destination.write(serialized)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise BatchCheckpointError(f"cannot write batch checkpoint {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _validated_entry(value: object, name: str) -> dict[str, object]:
    location = f"checkpoint.pairs[{name!r}]"
    entry = _mapping(value, location)
    _require_exact_keys(entry, _ENTRY_KEYS, location)

    fingerprint = entry["fingerprint"]
    if not isinstance(fingerprint, str) or _SHA256_FINGERPRINT.fullmatch(fingerprint) is None:
        raise BatchCheckpointError(f"checkpoint pair {name!r} has an invalid content fingerprint")

    sources = _mapping(entry["sources"], f"{location}.sources")
    _require_exact_keys(sources, _SOURCE_SIDES, f"{location}.sources")
    for side in sorted(_SOURCE_SIDES):
        _validate_source_identity(sources[side], f"{location}.sources.{side}")

    _validate_configuration(entry["configuration"], f"{location}.configuration")
    _validate_read_options(entry["read_options"], f"{location}.read_options")

    result = _mapping(entry["result"], f"{location}.result")
    try:
        result_from_dict(result)
    except (TypeError, ValueError) as exc:
        raise BatchCheckpointError(
            f"checkpoint pair {name!r} contains an invalid result: {exc}"
        ) from exc
    return entry


def _validate_json_nesting(value: object, path: Path) -> None:
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > _MAX_JSON_DEPTH:
            raise BatchCheckpointError(f"{path}: checkpoint JSON nesting is too deep")
        if isinstance(item, Mapping):
            pending.extend((nested, depth + 1) for nested in item.values())
        elif isinstance(item, list):
            pending.extend((nested, depth + 1) for nested in item)


def _validate_source_identity(value: object, location: str) -> None:
    identity = _mapping(value, location)
    _require_exact_keys(identity, _SOURCE_KEYS, location)
    source_path = identity["path"]
    if not isinstance(source_path, str) or not source_path or not Path(source_path).is_absolute():
        raise BatchCheckpointError(f"{location}.path must be an absolute non-empty path")
    suffix = identity["suffix"]
    if (
        not isinstance(suffix, str)
        or suffix != suffix.lower()
        or (suffix and not suffix.startswith("."))
    ):
        raise BatchCheckpointError(f"{location}.suffix must be an empty or lowercase file suffix")


def _validate_configuration(value: object, location: str) -> None:
    configuration = _mapping(value, location)
    _require_exact_keys(configuration, _CONFIGURATION_KEYS, location)
    key_fields = _string_array(configuration["key_fields"], f"{location}.key_fields")
    if not key_fields:
        raise BatchCheckpointError(f"{location}.key_fields must not be empty")
    compare_fields_value = configuration["compare_fields"]
    compare_fields = (
        None
        if compare_fields_value is None
        else _string_array(compare_fields_value, f"{location}.compare_fields")
    )
    ignored_fields = _string_array(configuration["ignore_fields"], f"{location}.ignore_fields")
    if type(configuration["case_sensitive_keys"]) is not bool:
        raise BatchCheckpointError(f"{location}.case_sensitive_keys must be a boolean")

    normalizers = _string_mapping(
        configuration["field_normalizers"], f"{location}.field_normalizers"
    )
    policies = _string_mapping(
        configuration["comparison_policies"], f"{location}.comparison_policies"
    )
    rules = _mapping(configuration["validation_rules"], f"{location}.validation_rules")
    for field, descriptors in rules.items():
        _require_field_name(field, f"{location}.validation_rules")
        parsed = _string_array(descriptors, f"{location}.validation_rules[{field!r}]")
        if not parsed:
            raise BatchCheckpointError(f"{location}.validation_rules[{field!r}] must not be empty")

    overlap = sorted(set(key_fields) & set(ignored_fields))
    if overlap:
        raise BatchCheckpointError(f"{location}.key_fields and ignore_fields overlap: {overlap!r}")
    ignored_configuration = sorted(
        (set(normalizers) | set(policies) | set(rules)) & set(ignored_fields)
    )
    if ignored_configuration:
        raise BatchCheckpointError(
            f"{location} configures ignored fields: {ignored_configuration!r}"
        )
    if compare_fields is not None:
        policies_outside_comparison = sorted(set(policies) - set(compare_fields))
        if policies_outside_comparison:
            raise BatchCheckpointError(
                f"{location}.comparison_policies contains fields outside compare_fields: "
                f"{policies_outside_comparison!r}"
            )


def _validate_read_options(value: object, location: str) -> None:
    read_options = _mapping(value, location)
    _require_exact_keys(read_options, _SOURCE_SIDES, location)
    valid_formats = {item.value for item in DatasetFormat}
    for side in sorted(_SOURCE_SIDES):
        side_location = f"{location}.{side}"
        options = _mapping(read_options[side], side_location)
        _require_exact_keys(options, _READ_OPTION_KEYS, side_location)
        format_name = options["format"]
        if format_name is not None and format_name not in valid_formats:
            raise BatchCheckpointError(
                f"{side_location}.format must be null or one of {sorted(valid_formats)!r}"
            )
        encoding = options["encoding"]
        if not isinstance(encoding, str) or not encoding:
            raise BatchCheckpointError(f"{side_location}.encoding must be a non-empty string")
        delimiter = options["delimiter"]
        if delimiter is not None and (not isinstance(delimiter, str) or not delimiter):
            raise BatchCheckpointError(
                f"{side_location}.delimiter must be null or a non-empty string"
            )
        for key in ("trim_headers", "trim_values", "empty_as_none"):
            if type(options[key]) is not bool:
                raise BatchCheckpointError(f"{side_location}.{key} must be a boolean")


def _string_mapping(value: object, location: str) -> dict[str, object]:
    mapping = _mapping(value, location)
    for field, descriptor in mapping.items():
        _require_field_name(field, location)
        if not isinstance(descriptor, str) or not descriptor:
            raise BatchCheckpointError(f"{location}[{field!r}] must be a non-empty string")
    return mapping


def _string_array(value: object, location: str) -> list[str]:
    if not isinstance(value, list):
        raise BatchCheckpointError(f"{location} must be an array")
    if any(not isinstance(item, str) or not item for item in value):
        raise BatchCheckpointError(f"{location} must contain only non-empty strings")
    duplicates = sorted({item for item in value if value.count(item) > 1})
    if duplicates:
        raise BatchCheckpointError(f"{location} must contain unique values: {duplicates!r}")
    return value


def _require_field_name(value: str, location: str) -> None:
    if not value:
        raise BatchCheckpointError(f"{location} field names must be non-empty strings")


def _require_exact_keys(
    value: Mapping[str, object], expected: frozenset[str], location: str
) -> None:
    missing = sorted(expected - set(value))
    if missing:
        raise BatchCheckpointError(
            f"{location} is missing required key(s): " + ", ".join(repr(key) for key in missing)
        )
    unknown = sorted(set(value) - expected)
    if unknown:
        raise BatchCheckpointError(
            f"{location} contains unknown key(s): " + ", ".join(repr(key) for key in unknown)
        )


def _mapping(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise BatchCheckpointError(f"{location} must be an object with string keys")
    return dict(value)
