"""Shared hard limits for live portfolio state."""


MAX_HOLDINGS_PER_STRATEGY = 10


class HoldingLimitError(ValueError):
    """Raised before a portfolio mutation would exceed the hard limit."""


def ordered_unique_symbols(symbols):
    """Return symbols once, preserving the research ranking order."""
    result = []
    seen = set()
    for symbol in symbols:
        if symbol in seen:
            continue
        seen.add(symbol)
        result.append(symbol)
    return result


def validate_portfolio_holding_limits(portfolio, limit=MAX_HOLDINGS_PER_STRATEGY):
    """Fail before persistence when any strategy exceeds its live-position cap."""
    violations = {
        strategy: len(holdings)
        for strategy, holdings in portfolio.items()
        if len(holdings) > limit
    }
    if violations:
        details = ", ".join(
            f"{strategy}={count}" for strategy, count in sorted(violations.items())
        )
        raise HoldingLimitError(
            f"Portfolio holding limit exceeded (maximum {limit}): {details}"
        )
    return True
