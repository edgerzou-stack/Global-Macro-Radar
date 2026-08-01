"""Stable Radar import path for the repository-wide provider error contract."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime_contracts.provider_errors import (  # noqa: E402,F401
    PROVIDER_ERROR_SCHEMA_VERSION,
    ProviderErrorContext,
    ProviderOperationError,
    log_provider_error,
    provider_error_context,
)
