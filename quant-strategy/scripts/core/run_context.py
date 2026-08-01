"""Immutable pipeline run identity and atomic artifact helpers.

Every stage in one pipeline run must receive the same context.  Checkpoints and
artifacts carry that identity so output from another date, configuration, mode,
or database cannot be silently reused.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional


RUN_CONTEXT_SCHEMA_VERSION = 1
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
FIXTURE_FILE_MAP = {
    "RADAR_CONFIG": "radar_config.yaml",
    "RADAR_RSS_FIXTURE": "radar_rss.json",
    "RADAR_SCORED_ARTICLES_FIXTURE": "radar_scored_articles.json",
    "HOT_SPOT_FIXTURE": "hot_spot.json",
    "UNIVERSE_FIXTURE": "universe.json",
    "GLOBAL_SCREEN_FIXTURE": "global_screen.json",
    "STOCK_API_HEALTH_FIXTURE": "stock_api_health.json",
    "HISTORICAL_PRICE_FIXTURE": "historical_prices.json",
}


class RunContextError(ValueError):
    """Base error for invalid or incompatible run metadata."""


class RunIdentityMismatch(RunContextError):
    """Raised when an artifact belongs to a different run identity."""


class RunMode(str, Enum):
    OFFLINE = "offline"
    SHADOW = "shadow"
    LIVE_SHADOW = "live-shadow"
    PRODUCTION = "production"


class DeliveryMode(str, Enum):
    DISABLED = "disabled"
    SINK = "sink"
    LIVE = "live"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclasses.dataclass(frozen=True)
class FixtureBundle:
    """Resolved, complete fixture input set for a reproducible pipeline run."""

    root: Path
    paths: tuple[tuple[str, Path], ...]
    hashes: tuple[tuple[str, str], ...]

    @classmethod
    def from_root(cls, root: os.PathLike) -> "FixtureBundle":
        fixture_root = Path(root).expanduser().resolve()
        if not fixture_root.is_dir():
            raise RunContextError(
                f"Fixture root must be an existing directory: {fixture_root}"
            )

        paths = []
        hashes = []
        for environment_name, filename in FIXTURE_FILE_MAP.items():
            candidate = fixture_root / filename
            resolved = candidate.resolve()
            try:
                resolved.relative_to(fixture_root)
            except ValueError as error:
                raise RunContextError(
                    f"Fixture path escapes fixture root: {environment_name}={candidate}"
                ) from error
            if not resolved.is_file():
                raise RunContextError(
                    f"Missing required fixture: {environment_name} ({candidate})"
                )
            paths.append((environment_name, resolved))
            hashes.append((environment_name, _sha256_file(resolved)))

        return cls(
            root=fixture_root,
            paths=tuple(paths),
            hashes=tuple(hashes),
        )

    @property
    def environment(self) -> dict[str, str]:
        return {name: str(path) for name, path in self.paths}

    @property
    def manifest(self) -> dict[str, dict[str, str]]:
        path_by_name = dict(self.paths)
        return {
            name: {"path": str(path_by_name[name]), "sha256": digest}
            for name, digest in self.hashes
        }


def _normalize_for_hash(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_for_hash(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_for_hash(item) for item in value]
    if isinstance(value, set):
        normalized = [_normalize_for_hash(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, os.PathLike):
        return os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(value))))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"Unsupported configuration value for hashing: {type(value).__name__}")


def configuration_hash(configuration: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 for configuration that changes run semantics."""
    canonical = json.dumps(
        _normalize_for_hash(configuration),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_effective_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise RunContextError("effective_date must use YYYY-MM-DD") from error


def _parse_created_at(value: Optional[Any]) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.timezone.utc)
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError as error:
            raise RunContextError("created_at must be an ISO-8601 timestamp") from error
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise RunContextError("created_at must be timezone-aware")
    return value.astimezone(dt.timezone.utc)


