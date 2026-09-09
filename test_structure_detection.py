"""
Real, runnable unit tests for structure_detection.py and
classification_rules.py -- pure functions, synthetic OHLC data, no MT5
dependency. Run with:  python3 -m unittest test_structure_detection.py -v
"""
import unittest

import numpy as np

import classification_rules as cr
import structure_detection as sd


def bars(closes, highs=None, lows=None, opens=None, start_time=1_700_000_000, step=900):
    """Builds parallel numpy arrays from a list of closes, defaulting
    open/high/low to the close (a flat candle) unless overridden -- keeps
    test setup terse when only the close sequence matters."""
    n = len(closes)
    closes = np.array(closes, dtype=float)
    opens = np.array(opens, dtype=float) if opens is not None else closes.copy()
    highs = np.array(highs, dtype=float) if highs is not None else np.maximum(opens, closes)
    lows = np.array(lows, dtype=float) if lows is not None else np.minimum(opens, closes)
    times = np.arange(start_time, start_time + n * step, step, dtype=np.int64)
    return times, opens, highs, lows, closes


class TestSwingPoints(unittest.TestCase):
    def test_finds_obvious_swing_high_and_low(self):
        # 0  1  2  3  4  5  6
        # 1, 2, 5, 2, 1, 0, 1  -> index 2 is a swing high, index 5 a swing low
        closes = [1, 2, 5, 2, 1, 0, 1]
        times, opens, highs, lows, _ = bars(closes)
        points = sd.find_swing_points(highs, lows, times, left=1, right=1)
        types_and_idx = [(p["type"], p["index"]) for p in points]
        self.assertIn(("HIGH", 2), types_and_idx)
        self.assertIn(("LOW", 5), types_and_idx)

    def test_no_false_positives_on_monotonic_series(self):
        closes = [1, 2, 3, 4, 5, 6, 7]
        times, opens, highs, lows, _ = bars(closes)
        points = sd.find_swing_points(highs, lows, times, left=1, right=1)
        self.assertEqual(points, [])


class TestTrendClassification(unittest.TestCase):
    def test_higher_highs_and_higher_lows_is_bullish(self):
        swing_points = [
            {"index": 0, "time": 0, "type": "LOW", "price": 1.0},
            {"index": 1, "time": 1, "type": "HIGH", "price": 2.0},
            {"index": 2, "time": 2, "type": "LOW", "price": 1.5},
            {"index": 3, "time": 3, "type": "HIGH", "price": 3.0},
        ]
        self.assertEqual(sd.classify_trend(swing_points), "BULLISH")

    def test_lower_highs_and_lower_lows_is_bearish(self):
        swing_points = [
            {"index": 0, "time": 0, "type": "HIGH", "price": 3.0},
            {"index": 1, "time": 1, "type": "LOW", "price": 2.0},
            {"index": 2, "time": 2, "type": "HIGH", "price": 2.5},
            {"index": 3, "time": 3, "type": "LOW", "price": 1.0},
        ]
        self.assertEqual(sd.classify_trend(swing_points), "BEARISH")

    def test_mixed_signals_is_neutral(self):
        swing_points = [
            {"index": 0, "time": 0, "type": "HIGH", "price": 2.0},
            {"index": 1, "time": 1, "type": "LOW", "price": 1.0},
            {"index": 2, "time": 2, "type": "HIGH", "price": 3.0},   # higher high
            {"index": 3, "time": 3, "type": "LOW", "price": 0.5},    # lower low too -> mixed
        ]
        self.assertEqual(sd.classify_trend(swing_points), "NEUTRAL")

    def test_insufficient_data_returns_none(self):
        self.assertIsNone(sd.classify_trend([{"index": 0, "time": 0, "type": "HIGH", "price": 1.0}]))


class TestStructureShifts(unittest.TestCase):
    def test_first_break_is_bos_and_flips_trend(self):
        # Rally that breaks a swing high for the first time: labeled BOS
        # (no established opposing trend to change character from).
        closes = [1, 3, 1.5, 2, 5]  # swing high at index 1 (price 3), broken at index 4
        times, opens, highs, lows, closes_arr = bars(closes)
        swing_points = sd.find_swing_points(highs, lows, times, left=1, right=1)
        events = sd.detect_structure_shifts(swing_points, closes_arr, times)
        bos_events = [e for e in events if e["type"] == "BOS"]
        self.assertTrue(any(e["index"] == 4 for e in bos_events))

    def test_reversal_after_established_trend_is_choch(self):
        # Establish a bullish break first (closes[4]=3.5 breaks the earlier
        # swing high of 3 at index 2), then break back down through the
        # swing low of 1.5 at index 3 -- that second break should be
        # CHoCH, not BOS, since a bullish trend was already established.
        closes = [2, 1, 3, 1.5, 3.5, 2, 0.5]
        times, opens, highs, lows, closes_arr = bars(closes)
        swing_points = sd.find_swing_points(highs, lows, times, left=1, right=1)
        events = sd.detect_structure_shifts(swing_points, closes_arr, times)
        self.assertTrue(any(e["type"] == "BOS" for e in events))
        self.assertTrue(any(e["type"] == "CHOCH" for e in events))


