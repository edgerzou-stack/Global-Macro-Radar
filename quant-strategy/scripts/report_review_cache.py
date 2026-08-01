"""Content-addressed cache for optional report LLM reviews."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import time
from pathlib import Path


CACHE_SCHEMA_VERSION = 1
CACHE_TTL_SECONDS = 30 * 24 * 60 * 60


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _record_sha256(payload: dict) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                key: value
                for key, value in payload.items()
                if key != "record_sha256"
            }
        ).encode("utf-8")
    ).hexdigest()


class ReportReviewCache:
    def __init__(self, *, ttl_seconds: float = CACHE_TTL_SECONDS):
        self.ttl_seconds = float(ttl_seconds)

    @staticmethod
    def _cache_dir() -> Path:
        default = Path(__file__).resolve().parents[1] / ".cache" / "report-reviews"
        return Path(
            os.environ.get("REPORT_REVIEW_CACHE_DIR", default)
        ).expanduser()

    @staticmethod
    def _identity(input_payload, prompt_version, provider, model) -> dict:
        return {
            "input_sha256": hashlib.sha256(
                _canonical_json(input_payload).encode("utf-8")
            ).hexdigest(),
            "prompt_version": str(prompt_version),
            "provider": str(provider),
            "model": str(model),
        }

    def path_for(self, input_payload, prompt_version, provider, model) -> Path:
        identity = self._identity(
            input_payload, prompt_version, provider, model
        )
        digest = hashlib.sha256(
            _canonical_json(identity).encode("utf-8")
        ).hexdigest()
        return self._cache_dir() / f"{digest}.json"

    def read(self, input_payload, prompt_version, provider, model):
        path = self.path_for(input_payload, prompt_version, provider, model)
        expected_identity = self._identity(
            input_payload, prompt_version, provider, model
        )
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
            if payload.get("identity") != expected_identity:
                return None
            age = time.time() - float(payload["stored_at"])
            if age < 0 or age > self.ttl_seconds:
                return None
            response = payload.get("response")
            if (
                not isinstance(response, dict)
                or not isinstance(response.get("strategy_reviews"), dict)
            ):
                return None
            return json.loads(_canonical_json(response))
        except (
            KeyError,
            TypeError,
            ValueError,
            OSError,
            json.JSONDecodeError,
        ):
            return None

    def write(
        self,
        input_payload,
        prompt_version,
        provider,
        model,
        response,
    ) -> bool:
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("strategy_reviews"), dict)
        ):
            return False
        path = self.path_for(input_payload, prompt_version, provider, model)
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "stored_at": time.time(),
            "identity": self._identity(
                input_payload, prompt_version, provider, model
            ),
            "response": response,
        }
        try:
            payload["record_sha256"] = _record_sha256(payload)
        except (TypeError, ValueError):
            return False

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
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
            return True
        except (OSError, TypeError, ValueError):
            return False
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass
