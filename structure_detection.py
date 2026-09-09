"""
Deterministic, rule-based price-structure detection (SMC/ICT-style):
swing highs/lows, break of structure (BOS), change of character (CHoCH),
order blocks (OB), fair value gaps (FVG), and liquidity sweeps.

Per the implementation plan: these are geometric patterns on OHLC data, not
judgment calls, so they're detected here with plain arithmetic -- no LLM
involved. The Java backend's AiSuggestionService later reasons *on top of*
this output for the handful of fields that genuinely require weighing
multiple signals together (see classification_rules.py for the fields that
ARE derived deterministically from these structures, vs. AiSuggestionService
in tm-server for the ones that aren't).

All functions here are pure and take plain numpy arrays (not MT5's
structured dtype directly) so they're trivially unit-testable with
synthetic data -- see test_structure_detection.py, which actually runs in
any Python environment (no MT5 install needed, unlike the rest of this
importer).

Everything is indexed against the same `times`/`opens`/`highs`/`lows`/
`closes` arrays passed in (typically M15 bars covering a lookback window
before a trade's entry through its exit).
"""
from __future__ import annotations

from typing import Optional, TypedDict

import numpy as np


class SwingPoint(TypedDict):
    index: int
    time: int
    type: str  # "HIGH" or "LOW"
    price: float


class StructureEvent(TypedDict, total=False):
    type: str
    index: int
    time: int
    price: float
    top: float
    bottom: float


def find_swing_points(highs: np.ndarray, lows: np.ndarray, times: np.ndarray,
                       left: int = 2, right: int = 2) -> list[SwingPoint]:
    """Fractal swing points: bar i is a swing high if its high is strictly
    greater than the `left` bars before it and the `right` bars after it
    (swing low is the mirror image on lows). This is the standard fractal
    definition used across SMC/ICT tooling -- simple, deterministic, and
    doesn't repaint once `right` bars have printed."""
    n = len(highs)
    points: list[SwingPoint] = []
    for i in range(left, n - right):
        window_highs_before = highs[i - left:i]
        window_highs_after = highs[i + 1:i + 1 + right]
        if highs[i] > window_highs_before.max() and highs[i] > window_highs_after.max():
            points.append({"index": i, "time": int(times[i]), "type": "HIGH", "price": float(highs[i])})
            continue  # a bar can't be both a swing high and low in this simple scheme

        window_lows_before = lows[i - left:i]
        window_lows_after = lows[i + 1:i + 1 + right]
        if lows[i] < window_lows_before.min() and lows[i] < window_lows_after.min():
            points.append({"index": i, "time": int(times[i]), "type": "LOW", "price": float(lows[i])})

    return points


def classify_trend(swing_points: list[SwingPoint]) -> Optional[str]:
    """BULLISH if the most recent swing high/low pair both made higher
    highs and higher lows vs. the pair before them; BEARISH for lower
    highs/lower lows; NEUTRAL otherwise (mixed signals -- e.g. a higher
    high but a lower low, which is expansion/indecision, not a clean
    trend). Returns None if there aren't enough swing points yet."""
    highs = [p for p in swing_points if p["type"] == "HIGH"]
    lows = [p for p in swing_points if p["type"] == "LOW"]
    if len(highs) < 2 or len(lows) < 2:
        return None

    higher_high = highs[-1]["price"] > highs[-2]["price"]
    higher_low = lows[-1]["price"] > lows[-2]["price"]

    if higher_high and higher_low:
        return "BULLISH"
    if not higher_high and not higher_low:
        return "BEARISH"
    return "NEUTRAL"


def detect_structure_shifts(swing_points: list[SwingPoint], closes: np.ndarray,
                             times: np.ndarray) -> list[StructureEvent]:
    """Walks forward through swing points tracking a running trend state.
    When a close breaks beyond the most recent relevant swing point:
      - breaking WITH the current trend  -> BOS (break of structure)
      - breaking AGAINST the current trend -> CHoCH (change of character),
        and the running trend flips.

    Trend state starts as None (undetermined) until the first break settles
    it. This deliberately only looks at *closes* beyond a swing level (not
    wicks) -- a close-based break is the more conservative, standard
    definition and avoids over-firing on noise.
    """
    events: list[StructureEvent] = []
    trend: Optional[str] = None
    last_swing_high: Optional[SwingPoint] = None
    last_swing_low: Optional[SwingPoint] = None

    # Merge swing points and bar-closes into one chronological walk: for each
    # bar after a swing point is confirmed, check whether its close broke it.
    sorted_swings = sorted(swing_points, key=lambda p: p["index"])
    swing_cursor = 0
    n = len(closes)

    for i in range(n):
        # Bring any swing points confirmed strictly before this bar into
        # view -- a swing point can't act as resistance/support against the
        # very bar that forms it (and using "<=" would let a bar's own
        # freshly-set swing price silently shadow the older level it may
        # also have broken, since it'd tie against itself and never count
        # as a break).
        while swing_cursor < len(sorted_swings) and sorted_swings[swing_cursor]["index"] < i:
            sp = sorted_swings[swing_cursor]
            if sp["type"] == "HIGH":
                last_swing_high = sp
            else:
                last_swing_low = sp
            swing_cursor += 1

        if last_swing_high is not None and closes[i] > last_swing_high["price"]:
            is_continuation = trend == "BULLISH"
            events.append({
                "type": "BOS" if is_continuation or trend is None else "CHOCH",
                "index": i, "time": int(times[i]), "price": float(closes[i]),
            })
            trend = "BULLISH"
            last_swing_high = None  # consumed -- don't keep re-triggering on the same level

        elif last_swing_low is not None and closes[i] < last_swing_low["price"]:
            is_continuation = trend == "BEARISH"
            events.append({
                "type": "BOS" if is_continuation or trend is None else "CHOCH",
                "index": i, "time": int(times[i]), "price": float(closes[i]),
            })
            trend = "BEARISH"
            last_swing_low = None

    return events


def detect_order_blocks(opens: np.ndarray, closes: np.ndarray,
                         structure_shifts: list[StructureEvent]) -> list[StructureEvent]:
    """For each BOS/CHoCH event, the order block is the last candle of the
    OPPOSITE color before the breaking candle -- the classic "last down-close
    candle before an up-break" (bullish OB) / "last up-close candle before a
    down-break" (bearish OB) definition. Its body (open-close range) defines
    the zone."""
    blocks: list[StructureEvent] = []
    for event in structure_shifts:
        break_index = event["index"]
        # Infer break direction from whether the breaking candle itself
        # closed above or below its own open.
        rising = closes[break_index] >= opens[break_index]

        j = break_index - 1
        if rising:
            while j >= 0 and closes[j] >= opens[j]:  # skip up-candles
                j -= 1
        else:
            while j >= 0 and closes[j] <= opens[j]:  # skip down-candles
                j -= 1

        if j < 0:
            continue

        top = float(max(opens[j], closes[j]))
        bottom = float(min(opens[j], closes[j]))
        blocks.append({
            "type": "BULLISH_OB" if rising else "BEARISH_OB",
            "index": j, "time": event.get("time"), "top": top, "bottom": bottom,
        })

    return blocks


def detect_fair_value_gaps(opens: np.ndarray, highs: np.ndarray, lows: np.ndarray,
                            closes: np.ndarray, times: np.ndarray) -> list[StructureEvent]:
    """Classic 3-candle FVG: a gap between candle i-1's high/low and candle
    i+1's low/high that candle i's range didn't fill. Bullish FVG when
    highs[i-1] < lows[i+1] (upside gap); bearish FVG when lows[i-1] >
    highs[i+1] (downside gap)."""
    gaps: list[StructureEvent] = []
    n = len(closes)
    for i in range(1, n - 1):
        if highs[i - 1] < lows[i + 1]:
            gaps.append({
                "type": "BULLISH_FVG", "index": i, "time": int(times[i]),
                "top": float(lows[i + 1]), "bottom": float(highs[i - 1]),
            })
        elif lows[i - 1] > highs[i + 1]:
            gaps.append({
                "type": "BEARISH_FVG", "index": i, "time": int(times[i]),
                "top": float(lows[i - 1]), "bottom": float(highs[i + 1]),
            })
    return gaps


def detect_liquidity_sweeps(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                             times: np.ndarray, swing_points: list[SwingPoint]) -> list[StructureEvent]:
    """A liquidity sweep ("stop hunt"): a bar wicks beyond a prior swing
    high/low but CLOSES back on the other side of it -- the wick took out
    resting stops without a genuine breakout. Only checks each swing point
    against bars strictly after it (can't sweep a level that hasn't formed
    yet)."""
    sweeps: list[StructureEvent] = []
    for sp in swing_points:
        for i in range(sp["index"] + 1, len(closes)):
            if sp["type"] == "HIGH" and highs[i] > sp["price"] and closes[i] < sp["price"]:
                sweeps.append({"type": "SELL_SIDE_SWEEP", "index": i, "time": int(times[i]), "price": float(sp["price"])})
                break  # one sweep per level is enough signal
            if sp["type"] == "LOW" and lows[i] < sp["price"] and closes[i] > sp["price"]:
                sweeps.append({"type": "BUY_SIDE_SWEEP", "index": i, "time": int(times[i]), "price": float(sp["price"])})
                break
    return sweeps


def detect_structures(times: np.ndarray, opens: np.ndarray, highs: np.ndarray,
                       lows: np.ndarray, closes: np.ndarray) -> list[StructureEvent]:
    """Top-level orchestrator: runs every detector above and returns one
    chronological list of structure events, each carrying at least
    `type`, `time`, and either `price` (point events: BOS/CHOCH/sweeps) or
    `top`/`bottom` (zone events: order blocks/FVGs)."""
    swing_points = find_swing_points(highs, lows, times)
    shifts = detect_structure_shifts(swing_points, closes, times)
    order_blocks = detect_order_blocks(opens, closes, shifts)
    fvgs = detect_fair_value_gaps(opens, highs, lows, closes, times)
    sweeps = detect_liquidity_sweeps(highs, lows, closes, times, swing_points)

    all_events = [*shifts, *order_blocks, *fvgs, *sweeps]
    all_events.sort(key=lambda e: e.get("time") or 0)
    return all_events
