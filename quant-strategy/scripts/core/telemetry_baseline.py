"""Robust phase-duration baselines that remain inert until sufficiently sampled."""

from __future__ import annotations

import math
import json
import statistics
from pathlib import Path


BASELINE_SCHEMA_VERSION = 1


def load_telemetry_snapshots(artifact_root, *, exclude_run_id=None):
    snapshots = []
    root = Path(artifact_root).expanduser().resolve()
    for path in sorted(root.glob("*/run-manifest.json")):
        if exclude_run_id and path.parent.name == exclude_run_id:
            continue
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            payload = envelope.get("payload")
            telemetry = payload.get("telemetry") if isinstance(payload, dict) else None
            if isinstance(telemetry, dict):
                snapshots.append(telemetry)
        except (OSError, json.JSONDecodeError):
            continue
    return snapshots


def _phase_durations(snapshot):
    phases = snapshot.get("phases") if isinstance(snapshot, dict) else None
    if not isinstance(phases, dict):
        return {}
    durations = {}
    for phase, payload in phases.items():
        value = payload.get("duration_seconds") if isinstance(payload, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        value = float(value)
        if math.isfinite(value) and value >= 0:
            durations[str(phase)] = value
    return durations


def _nearest_rank(values, quantile):
    ordered = sorted(float(value) for value in values)
    index = max(0, math.ceil(float(quantile) * len(ordered)) - 1)
    return ordered[index]


def build_phase_baseline(snapshots, *, min_samples=10):
    samples = {}
    valid_snapshots = 0
    for snapshot in snapshots:
        durations = _phase_durations(snapshot)
        if not durations:
            continue
        valid_snapshots += 1
        for phase, duration in durations.items():
            samples.setdefault(phase, []).append(duration)
    ready = valid_snapshots >= int(min_samples)
    phases = {}
    for phase, values in sorted(samples.items()):
        if len(values) < int(min_samples):
            continue
        phases[phase] = {
            "samples": len(values),
            "p50_seconds": round(float(statistics.median(values)), 6),
            "p95_seconds": round(_nearest_rank(values, 0.95), 6),
        }
    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "status": "ready" if ready and phases else "warming_up",
        "minimum_samples": int(min_samples),
        "sample_count": valid_snapshots,
        "phases": phases,
    }


def evaluate_phase_durations(
    snapshot,
    baseline,
    *,
    multiplier=1.5,
    minimum_regression_seconds=5.0,
):
    alerts = []
    if not isinstance(baseline, dict) or baseline.get("status") != "ready":
        return {"status": "warming_up", "alerts": alerts}
    observed = _phase_durations(snapshot)
    for phase, reference in baseline.get("phases", {}).items():
        if phase not in observed or not isinstance(reference, dict):
            continue
        p95 = float(reference.get("p95_seconds", 0))
        threshold = max(
            p95 * float(multiplier),
            p95 + float(minimum_regression_seconds),
        )
        if observed[phase] > threshold:
            alerts.append(
                {
                    "phase": phase,
                    "observed_seconds": round(observed[phase], 6),
                    "p95_seconds": round(p95, 6),
                    "threshold_seconds": round(threshold, 6),
                }
            )
    return {
        "status": "regressed" if alerts else "healthy",
        "alerts": alerts,
    }
