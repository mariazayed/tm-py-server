"""
Deterministic derivation of the trade-analysis enum fields that are direct
geometric consequences of the structures detected in structure_detection.py
-- proximity of a price to a zone, or the nearest preceding event type, is
plain arithmetic, not a judgment call, so none of this touches an LLM.

Fields intentionally NOT handled here (left for the server-side
AiSuggestionService, which genuinely has to weigh multiple signals into one
holistic label): entryReason, marketState, invalidationCondition,
probability. See tm-server's AiSuggestionService for those.

Every function returns a plain string matching a Java enum constant name
(e.g. "BOS", "OB_BOUNDARY") or None when nothing matched -- None is a
legitimate, common result (most trades won't have every field determinable
from structure alone), left null for a human or the AI-suggestion pass to
fill in later.
"""
from __future__ import annotations

from typing import Optional

# Tiny slack added to every tolerance comparison below so that a price sitting
# exactly at a tolerance boundary (a common outcome with real floating-point
# prices, e.g. 1.19 - 1.18 landing on 0.010000000000000009 instead of exactly
# 0.01) doesn't get spuriously excluded by a strict/borderline comparison.
_EPSILON = 1e-9


def _zone_contains(event: dict, price: float, tolerance: float) -> bool:
    top = event.get("top")
    bottom = event.get("bottom")
    if top is None or bottom is None:
        return False
    return (bottom - tolerance - _EPSILON) <= price <= (top + tolerance + _EPSILON)


def classify_entry_zone_type(structures: list[dict], entry_price: float, entry_time: int,
                              tolerance: float) -> Optional[str]:
    """OB or FVG if the entry price sits inside a zone that existed before
    entry; else LIQUIDITY if entry is near a swept level; else
    SUPPORT_RESISTANCE if near any other structural price point. Checked in
    this order because OB/FVG are the more specific, higher-conviction
    zones in SMC/ICT terms -- a generic "near some level" match shouldn't
    outrank a precise zone match.

    ("SK_LEVELS" is a valid value in the Java enum but its intended meaning
    isn't documented anywhere in the codebase, so it's deliberately never
    emitted here -- left for manual selection if that's ever what's meant.)
    """
    candidates = [e for e in structures if (e.get("time") or 0) <= entry_time]

    for event in candidates:
        if event["type"] in ("BULLISH_OB", "BEARISH_OB") and _zone_contains(event, entry_price, tolerance):
            return "OB"
    for event in candidates:
        if event["type"] in ("BULLISH_FVG", "BEARISH_FVG") and _zone_contains(event, entry_price, tolerance):
            return "FVG"
    for event in candidates:
        if event["type"] in ("BUY_SIDE_SWEEP", "SELL_SIDE_SWEEP") and abs(event.get("price", 1e18) - entry_price) <= tolerance + _EPSILON:
            return "LIQUIDITY"
    for event in candidates:
        if event["type"] in ("BOS", "CHOCH") and abs(event.get("price", 1e18) - entry_price) <= tolerance + _EPSILON:
            return "SUPPORT_RESISTANCE"
    return None


def classify_entry_confirmation(structures: list[dict], entry_time: int,
                                 window_seconds: int) -> str:
    """The most recent structural event within `window_seconds` before
    entry, mapped directly to its matching EntryConfirmationEnum constant.
    Defaults to NONE (a real enum value, not null) when nothing fired in
    the window -- absence of a detected confirmation is itself meaningful
    information, distinct from "we don't know" (null)."""
    recent = [e for e in structures if 0 <= entry_time - (e.get("time") or -1) <= window_seconds]
    if not recent:
        return "NONE"

    recent.sort(key=lambda e: e.get("time") or 0, reverse=True)
    type_to_confirmation = {
        "BOS": "BOS",
        "CHOCH": "CHOCH",
        "BUY_SIDE_SWEEP": "LIQUIDITY_SWEEP",
        "SELL_SIDE_SWEEP": "LIQUIDITY_SWEEP",
    }
    for event in recent:
        confirmation = type_to_confirmation.get(event["type"])
        if confirmation:
            return confirmation
    return "NONE"


