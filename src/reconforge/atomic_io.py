"""Collision-safe atomic writes for user-visible text artifacts."""

from __future__ import annotations

import os
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path

_DESTINATION_LOCKS: dict[str, threading.Lock] = {}
_DESTINATION_LOCKS_GUARD = threading.Lock()


def _destination_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.absolute()))
    with _DESTINATION_LOCKS_GUARD:
        return _DESTINATION_LOCKS.setdefault(key, threading.Lock())


def _replace(temporary: Path, destination: Path) -> None:
    for attempt in range(5):
        try:
            os.replace(temporary, destination)
            return
        except PermissionError:
            if attempt == 4:
                raise
            # Virus scanners and file indexers can briefly retain a handle on
            # freshly replaced files on Windows.
            time.sleep(0.01 * (2**attempt))


def atomic_write_text(
    destination: str | Path,
    payload: str,
    *,
    encoding: str = "utf-8",
    newline: str | None = "\n",
) -> Path:
    """Durably stage text beside its destination and replace it atomically.

    A unique temporary file allows concurrent writers to share a destination
    safely.  If staging or replacement fails, an existing destination remains
    untouched and this call's temporary file is removed.
    """

    if not isinstance(payload, str):
        raise TypeError("payload must be a string")

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline=newline) as stream:
            descriptor_open = False
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows can reject simultaneous replacements of the same path even
        # when every writer uses a distinct staging file.
        with _destination_lock(path):
            _replace(temporary, path)
    except BaseException:
        if descriptor_open:
            with suppress(OSError):
                os.close(descriptor)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
    return path.resolve()
