"""Retired unsafe price-repair entry point.

Execution prices are immutable raw market prices.  Replacing them with
adjusted closes corrupts cash, PnL, averaging, and stop-loss decisions.  The
historical implementation remains available in Git history; keeping its body
importable in production would make accidental execution too easy.
"""


DISABLED_MESSAGE = (
    "repair_db_prices.py is permanently disabled: it overwrote raw "
    "execution-price columns with adjusted closes and could generate false "
    "averaging/stop-loss events. Use rebuild_dividend_ledger.py with an "
    "audited event manifest instead."
)


def repair() -> None:
    raise RuntimeError(DISABLED_MESSAGE)


if __name__ == "__main__":
    repair()
