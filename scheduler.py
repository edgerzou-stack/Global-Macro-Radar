"""Explicit, fail-closed scheduler for the unified pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HEARTBEAT = Path(os.environ.get("SCHEDULER_HEARTBEAT", "/tmp/gmr-scheduler-heartbeat"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Scheduler] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)
STATE_FILENAMES = (
    "global_screen.json",
    "hot_spot_today.json",
    "universes.json",
    "universes_backup.json",
)
DEFAULT_PIPELINE_TIMEOUT_SECONDS = 2 * 60 * 60
DEFAULT_SHUTDOWN_GRACE_SECONDS = 30
HEALTH_MAX_AGE_SECONDS = 180
_HEALTH_STATE = "starting"
_LAST_SUCCESS_AT = None
_SHUTDOWN_REQUESTED = False


def _atomic_heartbeat(state=None):
    global _HEALTH_STATE, _LAST_SUCCESS_AT
    previous_state = _HEALTH_STATE
    if state is not None:
        _HEALTH_STATE = state
    now = time.time()
    if _HEALTH_STATE == "idle" and previous_state == "running":
        _LAST_SUCCESS_AT = now
    temporary = HEARTBEAT.with_suffix(".tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": _HEALTH_STATE,
        "updated_at": now,
        "last_success_at": _LAST_SUCCESS_AT,
    }
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="ascii")
    os.replace(str(temporary), str(HEARTBEAT))


def healthcheck(path=HEARTBEAT, *, now=None, max_age=HEALTH_MAX_AGE_SECONDS):
    try:
        payload = json.loads(Path(path).read_text(encoding="ascii"))
        updated_at = float(payload["updated_at"])
        state = payload["state"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    age = (time.time() if now is None else float(now)) - updated_at
    return 0 <= age < max_age and state in {"idle", "running"}


def _request_shutdown(_signum, _frame):
    global _SHUTDOWN_REQUESTED
    _SHUTDOWN_REQUESTED = True


def _terminate_process(process, grace_seconds):
    if process.poll() is not None:
        return process.returncode
    process_group = getattr(process, "pid", None)
    if process_group is None:
        process.terminate()
    else:
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        return process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        if process_group is None:
            process.kill()
        else:
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return process.wait()


def _state_paths(config):
    state_root = os.environ.get("PIPELINE_STATE_DIR")
    if not state_root:
        return None
    runtime_root = Path(
        os.environ.get("PROJECT_ROOT", ROOT / "quant-strategy")
    ).expanduser().resolve()
    mode_state_root = Path(state_root).expanduser().resolve() / config.mode
    return runtime_root, mode_state_root


def _atomic_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(source, temporary)
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(target))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _restore_pipeline_state(config):
    roots = _state_paths(config)
    if roots is None:
        return
    runtime_root, state_root = roots
    for filename in STATE_FILENAMES:
        target = runtime_root / filename
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        source = state_root / filename
        if source.is_file():
            _atomic_copy(source, target)


def _persist_pipeline_state(config):
    roots = _state_paths(config)
    if roots is None:
        return
    runtime_root, state_root = roots
    for filename in STATE_FILENAMES:
        source = runtime_root / filename
        if source.is_file():
            _atomic_copy(source, state_root / filename)


def build_pipeline_command(config):
    command = [
        str(ROOT / "run_all.sh"),
        "--mode",
        config.mode,
        "--database",
        str(Path(config.database).expanduser().resolve()),
    ]
    if config.artifact_root:
        command.extend(["--artifact-root", config.artifact_root])
    if config.fixture_root:
        command.extend(["--fixture-root", config.fixture_root])
    if config.mode == "production" and config.confirm_production_writes:
        command.append("--confirm-production-writes")
    delivery_mode = getattr(config, "delivery_mode", "sink")
    command.extend(["--delivery-mode", delivery_mode])
    if delivery_mode == "live" and getattr(config, "confirm_live_delivery", False):
        command.append("--confirm-live-delivery")
    return command


def run_radar_pipeline(config):
    logger.info("Triggering Global Macro Radar mode=%s", config.mode)
    try:
        _restore_pipeline_state(config)
        process = subprocess.Popen(
            build_pipeline_command(config),
            cwd=str(ROOT),
            stdout=sys.stdout,
            stderr=sys.stderr,
            text=True,
            start_new_session=True,
        )
    except Exception:
        logger.exception("Pipeline failed before the child process started")
        _atomic_heartbeat("failed")
        return 1
    _atomic_heartbeat("running")
    timeout_seconds = float(
        getattr(config, "pipeline_timeout_seconds", DEFAULT_PIPELINE_TIMEOUT_SECONDS)
    )
    grace_seconds = float(
        getattr(config, "shutdown_grace_seconds", DEFAULT_SHUTDOWN_GRACE_SECONDS)
    )
    deadline = time.monotonic() + timeout_seconds
    while True:
        if _SHUTDOWN_REQUESTED:
            logger.warning("Shutdown requested; terminating the active pipeline")
            _terminate_process(process, grace_seconds)
            _atomic_heartbeat("stopping")
            return 143
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.error("Pipeline exceeded timeout of %.0f seconds", timeout_seconds)
            _terminate_process(process, grace_seconds)
            _atomic_heartbeat("failed")
            return 124
        try:
            return_code = process.wait(timeout=min(30, remaining))
            break
        except subprocess.TimeoutExpired:
            _atomic_heartbeat()
    if return_code:
        logger.error("Pipeline failed with return code %s", return_code)
        _atomic_heartbeat("failed")
    else:
        _persist_pipeline_state(config)
        logger.info("Pipeline executed successfully.")
        _atomic_heartbeat("idle")
    return return_code


def _parse_schedule_times(value):
    times = [item.strip() for item in (value or "").split(",") if item.strip()]
    if not times:
        raise ValueError("At least one explicit scheduler time is required")
    for value in times:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError(f"Invalid scheduler time: {value!r}")
    if len(set(times)) != len(times):
        raise ValueError("Duplicate scheduler times are not allowed")
    return times


def build_parser():
    parser = argparse.ArgumentParser(description="Global Macro Radar Scheduler")
    parser.add_argument("--run-now", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument(
        "--mode",
        choices=("offline", "shadow", "live-shadow", "production"),
        default=os.environ.get("PIPELINE_MODE"),
    )
    parser.add_argument(
        "--delivery-mode",
        choices=("sink", "live"),
        default=os.environ.get("DELIVERY_MODE", "sink"),
    )
    parser.add_argument(
        "--confirm-live-delivery",
        action="store_true",
        default=os.environ.get("CONFIRM_LIVE_DELIVERY") == "YES",
    )
    parser.add_argument(
        "--pipeline-timeout-seconds",
        type=float,
        default=float(
            os.environ.get(
                "PIPELINE_TIMEOUT_SECONDS", DEFAULT_PIPELINE_TIMEOUT_SECONDS
            )
        ),
    )
    parser.add_argument(
        "--shutdown-grace-seconds",
        type=float,
        default=float(
            os.environ.get(
                "SCHEDULER_SHUTDOWN_GRACE_SECONDS",
                DEFAULT_SHUTDOWN_GRACE_SECONDS,
            )
        ),
    )
    parser.add_argument("--database", default=os.environ.get("PIPELINE_DATABASE"))
    parser.add_argument("--artifact-root", default=os.environ.get("PIPELINE_ARTIFACT_ROOT"))
    parser.add_argument("--fixture-root", default=os.environ.get("PIPELINE_FIXTURE_ROOT"))
    parser.add_argument(
        "--confirm-production-writes",
        action="store_true",
        default=os.environ.get("CONFIRM_PRODUCTION_WRITES") == "YES",
    )
    parser.add_argument("--schedule-times", default=os.environ.get("SCHEDULER_TIMES"))
    parser.add_argument(
        "--enable-scheduler",
        action="store_true",
        default=os.environ.get("SCHEDULER_ENABLED") == "YES",
        help="Explicitly enable the persistent scheduler loop",
    )
    return parser


def main(argv=None):
    global _SHUTDOWN_REQUESTED
    args = build_parser().parse_args(argv)
    if args.healthcheck:
        return 0 if healthcheck() else 1
    if args.pipeline_timeout_seconds <= 0:
        raise SystemExit("--pipeline-timeout-seconds must be positive")
    if args.shutdown_grace_seconds < 0:
        raise SystemExit("--shutdown-grace-seconds must be non-negative")
    if not args.mode or not args.database:
        raise SystemExit("--mode and --database (or matching env vars) are required")
    if args.mode == "production" and not Path(args.database).expanduser().is_file():
        raise SystemExit("production scheduler database must already exist")
    if args.mode == "production" and not args.confirm_production_writes:
        raise SystemExit(
            "production scheduler requires CONFIRM_PRODUCTION_WRITES=YES "
            "or --confirm-production-writes"
        )
    if args.delivery_mode == "live":
        if args.mode != "production":
            raise SystemExit("live delivery is only allowed in production mode")
        if not args.confirm_live_delivery:
            raise SystemExit(
                "live delivery requires CONFIRM_LIVE_DELIVERY=YES "
                "or --confirm-live-delivery"
            )
    elif args.confirm_live_delivery:
        raise SystemExit(
            "--confirm-live-delivery is only valid with --delivery-mode live"
        )
    if args.mode == "offline" and not args.fixture_root:
        raise SystemExit("offline scheduler requires --fixture-root")
    if args.run_now:
        _SHUTDOWN_REQUESTED = False
        previous_handlers = {
            signum: signal.signal(signum, _request_shutdown)
            for signum in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            return run_radar_pipeline(args)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    if not args.enable_scheduler:
        raise SystemExit(
            "persistent scheduling is disabled; pass --enable-scheduler "
            "or set SCHEDULER_ENABLED=YES"
        )

    try:
        import schedule
    except ImportError as error:
        raise SystemExit("scheduler runtime requires the 'schedule' package") from error
    times = _parse_schedule_times(args.schedule_times)
    for run_time in times:
        schedule.every().day.at(run_time).do(run_radar_pipeline, args)
    _SHUTDOWN_REQUESTED = False
    previous_handlers = {
        signum: signal.signal(signum, _request_shutdown)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    logger.info("Scheduler active at: %s", ", ".join(times))
    _atomic_heartbeat("idle")
    try:
        while not _SHUTDOWN_REQUESTED:
            schedule.run_pending()
            _atomic_heartbeat()
            time.sleep(30)
        _atomic_heartbeat("stopping")
        return 0
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
