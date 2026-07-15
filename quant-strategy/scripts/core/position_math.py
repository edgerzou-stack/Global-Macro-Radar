class DataIntegrityError(Exception):
    """Raised when there is a critical discrepancy in data adjustment factors (e.g. Baostock dirty read)."""
    pass


def validate_hfq_integrity(hfq_yesterday: float, hfq_today: float, unadj_yesterday: float, unadj_today: float) -> None:
    """
    Cross-validates Adjusted (HFQ) prices against Unadjusted prices to detect dirty reads.

    If Baostock returns raw unadjusted data for today's HFQ request before it has processed
    today's adjustment factors, HFQ will exhibit a massive fake drop, while Unadj remains stable.

    Raises:
        DataIntegrityError if a dirty read or data corruption is detected.
    """
    if hfq_yesterday <= 0 or unadj_yesterday <= 0:
        return

    r_hfq = (hfq_today / hfq_yesterday) - 1.0
    r_unadj = (unadj_today / unadj_yesterday) - 1.0

    # If the difference between HFQ return and Unadjusted return is > 10%
    if abs(r_hfq - r_unadj) > 0.10:
        # If the actual physical market didn't experience an extreme event (e.g., within 15% move)
        # then the HFQ factor has collapsed/changed unexpectedly (dirty read)
        if abs(r_unadj) < 0.15:
            raise DataIntegrityError(
                f"Data anomaly detected: HFQ return {r_hfq:.2%} vs Unadj return {r_unadj:.2%}. "
                f"Factor corruption or dirty read suspected."
            )
        # If abs(r_unadj) is also massive (e.g., > 15%), it's a real ex-dividend day or
        # legitimate stock split (where Unadj drops wildly, but HFQ might be normal).
        # We don't raise an error here.


def calculate_harmonic_average_cost(old_shares: int, old_ep: float, new_shares: int, new_ep: float) -> float:
    """
    Calculates the new entry price using harmonic mean, reflecting fixed capital allocation per tranche.

    Args:
        old_shares: Number of tranches previously held
        old_ep: Average cost basis so far
        new_shares: Number of new tranches added
        new_ep: Price of the new tranche

    Returns:
        The updated average cost basis.
    """
    if old_ep <= 0 or new_ep <= 0:
        return old_ep if old_ep > 0 else new_ep

    total_shares = old_shares + new_shares
    # Harmonic mean formula: Total Shares / (Sum of (shares / price))
    new_avg_cost = total_shares / ((old_shares / old_ep) + (new_shares / new_ep))
    return new_avg_cost


def calculate_true_pnl(entry_price: float, exit_price: float, fee: float) -> float:
    """
    Calculates the percentage PnL.

    Args:
        entry_price: Adjusted entry price
        exit_price: Adjusted exit price
        fee: Trading fee percentage (e.g., 0.001)

    Returns:
        Percentage profit/loss.
    """
    if entry_price <= 0:
        return 0.0
    return (exit_price / entry_price - 1.0) - fee