@dataclasses.dataclass(frozen=True)
class RunContext:
    run_id: str
    effective_date: dt.date
    config_hash: str
    mode: RunMode
    database_path: Path
    artifact_root: Path
    created_at: dt.datetime
    delivery_mode: DeliveryMode = DeliveryMode.SINK
    fixture_paths: tuple[tuple[str, str], ...] = ()
    database_source_sha256: Optional[str] = None
    allow_duplicate_effective_date_delivery: bool = False

    @classmethod
    def create(
        cls,
        *,
        mode: Any,
        database_path: os.PathLike,
        effective_date: Any,
        configuration: Mapping[str, Any],
        artifact_root: os.PathLike,
        run_id: Optional[str] = None,
        created_at: Optional[Any] = None,
        delivery_mode: Any = DeliveryMode.SINK,
        fixture_paths: Optional[Mapping[str, os.PathLike]] = None,
        database_source_sha256: Optional[str] = None,
        allow_duplicate_effective_date_delivery: bool = False,
    ) -> "RunContext":
        try:
            normalized_mode = mode if isinstance(mode, RunMode) else RunMode(str(mode))
        except ValueError as error:
            raise RunContextError(f"Unsupported pipeline mode: {mode!r}") from error
        try:
            normalized_delivery_mode = (
                delivery_mode
                if isinstance(delivery_mode, DeliveryMode)
                else DeliveryMode(str(delivery_mode))
            )
        except ValueError as error:
            raise RunContextError(
                f"Unsupported delivery mode: {delivery_mode!r}"
            ) from error
        if (
            normalized_mode is not RunMode.PRODUCTION
            and normalized_delivery_mode is DeliveryMode.LIVE
        ):
            raise RunContextError("Live delivery is only allowed in production mode")
        if type(allow_duplicate_effective_date_delivery) is not bool:
            raise RunContextError(
                "allow_duplicate_effective_date_delivery must be boolean"
            )
        if allow_duplicate_effective_date_delivery and (
            normalized_mode is not RunMode.PRODUCTION
            or normalized_delivery_mode is not DeliveryMode.LIVE
        ):
            raise RunContextError(
                "Duplicate effective-date delivery override requires "
                "production mode with live delivery"
            )

        normalized_run_id = run_id or str(uuid.uuid4())
        if not RUN_ID_PATTERN.fullmatch(normalized_run_id):
            raise RunContextError(
                "run_id must contain 1-128 safe alphanumeric/._:- characters"
            )

        database = Path(database_path).expanduser().resolve()
        artifacts = Path(artifact_root).expanduser().resolve()
        normalized_fixture_paths = tuple(
            sorted(
                (
                    str(name),
                    str(Path(path).expanduser().resolve()),
                )
                for name, path in (fixture_paths or {}).items()
            )
        )
        if normalized_mode is RunMode.PRODUCTION and not database.is_file():
            raise RunContextError(
                f"Production mode requires an existing database: {database}"
            )

        return cls(
            run_id=normalized_run_id,
            effective_date=_parse_effective_date(effective_date),
            config_hash=configuration_hash(configuration),
            mode=normalized_mode,
            database_path=database,
            artifact_root=artifacts,
            created_at=_parse_created_at(created_at),
            delivery_mode=normalized_delivery_mode,
            fixture_paths=normalized_fixture_paths,
            database_source_sha256=(
                str(database_source_sha256).lower()
                if database_source_sha256
                else None
            ),
            allow_duplicate_effective_date_delivery=(
                allow_duplicate_effective_date_delivery
            ),
        )

    @property
    def identity(self) -> dict[str, str]:
        identity = {
            "run_id": self.run_id,
            "effective_date": self.effective_date.isoformat(),
            "config_hash": self.config_hash,
            "mode": self.mode.value,
            "database_path": str(self.database_path),
        }
        if self.database_source_sha256:
            identity["database_source_sha256"] = self.database_source_sha256
        return identity

    def child_environment(self) -> dict[str, str]:
        database_environment = {
            RunMode.OFFLINE: "backtest",
            RunMode.SHADOW: "test",
            RunMode.LIVE_SHADOW: "test",
            RunMode.PRODUCTION: "production",
        }[self.mode]
        run_artifact_dir = str((self.artifact_root / self.run_id).resolve())
        values = {
            "PIPELINE_RUN_ID": self.run_id,
            "RUN_ID": self.run_id,
            "PIPELINE_EFFECTIVE_DATE": self.effective_date.isoformat(),
            "EFFECTIVE_DATE": self.effective_date.isoformat(),
            "PIPELINE_CONFIG_HASH": self.config_hash,
            "PIPELINE_MODE": self.mode.value,
            "PIPELINE_ARTIFACT_DIR": run_artifact_dir,
            "ARTIFACT_DIR": run_artifact_dir,
            "SQLITE_DB_PATH": str(self.database_path),
            "QUANT_DB_ENV": database_environment,
            "DELIVERY_MODE": self.delivery_mode.value,
        }
        if self.allow_duplicate_effective_date_delivery:
            values["PIPELINE_AUTHORIZED_RESEND"] = "1"
        if self.database_source_sha256:
            values[
                "PIPELINE_EXPECTED_DB_SOURCE_SHA256"
            ] = self.database_source_sha256
        if self.mode is RunMode.OFFLINE:
            # Offline fixtures represent a completed end-of-day snapshot.  Four
            # UTC hours into the following date is after the effective session
            # has closed in every supported market.
            values["MOCK_DATE"] = self.effective_date.isoformat()
            values["MOCK_NOW_UTC"] = dt.datetime.combine(
                self.effective_date + dt.timedelta(days=1),
                dt.time(4, 0),
                tzinfo=dt.timezone.utc,
            ).isoformat()
        elif self.mode is RunMode.SHADOW:
            # Deterministic shadow fixtures may pin the logical date.  Live
            # shadow deliberately does not export MOCK_DATE: it uses the
            # effective date for persisted facts while all market-calendar and
            # cache-age decisions retain the real timezone-aware instant.
            values["MOCK_DATE"] = self.effective_date.isoformat()
            values["MOCK_NOW_UTC"] = self.created_at.isoformat()
        if self.mode is not RunMode.PRODUCTION:
            values.update(
                {
                    "DISABLE_REAL_ORDERS": "1",
                }
            )
        if self.mode in {RunMode.SHADOW, RunMode.LIVE_SHADOW, RunMode.PRODUCTION}:
            values["PIPELINE_EXCLUDE_TEST_STRATEGIES"] = "1"
            values["PIPELINE_ENFORCE_SESSION_IDENTITY"] = "1"
        values.update(dict(self.fixture_paths))
        return values

    def assert_envelope_identity(self, envelope: Mapping[str, Any]) -> None:
        actual = envelope.get("run")
        if actual != self.identity:
            raise RunIdentityMismatch(
                f"Artifact run identity mismatch: expected={self.identity!r}, actual={actual!r}"
            )


