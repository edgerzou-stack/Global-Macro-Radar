"""Versioned, tamper-evident cache for immutable HKEX result documents."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

from free_financials import CumulativeObservation


CACHE_SCHEMA_VERSION = 1
POSITIVE_TTL_SECONDS = 30 * 24 * 60 * 60
NEGATIVE_TTL_SECONDS = 7 * 24 * 60 * 60
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _observation_payload(observation: CumulativeObservation) -> dict:
    return {
        "fiscal_year": observation.fiscal_year,
        "period_code": observation.period_code,
        "duration_months": observation.duration_months,
        "duration_days": observation.duration_days,
        "period_end": observation.period_end.isoformat(),
        "filed_date": observation.filed_date.isoformat(),
        "revenue": observation.revenue,
        "net_income": observation.net_income,
        "currency": observation.currency,
        "source": observation.source,
        "source_document": observation.source_document,
        "reporting_frequency": observation.reporting_frequency,
    }


def _observation_from_payload(payload: dict) -> CumulativeObservation:
    return CumulativeObservation(
        fiscal_year=int(payload["fiscal_year"]),
        period_code=str(payload["period_code"]),
        duration_months=int(payload["duration_months"]),
        duration_days=int(payload.get("duration_days", 0)),
        period_end=date.fromisoformat(payload["period_end"]),
        filed_date=date.fromisoformat(payload["filed_date"]),
        revenue=float(payload["revenue"]),
        net_income=float(payload["net_income"]),
        currency=str(payload["currency"]),
        source=str(payload["source"]),
        source_document=str(payload["source_document"]),
        reporting_frequency=str(payload.get("reporting_frequency", "")),
    )


def _record_sha256(payload: dict) -> str:
    canonical = json.dumps(
        {
            key: value
            for key, value in payload.items()
            if key != "record_sha256"
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class HKEXDocumentCache:
    """Cache deterministic parse outcomes without caching transport failures."""

    def __init__(
        self,
        parser_version: str,
        *,
        positive_ttl_seconds: float = POSITIVE_TTL_SECONDS,
        negative_ttl_seconds: float = NEGATIVE_TTL_SECONDS,
    ):
        self.parser_version = str(parser_version)
        self.positive_ttl_seconds = float(positive_ttl_seconds)
        self.negative_ttl_seconds = float(negative_ttl_seconds)

    @staticmethod
    def _cache_dir() -> Path:
        default = Path(__file__).resolve().parents[1] / ".cache" / "hkex-documents"
        return Path(
            os.environ.get("HKEX_DOCUMENT_CACHE_DIR", default)
        ).expanduser()

    def path_for(self, item: dict) -> Path:
        identity = json.dumps(
            {
                "parser_version": self.parser_version,
                "url": item["pdf_url"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self._cache_dir() / f"{digest}.json"

    def lock_for(self, item: dict) -> threading.Lock:
        key = str(self.path_for(item))
        with _LOCKS_GUARD:
            return _LOCKS.setdefault(key, threading.Lock())

    def read(self, item: dict):
        path = self.path_for(item)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            expected_record_sha256 = str(payload.get("record_sha256", ""))
            if not re.fullmatch(r"[0-9a-f]{64}", expected_record_sha256):
                return None
            if not hmac.compare_digest(
                expected_record_sha256,
                _record_sha256(payload),
            ):
                return None
            if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
                return None
            if payload.get("parser_version") != self.parser_version:
                return None
            if payload.get("url") != item["pdf_url"]:
                return None
            if payload.get("title") != item["title"]:
                return None
            if payload.get("released_at") != item["released_at"].isoformat():
                return None
            age = time.time() - float(payload["stored_at"])
            if age < 0:
                return None
            outcome = payload.get("outcome")
            ttl = (
                self.positive_ttl_seconds
                if outcome == "success"
                else self.negative_ttl_seconds
            )
            if age > ttl:
                return None
            content_sha256 = str(payload.get("content_sha256", ""))
            if not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
                return None
            if outcome == "success":
                return "success", _observation_from_payload(
                    payload["observation"]
                )
            if outcome == "parse_failure" and payload.get("error"):
                return "parse_failure", str(payload["error"])
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            return None
        return None

    def write(
        self,
        item: dict,
        content_sha256: str,
        *,
        observation: CumulativeObservation = None,
        error: str = "",
    ) -> bool:
        path = self.path_for(item)
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "parser_version": self.parser_version,
            "stored_at": time.time(),
            "url": item["pdf_url"],
            "title": item["title"],
            "released_at": item["released_at"].isoformat(),
            "content_sha256": content_sha256,
            "outcome": (
                "success" if observation is not None else "parse_failure"
            ),
        }
        if observation is not None:
            payload["observation"] = _observation_payload(observation)
        else:
            payload["error"] = str(error)
        payload["record_sha256"] = _record_sha256(payload)

        temporary_path = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
            return True
        except OSError:
            return False
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass
