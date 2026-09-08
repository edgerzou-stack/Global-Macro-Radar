"""Opt-in live RSS failure memory; never supplies articles or hides failures."""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path


def _timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result


def _validate(state):
    try:
        if state["schema_version"] != 1 or not isinstance(state["sources"], dict):
            raise ValueError("invalid schema")
        for url, record in state["sources"].items():
            if not isinstance(url, str) or not isinstance(record, dict):
                raise ValueError("invalid source")
            count = record["consecutive_failures"]
            if type(count) is not int or count < 0:
                raise ValueError("invalid failure count")
            if record["last_status"] not in {"failed", "healthy", "degraded"}:
                raise ValueError("invalid status")
            if not isinstance(record["prior_error"], str):
                raise ValueError("invalid error")
            checked = _timestamp(record["last_checked_at"])
            if record["retry_after"] is not None:
                retry = _timestamp(record["retry_after"])
                if not timedelta(0) < retry - checked <= timedelta(hours=24):
                    raise ValueError("invalid cooldown bound")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"RSS source state invalid: {exc}") from exc


@contextmanager
def locked_source_state(path):
    """Serialize fetch/update transactions; reject corrupt memory before fetching."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(path) + ".lock").open("a") as lock:
        # Fail promptly rather than hang behind an overlapping live fetch.
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            try:
                state = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "sources": {}}
            except (OSError, ValueError) as exc:
                raise ValueError(f"RSS source state unreadable: {exc}") from exc
            _validate(state)
            yield state
            _validate(state)
            fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
            try:
                with os.fdopen(fd, "w") as output:
                    json.dump(state, output, indent=2, sort_keys=True)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def active_cooldown(record, now):
    if record and now < _timestamp(record["last_checked_at"]):
        raise ValueError("RSS source state is newer than collection clock")
    return bool(record and record["retry_after"] and now < _timestamp(record["retry_after"]))


def record_results(state, health_results, now):
    for health in health_results:
        if health.get("cooldown_active"):
            continue
        previous = state["sources"].get(health["url"], {})
        eligible = health["status"] == "failed" and health.get("cooldown_eligible", False)
        count = previous.get("consecutive_failures", 0) + 1 if eligible else 0
        delay = min(24, 6 * 2 ** min(max(count - 2, 0), 2)) if count >= 2 else 0
        state["sources"][health["url"]] = {
            "consecutive_failures": count,
            "last_status": health["status"],
            "prior_error": health["error"],
            "last_checked_at": now.isoformat(),
            "retry_after": (now + timedelta(hours=delay)).isoformat() if delay else None,
        }
        health["consecutive_failures"] = count
        health["retry_after"] = state["sources"][health["url"]]["retry_after"]
