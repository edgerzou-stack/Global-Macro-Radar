"""Canonical strategy lifecycle registry.

Retired strategies remain recognizable for historical audit/recovery, but no
active pipeline component may create new ledger state for them.
"""

ACTIVE_STRATEGIES = (
    "dividend_a_stock",
    "growth_a_stock",
    "growth_us_stock",
    "growth_hk_stock",
    "hot_spot_a_stock",
    "hot_spot_us_stock",
    "hot_spot_hk_stock",
)

RETIRED_STRATEGIES = frozenset(
    {
        "dividend_us_stock",
        "dividend_hk_stock",
    }
)


class RetiredStrategyError(ValueError):
    """Raised when a caller attempts to create new state for a retired strategy."""


def assert_strategy_not_retired(strategy_id):
    if strategy_id in RETIRED_STRATEGIES:
        raise RetiredStrategyError(
            f"Strategy {strategy_id!r} is retired and cannot create new ledger state"
        )
