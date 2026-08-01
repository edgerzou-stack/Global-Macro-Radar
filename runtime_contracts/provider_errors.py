import json
from dataclasses import asdict, dataclass


PROVIDER_ERROR_SCHEMA_VERSION = 1
_MAX_TEXT_LENGTH = 500


def _bounded(value):
    text = str(value or "").replace("\n", " ").strip()
    return text[:_MAX_TEXT_LENGTH]


@dataclass(frozen=True)
class ProviderErrorContext:
    provider: str
    operation: str
    exception_type: str
    retryable: bool
    degraded_allowed: bool
    symbol: str = ""
    effective_date: str = ""
    detail: str = ""
    schema_version: int = PROVIDER_ERROR_SCHEMA_VERSION

    def __post_init__(self):
        for name in ("provider", "operation", "exception_type"):
            if not _bounded(getattr(self, name)):
                raise ValueError(f"{name} must be non-empty")

    def to_dict(self):
        payload = asdict(self)
        for name in (
            "provider",
            "operation",
            "exception_type",
            "symbol",
            "effective_date",
            "detail",
        ):
            payload[name] = _bounded(payload[name])
        return payload

    def to_json(self):
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


class ProviderOperationError(RuntimeError):
    """A provider failure whose operational semantics are machine-readable."""

    def __init__(self, context, *, cause=None):
        if not isinstance(context, ProviderErrorContext):
            raise TypeError("context must be ProviderErrorContext")
        self.context = context
        super().__init__(
            f"{context.provider}/{context.operation} failed: {context.detail}"
        )
        if cause is not None:
            self.__cause__ = cause


def provider_error_context(
    error,
    *,
    provider,
    operation,
    retryable,
    degraded_allowed,
    symbol="",
    effective_date="",
):
    if isinstance(error, ProviderOperationError):
        return error.context
    return ProviderErrorContext(
        provider=provider,
        operation=operation,
        exception_type=type(error).__name__,
        retryable=bool(retryable),
        degraded_allowed=bool(degraded_allowed),
        symbol=symbol,
        effective_date=effective_date,
        detail=str(error),
    )


def log_provider_error(
    logger,
    error,
    *,
    provider,
    operation,
    retryable,
    degraded_allowed,
    symbol="",
    effective_date="",
    level="warning",
):
    context = provider_error_context(
        error,
        provider=provider,
        operation=operation,
        retryable=retryable,
        degraded_allowed=degraded_allowed,
        symbol=symbol,
        effective_date=effective_date,
    )
    log = getattr(logger, level)
    log("PROVIDER_ERROR %s", context.to_json())
    return context