def artifact_envelope(
    context: RunContext, artifact_type: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    if not artifact_type or not isinstance(artifact_type, str):
        raise RunContextError("artifact_type must be a non-empty string")
    return {
        "schema_version": RUN_CONTEXT_SCHEMA_VERSION,
        "artifact_type": artifact_type,
        "created_at": context.created_at.isoformat(),
        "run": context.identity,
        "payload": dict(payload),
    }


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_artifact_envelope(
    path: os.PathLike,
    context: RunContext,
    artifact_type: str,
    payload: Mapping[str, Any],
) -> Path:
    """Atomically persist one identity-bound JSON artifact."""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(
                artifact_envelope(context, artifact_type, payload),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(target))
        temporary_path = None
        _fsync_directory(target.parent)
        return target
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def read_artifact_envelope(path: os.PathLike) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    with target.open("r", encoding="utf-8") as handle:
        envelope = json.load(handle)
    if envelope.get("schema_version") != RUN_CONTEXT_SCHEMA_VERSION:
        raise RunContextError("Unsupported artifact envelope schema version")
    if not isinstance(envelope.get("payload"), dict):
        raise RunContextError("Artifact envelope payload must be an object")
    return envelope


class CheckpointStore:
    """Identity-bound, atomic checkpoint for one interrupted pipeline run."""

    def __init__(self, path: os.PathLike, context: RunContext):
        self.path = Path(path).expanduser().resolve()
        self.context = context

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"status": "running", "completed_steps": []}
        envelope = read_artifact_envelope(self.path)
        if envelope.get("artifact_type") != "pipeline-checkpoint":
            raise RunContextError("Checkpoint has an unexpected artifact type")
        self.context.assert_envelope_identity(envelope)
        payload = envelope["payload"]
        completed = payload.get("completed_steps")
        if not isinstance(completed, list) or not all(
            isinstance(step, str) for step in completed
        ):
            raise RunContextError("Checkpoint completed_steps must be a string list")
        return payload

    def save(self, payload: Mapping[str, Any]) -> Path:
        return write_artifact_envelope(
            self.path, self.context, "pipeline-checkpoint", payload
        )

    def mark_completed(self, command_key: str, payload: dict[str, Any]) -> None:
        completed = payload.setdefault("completed_steps", [])
        if command_key not in completed:
            completed.append(command_key)
        payload["status"] = "running"
        for key in (
            "interrupted_at",
            "error_type",
            "error_message",
            "failed_command",
            "return_code",
        ):
            payload.pop(key, None)
        self.save(payload)

    def mark_interrupted(
        self, payload: dict[str, Any], error_payload: Mapping[str, Any]
    ) -> None:
        payload.setdefault("completed_steps", [])
        payload["status"] = "interrupted"
        payload.update(dict(error_payload))
        self.save(payload)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        _fsync_directory(self.path.parent)
