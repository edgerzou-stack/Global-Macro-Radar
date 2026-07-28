import os
from datetime import date, datetime


def logical_today() -> date:
    """Return the pipeline business date without changing the physical clock."""
    configured = os.environ.get("PIPELINE_EFFECTIVE_DATE") or os.environ.get(
        "EFFECTIVE_DATE"
    )
    if configured:
        try:
            return date.fromisoformat(configured)
        except ValueError as exc:
            raise ValueError(
                f"Invalid pipeline effective date {configured!r}; expected YYYY-MM-DD"
            ) from exc
    return datetime.now().astimezone().date()


def logical_date_text() -> str:
    return logical_today().isoformat()
