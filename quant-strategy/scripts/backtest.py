"""Compatibility wrapper for the retired legacy backtest entry point.

The old implementation used current universe/fundamental data and summed stock
ROIs without cash or weights. Keeping two engines would make results ambiguous,
so this entry point delegates to the validated offline engine.
"""

from backtest_engine import main


if __name__ == "__main__":
    main()
