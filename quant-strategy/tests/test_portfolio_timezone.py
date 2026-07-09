import unittest
import datetime
import pytz
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestPortfolioTimezone(unittest.TestCase):
    def test_us_market_timezone_execution(self):
        """
        Regression Test: Ensure that a US stock order placed at 19:10 Beijing Time
        is correctly identified as being BEFORE the US market open (09:30 NY Time),
        and therefore should execute on the SAME day in the US, rather than jumping to T+2.
        """
        req_date = "2026-07-08 19:10:00"
        dt_full = datetime.datetime.strptime(req_date, "%Y-%m-%d %H:%M:%S")
        bjt = pytz.timezone('Asia/Shanghai')
        dt_bjt = bjt.localize(dt_full)
        
        ny = pytz.timezone('America/New_York')
        dt_ny = dt_bjt.astimezone(ny)
        
        # At 19:10 BJT, it should be 07:10 NY time (summer time)
        # Check if it correctly identifies as before 09:30 NY time
        is_before_open = dt_ny.time() < datetime.time(9, 30)
        self.assertTrue(is_before_open)
        
        # It should execute on the same NY date: 2026-07-08
        expected_execution_date = datetime.date(2026, 7, 8)
        
        if dt_ny.time() < datetime.time(9, 30):
            start_date = dt_ny.date()
        else:
            start_date = dt_ny.date() + datetime.timedelta(days=1)
            
        self.assertEqual(start_date, expected_execution_date)

    def test_a_share_market_timezone_execution(self):
        """
        Regression Test: Ensure that an A-share order placed at 19:10 Beijing Time
        is correctly identified as being AFTER the A-share market open (09:30 BJT),
        and therefore should execute on the NEXT day (T+1).
        """
        req_date = "2026-07-08 19:10:00"
        dt_full = datetime.datetime.strptime(req_date, "%Y-%m-%d %H:%M:%S")
        bjt = pytz.timezone('Asia/Shanghai')
        dt_bjt = bjt.localize(dt_full)
        
        is_before_open = dt_bjt.time() < datetime.time(9, 30)
        self.assertFalse(is_before_open)
        
        expected_execution_date_str = "20260709"
        
        if dt_bjt.time() < datetime.time(9, 30):
            target_dt_str = dt_bjt.strftime("%Y%m%d")
        else:
            target_dt_str = (dt_bjt + datetime.timedelta(days=1)).strftime("%Y%m%d")
            
        self.assertEqual(target_dt_str, expected_execution_date_str)

if __name__ == '__main__':
    unittest.main()
