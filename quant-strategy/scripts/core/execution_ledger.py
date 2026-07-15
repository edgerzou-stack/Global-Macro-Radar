"""Isolated v6 order, fill, and balanced-journal core.

The module owns the transaction boundary for each public mutation. It does not
read or update the legacy portfolio, cash-manager, or trade-history tables.
"""

import datetime as dt
import sqlite3
import uuid
from decimal import Decimal, InvalidOperation


ORDER_STATES = {
    "PENDING",
    "OPEN",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCELLED",
    "REJECTED",
    "EXPIRED",
}

CURRENCY_EXPONENTS = {
    "JPY": 0,
    "KRW": 0,
    "CNY": 2,
    "CNH": 2,
    "HKD": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
}


class ExecutionLedgerError(Exception):
    pass


class OrderNotFound(ExecutionLedgerError):
    pass


class InvalidOrderTransition(ExecutionLedgerError):
    pass


class IdempotencyConflict(ExecutionLedgerError):
    pass


class InsufficientReservedCash(ExecutionLedgerError):
    pass


class UnbalancedJournal(ExecutionLedgerError):
    pass


class ConcurrentOrderModification(ExecutionLedgerError):
    pass


def utc_now_text():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def to_minor_units(amount, currency):
    currency = str(currency).upper()
    exponent = CURRENCY_EXPONENTS.get(currency, 2)
    try:
        decimal_amount = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid monetary amount: {amount!r}") from exc
    if not decimal_amount.is_finite() or decimal_amount < 0:
        raise ValueError(f"Monetary amount must be finite and non-negative: {amount!r}")
    scaled = decimal_amount * (Decimal(10) ** exponent)
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise ValueError(
            f"{amount!r} has precision below the minimum unit for {currency}"
        )
    return int(integral)


