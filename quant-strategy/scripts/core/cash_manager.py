import sqlite3
import os
import threading

def get_db_path():
    base_dir = os.environ.get("PROJECT_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    return os.path.join(base_dir, "quant_system.db")

class CashManager:
    """
    Manages isolated cash accounts for each strategy.
    Implements the Sandbox Benchmark Engine.
    """
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(CashManager, cls).__new__(cls)
                cls._instance.INITIAL_CAPITAL = 1000000.0  # 1 Million base
                cls._instance.TRANCHE_RATIO = 0.033        # 3.3% per tranche
        return cls._instance

    def get_connection(self):
        return sqlite3.connect(get_db_path(), timeout=30.0)

    def initialize_strategy(self, strategy_id: str, cursor=None):
        """Inject 1M initial capital if strategy account does not exist."""
        c = cursor
        conn = None
        if c is None:
            conn = self.get_connection()
            c = conn.cursor()
            
        try:
            c.execute("SELECT total_capital FROM strategy_accounts WHERE strategy_id = ?", (strategy_id,))
            row = c.fetchone()
            if not row:
                c.execute(
                    "INSERT INTO strategy_accounts (strategy_id, total_capital, available_cash) VALUES (?, ?, ?)",
                    (strategy_id, self.INITIAL_CAPITAL, self.INITIAL_CAPITAL)
                )
                if conn:
                    conn.commit()
                print(f"[CashManager] Initialized isolated sandbox for '{strategy_id}' with {self.INITIAL_CAPITAL:,.2f}")
        finally:
            if conn:
                conn.close()

    def get_balance(self, strategy_id: str, cursor=None):
        self.initialize_strategy(strategy_id, cursor=cursor)
        if cursor is not None:
            cursor.execute("SELECT total_capital, available_cash FROM strategy_accounts WHERE strategy_id = ?", (strategy_id,))
            row = cursor.fetchone()
            return row[0], row[1]
            
        conn = self.get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT total_capital, available_cash FROM strategy_accounts WHERE strategy_id = ?", (strategy_id,))
            row = c.fetchone()
            return row[0], row[1]
        finally:
            conn.close()

    def get_tranche_size(self, strategy_id: str, cursor=None) -> float:
        total, _ = self.get_balance(strategy_id, cursor=cursor)
        return total * self.TRANCHE_RATIO

    def allocate(self, strategy_id: str, cursor=None) -> bool:
        """
        Attempt to allocate 1 tranche of capital.
        Returns True if successful, False if insufficient funds.
        If 'cursor' is provided, runs inside that external transaction (no auto-commit).
        """
        total, available = self.get_balance(strategy_id, cursor=cursor)
        tranche = total * self.TRANCHE_RATIO
        
        if available >= tranche:
            c = cursor
            conn = None
            if c is None:
                conn = self.get_connection()
                c = conn.cursor()
                
            try:
                c.execute(
                    "UPDATE strategy_accounts SET available_cash = available_cash - ? WHERE strategy_id = ?",
                    (tranche, strategy_id)
                )
                if conn:
                    conn.commit()
                print(f"[CashManager] Allocated {tranche:,.2f} for '{strategy_id}'. Remaining cash: {available - tranche:,.2f}")
                return True
            finally:
                if conn:
                    conn.close()
        else:
            print(f"[CashManager] WARNING: Insufficient funds for '{strategy_id}'. Need {tranche:,.2f}, have {available:,.2f}.")
            return False

    def release(self, strategy_id: str, tranches_held: int, pnl_pct: float, cursor=None):
        '''
        Release capital back to the pool after a position is closed.
        pnl_pct is e.g. 0.10 for +10% or -0.25 for -25%.
        If cursor is provided, runs inside that external transaction (no auto-commit).
        '''
        total, _ = self.get_balance(strategy_id, cursor=cursor)
        tranche = total * self.TRANCHE_RATIO
        invested_capital = tranche * tranches_held
        returned_capital = invested_capital * (1 + pnl_pct)
        
        c = cursor
        conn = None
        if c is None:
            conn = self.get_connection()
            c = conn.cursor()
            
        try:
            c.execute(
                "UPDATE strategy_accounts SET available_cash = available_cash + ? WHERE strategy_id = ?",
                (returned_capital, strategy_id)
            )
            # Update total capital (NAV logic simplified, full NAV calculated separately)
            c.execute(
                "UPDATE strategy_accounts SET total_capital = total_capital + ? WHERE strategy_id = ?",
                (invested_capital * pnl_pct, strategy_id)
            )
            if conn:
                conn.commit()
            print(f"[CashManager] Released {returned_capital:,.2f} (PnL: {pnl_pct*100:,.2f}%) to '{strategy_id}'.")
        finally:
            if conn:
                conn.close()
