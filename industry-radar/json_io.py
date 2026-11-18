"""Shared canonical JSON hashing and atomic file I/O helpers."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path


def canonical_json(value):
    """Serialize ``value`` with stable keys and zero non-finite values."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_sha256(value):
    """Return the SHA-256 hex digest of ``canonical_json(value)``."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path):
    """Return the streaming SHA-256 hex digest of ``path``."""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path, *, label="JSON artifact"):
    """Load and parse a UTF-8 JSON file, raising ``ValueError`` on failure."""

    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read {label} {path}: {error}") from error


@contextmanager
def locked_file(target_path, *, blocking=True):
    """Acquire an advisory flock on ``<target_path>.lock``."""

    target = os.path.abspath(os.fspath(target_path))
    target_dir = os.path.dirname(target)
    os.makedirs(target_dir, exist_ok=True)
    lock_path = target + ".lock"
    flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    with open(lock_path, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), flags)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def write_json_atomic(
    path,
    payload,
    *,
    indent=2,
    sort_keys=True,
    allow_nan=True,
    ensure_ascii=False,
    compact=False,
    prefix=None,
    trailing_newline=False,
    replace_fn=None,
):
    """Write ``payload`` as UTF-8 JSON via fsync + atomic directory replace."""

    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_prefix = prefix or f".{target.name}."
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=tmp_prefix,
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            dump_kwargs = {
                "ensure_ascii": ensure_ascii,
                "sort_keys": sort_keys,
                "allow_nan": allow_nan,
            }
            if compact:
                dump_kwargs["separators"] = (",", ":")
            elif indent is not None:
                dump_kwargs["indent"] = indent
            json.dump(payload, handle, **dump_kwargs)
            if trailing_newline:
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        (replace_fn or os.replace)(str(temporary), str(target))
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)
