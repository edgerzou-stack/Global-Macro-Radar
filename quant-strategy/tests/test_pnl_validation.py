import unittest
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(ROOT, "scripts"))

from core.data_gateway import DataAnomalyError, DataGateway

class TestPnLValidation(unittest.TestCase):
    def test_extreme_a_share_move_blocked(self):
        """
        Tests that an impossible 1-day move for an A-share (e.g. -92%) 
        is correctly flagged as a data anomaly (preventing corrupt data from entering the backtest).
        """
        gateway = DataGateway()
        
        # Scenario: entry is 113.83 (hfq), exit is 8.42 (qfq) one day later.
        # This yields a -92% return, which is mathematically impossible for A-shares in 1 day.
        
        # Test a valid move
        is_valid = gateway.verify_extreme_move("002003", 1, 113.83, 114.50)
        self.assertTrue(is_valid)
        
        # Test an invalid drop
        with self.assertRaises(DataAnomalyError):
            gateway.verify_extreme_move("002003", 1, 113.83, 8.42)
            
        # Test an invalid spike
        with self.assertRaises(DataAnomalyError):
            gateway.verify_extreme_move("002287", 3, 18.84, 33.95)

if __name__ == "__main__":
    unittest.main()