class TestFairValueGaps(unittest.TestCase):
    def test_detects_bullish_gap(self):
        # candle 0 high=1.0, candle 1 (impulsive), candle 2 low=1.5 -> gap [1.0, 1.5]
        opens = [1.0, 1.2, 1.6]
        highs = [1.0, 1.8, 1.9]
        lows = [0.9, 1.3, 1.5]
        closes = [0.95, 1.7, 1.55]
        times, opens_arr, highs_arr, lows_arr, closes_arr = bars(closes, highs=highs, lows=lows, opens=opens)
        gaps = sd.detect_fair_value_gaps(opens_arr, highs_arr, lows_arr, closes_arr, times)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["type"], "BULLISH_FVG")
        self.assertAlmostEqual(gaps[0]["bottom"], 1.0)
        self.assertAlmostEqual(gaps[0]["top"], 1.5)

    def test_no_gap_when_ranges_overlap(self):
        opens = [1.0, 1.1, 1.05]
        highs = [1.2, 1.3, 1.25]
        lows = [0.9, 1.0, 0.95]
        closes = [1.1, 1.2, 1.1]
        times, opens_arr, highs_arr, lows_arr, closes_arr = bars(closes, highs=highs, lows=lows, opens=opens)
        gaps = sd.detect_fair_value_gaps(opens_arr, highs_arr, lows_arr, closes_arr, times)
        self.assertEqual(gaps, [])


class TestLiquiditySweeps(unittest.TestCase):
    def test_wick_beyond_swing_high_that_closes_back_below_is_a_sweep(self):
        swing_points = [{"index": 1, "time": 900, "type": "HIGH", "price": 2.0}]
        highs = np.array([1.5, 2.0, 2.5, 1.8])
        lows = np.array([1.0, 1.5, 1.9, 1.4])
        closes = np.array([1.2, 1.9, 1.95, 1.5])  # index 2 wicks above 2.0 but closes at 1.95
        times = np.array([0, 900, 1800, 2700], dtype=np.int64)
        sweeps = sd.detect_liquidity_sweeps(highs, lows, closes, times, swing_points)
        self.assertEqual(len(sweeps), 1)
        self.assertEqual(sweeps[0]["type"], "SELL_SIDE_SWEEP")
        self.assertEqual(sweeps[0]["index"], 2)


class TestClassificationRules(unittest.TestCase):
    def test_entry_zone_type_prefers_ob_over_liquidity(self):
        structures = [
            {"type": "BULLISH_OB", "time": 100, "top": 1.05, "bottom": 1.0},
            {"type": "BUY_SIDE_SWEEP", "time": 200, "price": 1.02},
        ]
        result = cr.classify_entry_zone_type(structures, entry_price=1.02, entry_time=300, tolerance=0.01)
        self.assertEqual(result, "OB")

    def test_entry_zone_type_none_when_nothing_matches(self):
        structures = [{"type": "BULLISH_OB", "time": 100, "top": 5.0, "bottom": 4.9}]
        result = cr.classify_entry_zone_type(structures, entry_price=1.0, entry_time=300, tolerance=0.01)
        self.assertIsNone(result)

    def test_entry_confirmation_picks_most_recent_within_window(self):
        structures = [
            {"type": "BOS", "time": 100},
            {"type": "CHOCH", "time": 500},
        ]
        result = cr.classify_entry_confirmation(structures, entry_time=600, window_seconds=200)
        self.assertEqual(result, "CHOCH")

    def test_entry_confirmation_defaults_to_none_value_outside_window(self):
        structures = [{"type": "BOS", "time": 100}]
        result = cr.classify_entry_confirmation(structures, entry_time=10000, window_seconds=200)
        self.assertEqual(result, "NONE")

    def test_exit_reason_tp_hit(self):
        result = cr.classify_exit_reason(
            exit_price=1.105, sl_price=1.09, tp_price=1.105, structures=[],
            exit_time=1000, direction="LONG", price_tolerance=0.001, window_seconds=900,
        )
        self.assertEqual(result, "TP_HIT")

    def test_exit_reason_structure_break_when_no_price_target_hit(self):
        structures = [{"type": "CHOCH", "time": 950}]
        result = cr.classify_exit_reason(
            exit_price=1.10, sl_price=1.09, tp_price=1.20, structures=structures,
            exit_time=1000, direction="LONG", price_tolerance=0.001, window_seconds=100,
        )
        self.assertEqual(result, "STRUCTURE_BREAK")

    def test_exit_reason_falls_back_to_manual(self):
        result = cr.classify_exit_reason(
            exit_price=1.10, sl_price=1.09, tp_price=1.20, structures=[],
            exit_time=1000, direction="LONG", price_tolerance=0.001, window_seconds=100,
        )
        self.assertEqual(result, "MANUAL_EXIT")

    def test_sl_placement_reason_ob_boundary(self):
        structures = [{"type": "BEARISH_OB", "time": 100, "top": 1.10, "bottom": 1.08}]
        result = cr.classify_sl_placement_reason(structures, swing_points=[], sl_price=1.081, tolerance=0.005)
        self.assertEqual(result, "OB_BOUNDARY")

    def test_sl_placement_reason_previous_low_when_no_zone_nearby(self):
        swing_points = [{"index": 0, "time": 0, "type": "LOW", "price": 1.05}]
        result = cr.classify_sl_placement_reason([], swing_points, sl_price=1.051, tolerance=0.01)
        self.assertEqual(result, "PREVIOUS_LOW")

    def test_sl_placement_reason_none_when_nothing_nearby(self):
        result = cr.classify_sl_placement_reason([], [], sl_price=9.99, tolerance=0.001)
        self.assertIsNone(result)

    def test_tp_placement_reason_fvg_fill(self):
        structures = [{"type": "BULLISH_FVG", "time": 100, "top": 1.20, "bottom": 1.18}]
        result = cr.classify_tp_placement_reason(structures, tp_price=1.19, swing_points=[], tolerance=0.01)
        self.assertEqual(result, "FVG_FILL")


if __name__ == "__main__":
    unittest.main()
