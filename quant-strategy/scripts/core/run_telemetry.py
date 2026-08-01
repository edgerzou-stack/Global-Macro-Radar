"""Structured pipeline timings, child metrics, and degradation summaries."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path


TELEMETRY_SCHEMA_VERSION = 1
METRIC_PREFIX = "PIPELINE_METRIC "
_COMPONENT_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}")


def metric_line(component: str, counters: dict, *, dimensions=None) -> str:
    if not _COMPONENT_PATTERN.fullmatch(str(component)):
        raise ValueError(f"invalid metric component: {component!r}")
    normalized = _validated_counters(counters)
    if normalized is None:
        raise ValueError("metric counters must be finite nonnegative numbers")
    normalized_dimensions = _validated_dimensions(dimensions)
    if dimensions is not None and normalized_dimensions is None:
        raise ValueError("metric dimensions must be bounded string pairs")
    payload = {"component": component, "counters": normalized}
    if normalized_dimensions:
        payload["dimensions"] = normalized_dimensions
    return METRIC_PREFIX + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validated_counters(counters):
    if not isinstance(counters, dict) or not counters:
        return None
    normalized = {}
    for key, value in counters.items():
        if not isinstance(key, str) or not key:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number) or number < 0:
            return None
        normalized[key] = int(value) if number.is_integer() else number
    return normalized


def _validated_dimensions(dimensions):
    if dimensions is None:
        return {}
    if not isinstance(dimensions, dict) or len(dimensions) > 16:
        return None
    normalized = {}
    for key, value in dimensions.items():
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 64
            or not isinstance(value, str)
            or not value
            or len(value) > 128
        ):
            return None
        normalized[key] = value
    return normalized


def _nested_item_count(value):
    if not isinstance(value, dict):
        return 0
    return sum(
        len(items)
        for items in value.values()
        if isinstance(items, (list, tuple, dict))
    )


def _semantic_summary(name, value):
    if not isinstance(value, dict):
        return None
    summary = {"summary_schema_version": 1}
    if name == "global_screen":
        strategies = value.get("results")
        if not isinstance(strategies, dict):
            strategies = value.get("strategies")
        if isinstance(strategies, dict):
            summary["strategy_count"] = len(strategies)
            summary["candidate_count"] = _nested_item_count(strategies)
        for field in ("snapshot_date", "mode"):
            if isinstance(value.get(field), str):
                summary[field] = value[field]
    elif name == "hot_spot":
        if isinstance(value.get("schema_version"), int):
            summary["artifact_schema_version"] = value["schema_version"]
        for field in ("run_id", "effective_date", "status"):
            if isinstance(value.get(field), (str, int)):
                summary[field] = value[field]
        if isinstance(value.get("hot_news_count"), int):
            summary["hot_news_count"] = value["hot_news_count"]
        if isinstance(value.get("data"), dict):
            summary["candidate_count"] = _nested_item_count(value["data"])
    elif name == "prepared_manifest":
        if isinstance(value.get("schema_version"), int):
            summary["artifact_schema_version"] = value["schema_version"]
        for field in ("run_id", "effective_date", "news_count"):
            if isinstance(value.get(field), (str, int)):
                summary[field] = value[field]
    return summary if len(summary) > 1 else None


def read_json_status(database_path, key):
    """Read one JSON status row through SQLite's fail-closed read-only mode."""
    database_uri = Path(database_path).expanduser().resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(database_uri, uri=True, timeout=30.0) as connection:
        try:
            row = connection.execute(
                "SELECT value FROM meta_data WHERE key=?",
                (str(key),),
            ).fetchone()
        except sqlite3.OperationalError as error:
            if "no such table: meta_data" not in str(error):
                raise
            return None
    if not row:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def summarize_artifacts(artifacts):
    """Return content evidence and coarse shape without copying business data."""
    summaries = {}
    for name, raw_path in (artifacts or {}).items():
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            summaries[str(name)] = {"available": False}
            continue
        try:
            content = path.read_bytes()
        except OSError:
            summaries[str(name)] = {"available": False}
            continue
        summary = {
            "available": True,
            "path": str(path),
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        if path.suffix.lower() == ".json":
            try:
                value = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = None
            if isinstance(value, dict):
                summary["json_shape"] = {
                    "type": "object",
                    "top_level_keys": len(value),
                }
            elif isinstance(value, list):
                summary["json_shape"] = {
                    "type": "array",
                    "items": len(value),
                }
            semantics = _semantic_summary(str(name), value)
            if semantics is not None:
                summary["semantic_summary"] = semantics
        summaries[str(name)] = summary
    return summaries


class PipelineTelemetry:
    def __init__(self, *, monotonic=time.monotonic, now_utc=None):
        self._monotonic = monotonic
        self._now_utc = now_utc or (
            lambda: dt.datetime.now(dt.timezone.utc)
        )
        self.started_at = self._now_utc().isoformat()
        self.commands = []
        self.metrics = defaultdict(lambda: defaultdict(float))
        self.metric_dimensions = defaultdict(lambda: defaultdict(set))

    @classmethod
    def from_snapshot(cls, snapshot, *, monotonic=time.monotonic, now_utc=None):
        telemetry = cls(monotonic=monotonic, now_utc=now_utc)
        if not isinstance(snapshot, dict):
            return telemetry
        if snapshot.get("schema_version") != TELEMETRY_SCHEMA_VERSION:
            return telemetry
        if isinstance(snapshot.get("started_at"), str):
            telemetry.started_at = snapshot["started_at"]
        commands = snapshot.get("commands")
        if isinstance(commands, list):
            telemetry.commands = [
                dict(item) for item in commands if isinstance(item, dict)
            ]
        metrics = snapshot.get("metrics")
        if isinstance(metrics, dict):
            for component, counters in metrics.items():
                normalized = _validated_counters(counters)
                if (
                    _COMPONENT_PATTERN.fullmatch(str(component))
                    and normalized is not None
                ):
                    for key, value in normalized.items():
                        telemetry.metrics[component][key] += value
        dimensions = snapshot.get("metric_dimensions")
        if isinstance(dimensions, dict):
            for component, fields in dimensions.items():
                if not isinstance(fields, dict):
                    continue
                for key, values in fields.items():
                    if not isinstance(values, list):
                        continue
                    for value in values:
                        normalized = _validated_dimensions({key: value})
                        if normalized:
                            telemetry.metric_dimensions[component][key].add(value)
        return telemetry

    def begin_command(self, phase: str, command: str):
        return {
            "phase": str(phase),
            "command": str(command),
            "started_at": self._now_utc().isoformat(),
            "monotonic": self._monotonic(),
        }

    def finish_command(self, token, *, status: str, return_code=None):
        duration = max(0.0, self._monotonic() - float(token["monotonic"]))
        record = {
            "phase": token["phase"],
            "command": token["command"],
            "status": str(status),
            "started_at": token["started_at"],
            "duration_seconds": round(duration, 6),
        }
        if return_code is not None:
            record["return_code"] = int(return_code)
        self.commands.append(record)
        return record

    def record_checkpoint_reuse(self, phase: str, command: str):
        self.commands.append(
            {
                "phase": str(phase),
                "command": str(command),
                "status": "checkpoint_reused",
                "started_at": self._now_utc().isoformat(),
                "duration_seconds": 0.0,
            }
        )

    def has_command(self, command: str) -> bool:
        return any(item.get("command") == command for item in self.commands)

    def consume_output_line(self, line: str) -> bool:
        text = str(line).strip()
        if not text.startswith(METRIC_PREFIX):
            return False
        try:
            payload = json.loads(text[len(METRIC_PREFIX) :])
        except json.JSONDecodeError:
            return False
        component = payload.get("component") if isinstance(payload, dict) else None
        counters = payload.get("counters") if isinstance(payload, dict) else None
        dimensions = (
            payload.get("dimensions") if isinstance(payload, dict) else None
        )
        normalized = _validated_counters(counters)
        normalized_dimensions = _validated_dimensions(dimensions)
        if (
            not isinstance(component, str)
            or not _COMPONENT_PATTERN.fullmatch(component)
            or normalized is None
            or normalized_dimensions is None
        ):
            return False
        for key, value in normalized.items():
            self.metrics[component][key] += value
        for key, value in normalized_dimensions.items():
            self.metric_dimensions[component][key].add(value)
        return True

    def snapshot(self, *, status: str):
        phases = {}
        grouped = defaultdict(list)
        for command in self.commands:
            grouped[str(command.get("phase", "unknown"))].append(command)
        for phase, commands in sorted(grouped.items()):
            phases[phase] = {
                "duration_seconds": round(
                    sum(float(item.get("duration_seconds", 0)) for item in commands),
                    6,
                ),
                "command_count": len(commands),
                "completed": sum(
                    item.get("status") in {"completed", "checkpoint_reused"}
                    for item in commands
                ),
                "failed": sum(item.get("status") == "failed" for item in commands),
            }
        active_duration = round(
            sum(float(item.get("duration_seconds", 0)) for item in self.commands),
            6,
        )
        metrics = {
            component: {
                key: int(value) if float(value).is_integer() else round(value, 6)
                for key, value in sorted(counters.items())
            }
            for component, counters in sorted(self.metrics.items())
        }
        metric_dimensions = {
            component: {
                key: sorted(values)
                for key, values in sorted(fields.items())
            }
            for component, fields in sorted(self.metric_dimensions.items())
        }
        return {
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "status": str(status),
            "started_at": self.started_at,
            "updated_at": self._now_utc().isoformat(),
            "active_duration_seconds": active_duration,
            "commands": [dict(item) for item in self.commands],
            "phases": phases,
            "metrics": metrics,
            "metric_dimensions": metric_dimensions,
        }


def build_degradation_summary(run_id: str, settlement, nav) -> dict:
    signals = []
    settlement_totals = {"pending": 0, "blocked": 0, "deferred": 0}
    nav_totals = {
        "fresh": 0,
        "certified_carry_forward": 0,
        "unavailable": 0,
    }

    if not isinstance(settlement, dict) or settlement.get("run_id") != run_id:
        signals.append("missing_or_mismatched_settlement_status")
    else:
        for market in settlement.get("markets", {}).values():
            if not isinstance(market, dict):
                continue
            settlement_totals["pending"] += int(market.get("pending", 0) or 0)
            settlement_totals["blocked"] += int(market.get("blocked", 0) or 0)
            deferred = market.get("deferred", [])
            settlement_totals["deferred"] += (
                len(deferred) if isinstance(deferred, list) else 0
            )
        for key in ("pending", "blocked", "deferred"):
            if settlement_totals[key]:
                signals.append(f"settlement_{key}")

    if not isinstance(nav, dict) or nav.get("run_id") != run_id:
        signals.append("missing_or_mismatched_nav_status")
    else:
        for strategy in nav.get("strategies", {}).values():
            status = strategy.get("status") if isinstance(strategy, dict) else None
            if status in nav_totals:
                nav_totals[status] += 1
        if nav_totals["certified_carry_forward"]:
            signals.append("nav_certified_carry_forward")
        if nav_totals["unavailable"]:
            signals.append("nav_unavailable")

    return {
        "status": "completed_degraded" if signals else "completed",
        "signals": signals,
        "settlement": settlement_totals,
        "nav": nav_totals,
    }