def classify_exit_reason(exit_price: float, sl_price: Optional[float], tp_price: Optional[float],
                          structures: list[dict], exit_time: int, direction: str,
                          price_tolerance: float, window_seconds: int) -> str:
    """TP_HIT / SL_HIT by direct price-tolerance match against the trade's
    own tp/sl (the most reliable signal available); STRUCTURE_BREAK if a
    CHoCH against the trade's direction fired shortly before exit and
    neither price target was hit; MANUAL_EXIT as the residual default (the
    honest label for "closed for some other reason we can't infer from
    price alone" -- most likely candidate for a real manual close)."""
    if tp_price and abs(exit_price - tp_price) <= price_tolerance + _EPSILON:
        return "TP_HIT"
    if sl_price and abs(exit_price - sl_price) <= price_tolerance + _EPSILON:
        return "SL_HIT"

    adverse_choch = "CHOCH"
    recent = [e for e in structures if e["type"] == adverse_choch
              and 0 <= exit_time - (e.get("time") or -1) <= window_seconds]
    if recent:
        return "STRUCTURE_BREAK"

    return "MANUAL_EXIT"


def _nearest_zone_reason(structures: list[dict], price: float, tolerance: float,
                          zone_type_to_reason: dict[str, str]) -> Optional[str]:
    best_reason = None
    best_distance = tolerance
    for event in structures:
        reason = zone_type_to_reason.get(event["type"])
        if reason is None:
            continue
        if "top" in event and "bottom" in event:
            if _zone_contains(event, price, tolerance):
                distance = min(abs(price - event["top"]), abs(price - event["bottom"]))
            else:
                continue
        else:
            distance = abs(event.get("price", 1e18) - price)
            if distance > tolerance + _EPSILON:
                continue
        if distance <= best_distance + _EPSILON:
            best_distance = distance
            best_reason = reason
    return best_reason


def classify_sl_placement_reason(structures: list[dict], swing_points: list[dict],
                                  sl_price: Optional[float], tolerance: float) -> Optional[str]:
    """OB_BOUNDARY / FVG_BOUNDARY if the stop sits right at a detected
    zone's edge (checked first -- a precise zone match outranks a generic
    swing match); else PREVIOUS_HIGH / PREVIOUS_LOW if it sits at a swing
    point. Returns None (not a guessed VOLATILITY_BASED) when nothing
    structural is nearby -- "we don't know, ask a human or the AI pass" is
    more honest than defaulting to a label we can't actually support from
    price action alone."""
    if sl_price is None:
        return None

    reason = _nearest_zone_reason(structures, sl_price, tolerance, {
        "BULLISH_OB": "OB_BOUNDARY", "BEARISH_OB": "OB_BOUNDARY",
        "BULLISH_FVG": "FVG_BOUNDARY", "BEARISH_FVG": "FVG_BOUNDARY",
    })
    if reason:
        return reason

    best_type = None
    best_distance = tolerance
    for sp in swing_points:
        distance = abs(sp["price"] - sl_price)
        if distance <= best_distance + _EPSILON:
            best_distance = distance
            best_type = "PREVIOUS_HIGH" if sp["type"] == "HIGH" else "PREVIOUS_LOW"
    return best_type


def classify_tp_placement_reason(structures: list[dict], tp_price: Optional[float],
                                  swing_points: list[dict], tolerance: float) -> Optional[str]:
    """FVG_FILL / OPPOSING_OB if the target sits at a detected zone;
    MAJOR_LEVEL if it sits at a swing point; None otherwise (see rationale
    in classify_sl_placement_reason -- no guessed default)."""
    if tp_price is None:
        return None
    reason = _nearest_zone_reason(structures, tp_price, tolerance, {
        "BULLISH_FVG": "FVG_FILL", "BEARISH_FVG": "FVG_FILL",
        "BULLISH_OB": "OPPOSING_OB", "BEARISH_OB": "OPPOSING_OB",
    })
    if reason:
        return reason

    for sp in swing_points:
        if abs(sp["price"] - tp_price) <= tolerance + _EPSILON:
            return "MAJOR_LEVEL"
    return None


def classify_market_bias(swing_points: list[dict]) -> Optional[str]:
    """Thin wrapper around structure_detection.classify_trend, kept here so
    every field-classification function lives in one module. Imported
    lazily to avoid a hard circular-import dependency between the two
    modules."""
    from structure_detection import classify_trend
    return classify_trend(swing_points)
