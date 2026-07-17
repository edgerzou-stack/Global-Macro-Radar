"""Cross-process writer fence keyed by canonical SQLite database path."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Iterator, Optional


class WriterLockUnavailable(RuntimeError):
    """Raised when another runner or release process owns the database fence."""


def canonical_database_path(database_path) -> Path:
    return Path(database_path).expanduser().resolve()


def writer_lock_path(database_path, lock_dir=None) -> Path:
    database = canonical_database_path(database_path)
    root = Path(
        lock_dir
        or os.environ.get("PIPELINE_LOCK_DIR")
        or Path(tempfile.gettempdir()) / "global-macro-radar-writer-locks"
    ).expanduser().resolve()
    digest = hashlib.sha256(str(database).encode("utf-8")).hexdigest()
    return root / f"{digest}.lock"


@contextlib.contextmanager
def writer_fence(
    database_path,
    *,
    owner: str,
    timeout: float = 0.0,
    poll_interval: float = 0.05,
    lock_dir: Optional[Path] = None,
) -> Iterator[Path]:
    """Hold the exclusive fence for one database or fail after ``timeout``."""
    if not owner or not isinstance(owner, str):
        raise ValueError("writer fence owner must be a non-empty string")
    if timeout < 0:
        raise ValueError("writer fence timeout must be non-negative")
    if poll_interval <= 0:
        raise ValueError("writer fence poll_interval must be positive")

    database = canonical_database_path(database_path)
    lock_path = writer_lock_path(database, lock_dir=lock_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(lock_path.parent, 0o700)
    except OSError:
        pass

    descriptor = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise WriterLockUnavailable(
                        f"Database writer fence is already held for {database}"
                    ) from error
                remaining = max(0.0, deadline - time.monotonic())
                time.sleep(min(poll_interval, remaining))

        metadata = {
            "database_path": str(database),
            "owner": owner,
            "pid": os.getpid(),
            "acquired_at_unix": time.time(),
        }
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, json.dumps(metadata, sort_keys=True).encode("utf-8"))
        os.fsync(descriptor)
        yield lock_path
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