class ExecutionLedger:
    def __init__(self, connection: sqlite3.Connection):
        self.conn = connection
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _validate_quantity(quantity):
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("quantity must be a positive integer")

    @staticmethod
    def _row_dict(row):
        return dict(row) if row is not None else None

    def get_order(self, order_id):
        row = self.conn.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise OrderNotFound(order_id)
        return self._row_dict(row)

    def _get_order_by_key(self, idempotency_key):
        return self.conn.execute(
            "SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()

    def create_order(
        self,
        *,
        idempotency_key,
        strategy_id,
        symbol,
        market,
        currency,
        side,
        quantity,
        limit_price=None,
        reserve_amount=Decimal("0"),
        order_id=None,
        created_at=None,
    ):
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        self._validate_quantity(quantity)
        currency = str(currency).upper()
        side = str(side).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        limit_price_minor = (
            to_minor_units(limit_price, currency) if limit_price is not None else None
        )
        if limit_price_minor == 0:
            raise ValueError("limit_price must be positive")
        reserve_minor = to_minor_units(reserve_amount, currency)
        if side == "BUY" and reserve_minor <= 0:
            raise ValueError("BUY orders require a positive reserve_amount")
        if side == "SELL" and reserve_minor != 0:
            raise ValueError("SELL orders cannot reserve cash")

        expected = {
            "strategy_id": strategy_id,
            "symbol": symbol,
            "market": market,
            "currency": currency,
            "side": side,
            "requested_quantity": quantity,
            "limit_price_minor": limit_price_minor,
            "initial_reserved_cash_minor": reserve_minor,
        }
        existing = self._get_order_by_key(idempotency_key)
        if existing is not None:
            if any(existing[key] != value for key, value in expected.items()):
                raise IdempotencyConflict(
                    f"Order idempotency key {idempotency_key!r} has a different payload"
                )
            return self._row_dict(existing)

        order_id = order_id or str(uuid.uuid4())
        timestamp = created_at or utc_now_text()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO orders (
                    order_id, idempotency_key, strategy_id, symbol, market,
                    currency, side, state, requested_quantity, filled_quantity,
                    limit_price_minor, initial_reserved_cash_minor,
                    reserved_cash_minor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    idempotency_key,
                    strategy_id,
                    symbol,
                    market,
                    currency,
                    side,
                    quantity,
                    limit_price_minor,
                    reserve_minor,
                    reserve_minor,
                    timestamp,
                    timestamp,
                ),
            )
            if reserve_minor:
                self._insert_journal(
                    transaction_id=str(uuid.uuid4()),
                    idempotency_key=f"reserve:{idempotency_key}",
                    event_type="ORDER_CASH_RESERVATION",
                    reference_id=order_id,
                    strategy_id=strategy_id,
                    entries=[
                        ("CASH_RESERVED", currency, reserve_minor, 0),
                        ("CASH_AVAILABLE", currency, 0, reserve_minor),
                    ],
                    created_at=timestamp,
                )
        return self.get_order(order_id)

    def open_order(self, order_id, opened_at=None):
        order = self.get_order(order_id)
        if order["state"] == "OPEN":
            return order
        if order["state"] != "PENDING":
            raise InvalidOrderTransition(
                f"Cannot open order {order_id} from {order['state']}"
            )
        with self.conn:
            result = self.conn.execute(
                """
                UPDATE orders SET state='OPEN', updated_at=?
                WHERE order_id=? AND state='PENDING'
                """,
                (opened_at or utc_now_text(), order_id),
            )
            if result.rowcount != 1:
                raise ConcurrentOrderModification(
                    f"Order {order_id} changed while it was being opened"
                )
        return self.get_order(order_id)

    def cancel_order(self, order_id, cancelled_at=None):
        order = self.get_order(order_id)
        if order["state"] == "CANCELLED":
            return order
        if order["state"] not in {"PENDING", "OPEN", "PARTIALLY_FILLED"}:
            raise InvalidOrderTransition(
                f"Cannot cancel order {order_id} from {order['state']}"
            )
        timestamp = cancelled_at or utc_now_text()
        with self.conn:
            remaining_reserve = order["reserved_cash_minor"]
            if remaining_reserve:
                self._insert_journal(
                    transaction_id=str(uuid.uuid4()),
                    idempotency_key=f"cancel:{order_id}",
                    event_type="ORDER_CANCEL_RELEASE",
                    reference_id=order_id,
                    strategy_id=order["strategy_id"],
                    entries=[
                        ("CASH_AVAILABLE", order["currency"], remaining_reserve, 0),
                        ("CASH_RESERVED", order["currency"], 0, remaining_reserve),
                    ],
                    created_at=timestamp,
                )
            result = self.conn.execute(
                """
                UPDATE orders
                SET state='CANCELLED', reserved_cash_minor=0, updated_at=?
                WHERE order_id=? AND state=? AND filled_quantity=?
                    AND reserved_cash_minor=?
                """,
                (
                    timestamp,
                    order_id,
                    order["state"],
                    order["filled_quantity"],
                    order["reserved_cash_minor"],
                ),
            )
            if result.rowcount != 1:
                raise ConcurrentOrderModification(
                    f"Order {order_id} changed while it was being cancelled"
                )
        return self.get_order(order_id)

    def record_fill(
        self,
        *,
        order_id,
        idempotency_key,
        quantity,
        price,
        fee=Decimal("0"),
        fill_id=None,
        executed_at=None,
        fault_injector=None,
    ):
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        self._validate_quantity(quantity)

        existing = self.conn.execute(
            "SELECT * FROM fills WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            order = self.get_order(order_id)
            price_minor = to_minor_units(price, order["currency"])
            fee_minor = to_minor_units(fee, order["currency"])
            expected = {
                "order_id": order_id,
                "quantity": quantity,
                "price_minor": price_minor,
                "fee_minor": fee_minor,
            }
            if any(existing[key] != value for key, value in expected.items()):
                raise IdempotencyConflict(
                    f"Fill idempotency key {idempotency_key!r} has a different payload"
                )
            return self._row_dict(existing)

        order = self.get_order(order_id)
        if order["state"] not in {"OPEN", "PARTIALLY_FILLED"}:
            raise InvalidOrderTransition(
                f"Cannot fill order {order_id} from {order['state']}"
            )
        remaining_quantity = order["requested_quantity"] - order["filled_quantity"]
        if quantity > remaining_quantity:
            raise ValueError(
                f"Fill quantity {quantity} exceeds remaining quantity {remaining_quantity}"
            )

        price_minor = to_minor_units(price, order["currency"])
        if price_minor <= 0:
            raise ValueError("fill price must be positive")
        fee_minor = to_minor_units(fee, order["currency"])
        gross_minor = price_minor * quantity
        fill_id = fill_id or str(uuid.uuid4())
        transaction_id = str(uuid.uuid4())
        timestamp = executed_at or utc_now_text()
        final_fill = quantity == remaining_quantity

        if order["side"] == "BUY":
            consumed = gross_minor + fee_minor
            if consumed > order["reserved_cash_minor"]:
                raise InsufficientReservedCash(
                    f"Fill requires {consumed} minor units but only "
                    f"{order['reserved_cash_minor']} are reserved"
                )
            release_minor = order["reserved_cash_minor"] - consumed if final_fill else 0
            entries = [("POSITION_COST", order["currency"], gross_minor, 0)]
            if fee_minor:
                entries.append(("TRANSACTION_FEES", order["currency"], fee_minor, 0))
            if release_minor:
                entries.append(("CASH_AVAILABLE", order["currency"], release_minor, 0))
            entries.append(
                (
                    "CASH_RESERVED",
                    order["currency"],
                    0,
                    consumed + release_minor,
                )
            )
            new_reserved = 0 if final_fill else order["reserved_cash_minor"] - consumed
        else:
            if fee_minor > gross_minor:
                raise ValueError("SELL fill fee cannot exceed gross proceeds")
            net_minor = gross_minor - fee_minor
            entries = []
            if net_minor:
                entries.append(("CASH_AVAILABLE", order["currency"], net_minor, 0))
            if fee_minor:
                entries.append(("TRANSACTION_FEES", order["currency"], fee_minor, 0))
            entries.append(("SECURITY_SALE_CLEARING", order["currency"], 0, gross_minor))
            new_reserved = 0

        with self.conn:
            self._insert_journal(
                transaction_id=transaction_id,
                idempotency_key=f"fill:{idempotency_key}",
                event_type="ORDER_FILL",
                reference_id=fill_id,
                strategy_id=order["strategy_id"],
                entries=entries,
                created_at=timestamp,
            )
            if fault_injector:
                fault_injector("after_journal")

            self.conn.execute(
                """
                INSERT INTO fills (
                    fill_id, order_id, idempotency_key, quantity, price_minor,
                    fee_minor, gross_minor, executed_at, transaction_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill_id,
                    order_id,
                    idempotency_key,
                    quantity,
                    price_minor,
                    fee_minor,
                    gross_minor,
                    timestamp,
                    transaction_id,
                ),
            )
            if fault_injector:
                fault_injector("after_fill")

            new_filled = order["filled_quantity"] + quantity
            new_state = "FILLED" if final_fill else "PARTIALLY_FILLED"
            result = self.conn.execute(
                """
                UPDATE orders
                SET state=?, filled_quantity=?, reserved_cash_minor=?, updated_at=?
                WHERE order_id=? AND state=? AND filled_quantity=?
                    AND reserved_cash_minor=?
                """,
                (
                    new_state,
                    new_filled,
                    new_reserved,
                    timestamp,
                    order_id,
                    order["state"],
                    order["filled_quantity"],
                    order["reserved_cash_minor"],
                ),
            )
            if result.rowcount != 1:
                raise ConcurrentOrderModification(
                    f"Order {order_id} changed while fill {fill_id} was recorded"
                )
            if fault_injector:
                fault_injector("after_order_update")

        return self._row_dict(
            self.conn.execute("SELECT * FROM fills WHERE fill_id=?", (fill_id,)).fetchone()
        )

    def _insert_journal(
        self,
        *,
        transaction_id,
        idempotency_key,
        event_type,
        reference_id,
        strategy_id,
        entries,
        created_at,
    ):
        balances = {}
        for account, currency, debit_minor, credit_minor in entries:
            currency = str(currency).upper()
            balances[currency] = balances.get(currency, 0) + debit_minor - credit_minor
        unbalanced = {currency: value for currency, value in balances.items() if value}
        if not entries or unbalanced:
            raise UnbalancedJournal(
                f"Journal {transaction_id} is not balanced by currency: {unbalanced}"
            )

        self.conn.execute(
            """
            INSERT INTO journal_transactions (
                transaction_id, idempotency_key, event_type, reference_id, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (transaction_id, idempotency_key, event_type, reference_id, created_at),
        )
        self.conn.executemany(
            """
            INSERT INTO journal_entries (
                transaction_id, line_no, strategy_id, account, currency,
                debit_minor, credit_minor
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    transaction_id,
                    line_no,
                    strategy_id,
                    account,
                    str(currency).upper(),
                    debit_minor,
                    credit_minor,
                )
                for line_no, (account, currency, debit_minor, credit_minor) in enumerate(
                    entries, start=1
                )
            ],
        )

    def assert_all_transactions_balanced(self):
        rows = self.conn.execute(
            """
            SELECT
                jt.transaction_id,
                je.currency,
                COUNT(je.entry_id) AS entry_count,
                COALESCE(SUM(je.debit_minor), 0) AS debits,
                COALESCE(SUM(je.credit_minor), 0) AS credits
            FROM journal_transactions jt
            LEFT JOIN journal_entries je
                ON je.transaction_id = jt.transaction_id
            GROUP BY jt.transaction_id, je.currency
            HAVING entry_count = 0 OR debits != credits
            """
        ).fetchall()
        if rows:
            raise UnbalancedJournal([dict(row) for row in rows])
        return True
