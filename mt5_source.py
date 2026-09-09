"""
All interaction with the MetaTrader5 terminal lives in this module.

Important, documented limitations of MT5's historical API that shape the
design here -- these are not bugs, they are honest constraints of what MT5
exposes after the fact:

  * MT5 does not expose a "position" object for closed trades directly.
    A closed position is reconstructed by grouping *deals* (fills) by their
    shared `position_id`. A simple trade has exactly one entry deal
    (DEAL_ENTRY_IN) and one exit deal (DEAL_ENTRY_OUT). A scaled-in and/or
    scaled-out trade has multiple deals on one or both sides.

  * MT5 does NOT keep a history of stop-loss / take-profit *edits*. The
    order history only tells us the SL/TP that was attached to the entry
    order at the time it was placed (or last modified before execution).
    If the user moved their SL to break-even mid-trade, we cannot see that
    from history alone. `_last_known_sl_tp` is explicitly best-effort: it
    reflects the SL/TP on record for the entry order, not necessarily the
    SL/TP that was active when the position closed. Likewise,
    `detect_scale_events` can only detect SCALE_IN / SCALE_OUT (extra fills
    on a position) -- it cannot detect MOVE_SL_TO_BE, SL_MODIFIED, or
    TP_MODIFIED events, because MT5's history API has no record of them.

  * `account_history` (used for `reconstruct_balance_at_entry`) has no
    concept of "since account inception" -- you must give it a start date.
    We ask the caller for `ACCOUNT_HISTORY_START` and trust it predates the
    account's real first deposit. If it doesn't, the reconstructed running
    balance will be wrong from the start.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

import classification_rules as cr
import structure_detection as sd

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover - only importable on Windows
    mt5 = None


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------

def connect(login: Optional[int], password: Optional[str], server: Optional[str],
            terminal_path: Optional[str]) -> None:
    """Initializes the MT5 terminal connection. Exits the process on failure
    -- there is no sensible way to continue an import without a working
    terminal connection."""
    if mt5 is None:
        print(
            "ERROR: the MetaTrader5 package is not installed or not "
            "importable. It only works on Windows with a real MT5 "
            "installation. Run: pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    init_kwargs = {}
    if terminal_path:
        init_kwargs["path"] = terminal_path

    ok = mt5.initialize(**init_kwargs) if init_kwargs else mt5.initialize()
    if not ok:
        print(f"ERROR: mt5.initialize() failed: {mt5.last_error()}", file=sys.stderr)
        sys.exit(1)

    if login and password and server:
        authorized = mt5.login(login, password=password, server=server)
        if not authorized:
            print(f"ERROR: mt5.login() failed: {mt5.last_error()}", file=sys.stderr)
            mt5.shutdown()
            sys.exit(1)

    account_info = mt5.account_info()
    if account_info is None:
        print(
            "ERROR: connected to a terminal but no account is logged in. "
            "Either log into an account in the MT5 terminal manually, or "
            "set MT5_LOGIN/MT5_PASSWORD/MT5_SERVER in .env.",
            file=sys.stderr,
        )
        mt5.shutdown()
        sys.exit(1)

    print(f"Connected to MT5 account {account_info.login} on {account_info.server} "
          f"(balance: {account_info.balance} {account_info.currency})")


def disconnect() -> None:
    if mt5 is not None:
        mt5.shutdown()


# ---------------------------------------------------------------------------
# Closed position reconstruction
# ---------------------------------------------------------------------------

@dataclass
class ClosedPosition:
    position_id: int
    symbol: str
    direction: str  # "LONG" or "SHORT"
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    volume: float
    pnl: float
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    entry_deal_count: int = 1
    exit_deal_count: int = 1
    # raw deal tickets, kept around for scale-event detection downstream
    entry_deals: list = field(default_factory=list)
    exit_deals: list = field(default_factory=list)


def _to_direction(deal_type: int) -> str:
    # DEAL_TYPE_BUY = 0, DEAL_TYPE_SELL = 1. An entry deal that is a BUY
    # means the position is LONG; an entry deal that is a SELL means SHORT.
    return "LONG" if deal_type == mt5.DEAL_TYPE_BUY else "SHORT"


def _last_known_sl_tp(position_id: int) -> tuple[Optional[float], Optional[float]]:
    """Best-effort lookup of the SL/TP recorded on the entry order for this
    position. See module docstring: this does NOT capture later manual
    SL/TP edits made without a corresponding new order (MT5 does not log
    those as retrievable history)."""
    orders = mt5.history_orders_get(position=position_id)
    if not orders:
        return None, None
    # Use the earliest order (the original entry order) as the reference.
    orders_sorted = sorted(orders, key=lambda o: o.time_setup)
    entry_order = orders_sorted[0]
    sl = entry_order.sl or None
    tp = entry_order.tp or None
    return sl, tp


def fetch_closed_positions(date_from: datetime, date_to: datetime
                            ) -> tuple[list[ClosedPosition], dict[int, list]]:
    """Pulls all deals in [date_from, date_to], groups them by position_id,
    and returns fully-closed positions (those with both an IN and an OUT
    side). Positions still open (only IN deals) are skipped -- there is
    nothing to record yet.

    Returns (positions, deals_by_position) -- the raw deals map is handed
    back so detect_scale_events() can reuse it without re-querying MT5.
    """
    deals = mt5.history_deals_get(date_from, date_to)
    if deals is None:
        print(f"WARNING: history_deals_get returned None: {mt5.last_error()}", file=sys.stderr)
        deals = ()

    by_position: dict[int, list] = {}
    for deal in deals:
        # DEAL_TYPE_BUY/SELL only -- skip balance ops, credits, etc.
        if deal.type not in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
            continue
        by_position.setdefault(deal.position_id, []).append(deal)

    positions: list[ClosedPosition] = []
    for position_id, position_deals in by_position.items():
        position_deals.sort(key=lambda d: d.time)
        entry_deals = [d for d in position_deals if d.entry == mt5.DEAL_ENTRY_IN]
        exit_deals = [d for d in position_deals if d.entry == mt5.DEAL_ENTRY_OUT]

        if not entry_deals or not exit_deals:
            # Either still open, or a deal type we don't handle (e.g. a
            # DEAL_ENTRY_INOUT reversal) -- skip rather than guess.
            continue

        entry_volume = sum(d.volume for d in entry_deals)
        exit_volume = sum(d.volume for d in exit_deals)
        if entry_volume <= 0 or exit_volume <= 0:
            continue

        entry_price = sum(d.price * d.volume for d in entry_deals) / entry_volume
        exit_price = sum(d.price * d.volume for d in exit_deals) / exit_volume
        pnl = sum(d.profit + d.swap + d.commission for d in position_deals)

        sl_price, tp_price = _last_known_sl_tp(position_id)

        positions.append(ClosedPosition(
            position_id=position_id,
            symbol=entry_deals[0].symbol,
            direction=_to_direction(entry_deals[0].type),
            entry_time=datetime.fromtimestamp(entry_deals[0].time, tz=timezone.utc),
            exit_time=datetime.fromtimestamp(exit_deals[-1].time, tz=timezone.utc),
            entry_price=entry_price,
            exit_price=exit_price,
            volume=min(entry_volume, exit_volume),
            pnl=pnl,
            sl_price=sl_price,
            tp_price=tp_price,
            entry_deal_count=len(entry_deals),
            exit_deal_count=len(exit_deals),
            entry_deals=entry_deals,
            exit_deals=exit_deals,
        ))

    positions.sort(key=lambda p: p.entry_time)
    return positions, by_position


# ---------------------------------------------------------------------------
# Scale-in / scale-out step detection
# ---------------------------------------------------------------------------

def detect_scale_events(position: ClosedPosition) -> list[dict]:
    """Returns a list of trade_step payload dicts (type + occurred_at +
    price_at_event + notes) for SCALE_IN and SCALE_OUT events only.

    Deliberately NOT detected here (MT5 history cannot reconstruct these
    reliably after the fact): MOVE_SL_TO_BE, SL_MODIFIED, TP_MODIFIED,
    TRAILING_STOP_ADJUSTED. See module docstring.
    """
    steps: list[dict] = []

    # First entry deal is the original entry, not a scale-in.
    for deal in position.entry_deals[1:]:
        steps.append({
            "type": "SCALE_IN",
            "occurredAt": datetime.fromtimestamp(deal.time, tz=timezone.utc).isoformat(),
            "priceAtEvent": deal.price,
            "notes": f"Additional entry fill: {deal.volume} lots @ {deal.price} "
                     f"(deal #{deal.ticket})",
        })

    # Last exit deal is the final close, not a scale-out.
    for deal in position.exit_deals[:-1]:
        steps.append({
            "type": "SCALE_OUT",
            "occurredAt": datetime.fromtimestamp(deal.time, tz=timezone.utc).isoformat(),
            "priceAtEvent": deal.price,
            "notes": f"Partial close fill: {deal.volume} lots @ {deal.price} "
                     f"(deal #{deal.ticket})",
        })

    return steps


# ---------------------------------------------------------------------------
# MAE / MFE + ATR-based volatility
# ---------------------------------------------------------------------------

_VOLATILITY_LOOKBACK_BARS = 200  # M15 bars used to build the ATR percentile distribution


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    prev_close = np.roll(closes, 1)
    prev_close[0] = closes[0]
    tr = np.maximum.reduce([
        highs - lows,
        np.abs(highs - prev_close),
        np.abs(lows - prev_close),
    ])
    atr = np.full_like(tr, np.nan)
    if len(tr) >= period:
        atr[period - 1] = tr[:period].mean()
        for i in range(period, len(tr)):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def _volatility_bucket(current_atr: float, historical_atr: np.ndarray) -> Optional[str]:
    historical_atr = historical_atr[~np.isnan(historical_atr)]
    if len(historical_atr) < 20 or np.isnan(current_atr):
        return None
    p33, p66 = np.percentile(historical_atr, [33, 66])
    if current_atr <= p33:
        return "LOW"
    if current_atr <= p66:
        return "MEDIUM"
    return "HIGH"


def fetch_mae_mfe_and_volatility(positions: list[ClosedPosition], skip_volatility: bool
                                  ) -> dict[int, dict]:
    """One copy_rates_range (M1) call per distinct symbol covering the full
    span of all positions on that symbol, sliced in memory per-trade to
    compute MAE/MFE. One copy_rates_range (M15) call per symbol for the
    ATR-based volatility bucket. Batching per-symbol keeps this to a
    handful of MT5 calls instead of one per trade.

    Returns {position_id: {"mae": float|None, "mfe": float|None,
                            "volatility": "LOW"|"MEDIUM"|"HIGH"|None}}
    """
    results: dict[int, dict] = {}
    by_symbol: dict[str, list[ClosedPosition]] = {}
    for p in positions:
        by_symbol.setdefault(p.symbol, []).append(p)

    for symbol, symbol_positions in by_symbol.items():
        earliest = min(p.entry_time for p in symbol_positions)
        latest = max(p.exit_time for p in symbol_positions)

        m1_rates = mt5.copy_rates_range(
            symbol, mt5.TIMEFRAME_M1,
            earliest - timedelta(minutes=5), latest + timedelta(minutes=5),
        )

        m15_atr_by_time = None
        if not skip_volatility:
            m15_rates = mt5.copy_rates_range(
                symbol, mt5.TIMEFRAME_M15,
                earliest - timedelta(days=10), latest,
            )
            if m15_rates is not None and len(m15_rates) > 0:
                highs = m15_rates["high"].astype(float)
                lows = m15_rates["low"].astype(float)
                closes = m15_rates["close"].astype(float)
                atr_series = _atr(highs, lows, closes)
                m15_atr_by_time = (m15_rates["time"], atr_series)

        for position in symbol_positions:
            mae = mfe = volatility = None

            if m1_rates is not None and len(m1_rates) > 0:
                mask = (m1_rates["time"] >= int(position.entry_time.timestamp())) & \
                       (m1_rates["time"] <= int(position.exit_time.timestamp()))
                window = m1_rates[mask]
                if len(window) > 0:
                    highs = window["high"].astype(float)
                    lows = window["low"].astype(float)
                    if position.direction == "LONG":
                        mfe = float(highs.max()) - position.entry_price
                        mae = position.entry_price - float(lows.min())
                    else:
                        mfe = position.entry_price - float(lows.min())
                        mae = float(highs.max()) - position.entry_price
                    mae = max(mae, 0.0)
                    mfe = max(mfe, 0.0)

            if m15_atr_by_time is not None:
                times, atr_series = m15_atr_by_time
                idx = np.searchsorted(times, int(position.entry_time.timestamp()), side="right") - 1
                if 0 <= idx < len(atr_series):
                    lookback_start = max(0, idx - _VOLATILITY_LOOKBACK_BARS)
                    historical = atr_series[lookback_start:idx]
                    volatility = _volatility_bucket(atr_series[idx], historical)

            results[position.position_id] = {"mae": mae, "mfe": mfe, "volatility": volatility}

    return results


# ---------------------------------------------------------------------------
# Opt-in account balance reconstruction (for accountBalanceAtEntry)
# ---------------------------------------------------------------------------

def reconstruct_balance_at_entry(positions: list[ClosedPosition], history_start
                                  ) -> dict[int, Optional[float]]:
    """Builds a cumulative running-balance timeline from ALL deals since
    `history_start` (deposits, withdrawals, and every trade's P&L), then
    looks up the balance immediately before each position's entry time.

    This assumes `history_start` predates the account's actual inception --
    if it doesn't, every reconstructed balance will be off by whatever
    activity happened before `history_start`. This is why the CLI makes
    this opt-in (--reconstruct-balance) rather than always-on.
    """
    if not isinstance(history_start, datetime):
        history_start = datetime.combine(history_start, datetime.min.time(), tzinfo=timezone.utc)
    elif history_start.tzinfo is None:
        history_start = history_start.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    all_deals = mt5.history_deals_get(history_start, now)
    if not all_deals:
        print("WARNING: could not fetch full deal history for balance reconstruction; "
              "accountBalanceAtEntry will be left blank.", file=sys.stderr)
        return {p.position_id: None for p in positions}

    all_deals = sorted(all_deals, key=lambda d: d.time)
    times = np.array([d.time for d in all_deals], dtype=np.int64)
    # Every deal's net effect on balance: trade P&L (profit+swap+commission)
    # plus any raw balance operations (deposits/withdrawals/credits) which
    # MT5 represents as DEAL_TYPE_BALANCE deals with the amount in `profit`.
    deltas = np.array([d.profit + d.swap + d.commission for d in all_deals], dtype=np.float64)
    running_balance = np.cumsum(deltas)

    result: dict[int, Optional[float]] = {}
    for position in positions:
        entry_ts = int(position.entry_time.timestamp())
        idx = np.searchsorted(times, entry_ts, side="left") - 1
        if idx < 0:
            # No deal history before this trade's entry within our window --
            # cannot reconstruct.
            result[position.position_id] = None
        else:
            result[position.position_id] = float(running_balance[idx])

    return result


# ---------------------------------------------------------------------------
# Structure detection + deterministic field classification (Phase 5)
# ---------------------------------------------------------------------------

_STRUCTURE_LOOKBACK_BARS = 150  # M15 bars of history before entry, for swing/BOS/CHoCH context
_STRUCTURE_M15_SECONDS = 15 * 60
_CONFIRMATION_WINDOW_SECONDS = 4 * _STRUCTURE_M15_SECONDS  # "recent" = within the last hour of M15 bars
_TOLERANCE_ATR_FRACTION = 0.25  # how close price must be to a structure to "count" as touching it


def _strip_for_json(events: list[dict]) -> list[dict]:
    """Drops the slice-local `index` field before this goes into the
    detectedStructures JSON sent to the backend -- it's meaningless outside
    the in-memory bar slice it was computed from. `time`/`price`/`top`/
    `bottom` are all real, portable values (unix timestamps and actual
    prices), so those are kept."""
    return [{k: v for k, v in e.items() if k != "index"} for e in events]


def fetch_structures_and_classifications(positions: list["ClosedPosition"]) -> dict[int, dict]:
    """One M15 copy_rates_range call per distinct symbol (covering from
    _STRUCTURE_LOOKBACK_BARS bars before the earliest entry through the
    latest exit), sliced per-trade in memory -- same batching philosophy as
    fetch_mae_mfe_and_volatility, for the same reason (avoid one MT5 call
    per trade).

    For each position, runs the deterministic detectors in
    structure_detection.py and the deterministic field classifiers in
    classification_rules.py (both pure, unit-tested, no MT5/LLM
    involvement) and returns:

        {position_id: {
            "detectedStructures": [...],       # JSON-ready list of dicts
            "entryZoneType": "OB"|"FVG"|"LIQUIDITY"|"SUPPORT_RESISTANCE"|None,
            "entryConfirmation": "BOS"|"CHOCH"|"LIQUIDITY_SWEEP"|"NONE",
            "exitReason": "TP_HIT"|"SL_HIT"|"STRUCTURE_BREAK"|"MANUAL_EXIT",
            "slPlacementReason": ...|None,
            "tpPlacementReason": ...|None,
            "marketBias": "BULLISH"|"BEARISH"|"NEUTRAL"|None,
        }}

    entryReason, marketState, invalidationCondition and probability are
    deliberately NOT produced here -- those need holistic judgment across
    multiple signals and are left for tm-server's AiSuggestionService
    (explicit, cost-gated LLM call) to fill in, only for whatever's still
    null after this deterministic pass.
    """
    results: dict[int, dict] = {}
    by_symbol: dict[str, list["ClosedPosition"]] = {}
    for p in positions:
        by_symbol.setdefault(p.symbol, []).append(p)

    for symbol, symbol_positions in by_symbol.items():
        earliest = min(p.entry_time for p in symbol_positions)
        latest = max(p.exit_time for p in symbol_positions)
        range_start = earliest - timedelta(seconds=_STRUCTURE_LOOKBACK_BARS * _STRUCTURE_M15_SECONDS)

        m15_rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M15, range_start, latest)
        if m15_rates is None or len(m15_rates) == 0:
            print(f"WARNING: no M15 bars for {symbol}; skipping structure detection for its trades.",
                  file=sys.stderr)
            continue

        times_all = m15_rates["time"].astype(np.int64)
        opens_all = m15_rates["open"].astype(float)
        highs_all = m15_rates["high"].astype(float)
        lows_all = m15_rates["low"].astype(float)
        closes_all = m15_rates["close"].astype(float)

        for position in symbol_positions:
            window_start_ts = int(position.entry_time.timestamp()) - \
                _STRUCTURE_LOOKBACK_BARS * _STRUCTURE_M15_SECONDS
            window_end_ts = int(position.exit_time.timestamp())
            mask = (times_all >= window_start_ts) & (times_all <= window_end_ts)

            if mask.sum() < 10:
                # Not enough bars to say anything meaningful.
                results[position.position_id] = {
                    "detectedStructures": [], "entryZoneType": None, "entryConfirmation": "NONE",
                    "exitReason": "MANUAL_EXIT", "slPlacementReason": None, "tpPlacementReason": None,
                    "marketBias": None,
                }
                continue

            times = times_all[mask]
            opens = opens_all[mask]
            highs = highs_all[mask]
            lows = lows_all[mask]
            closes = closes_all[mask]

            swing_points = sd.find_swing_points(highs, lows, times)
            structures = sd.detect_structures(times, opens, highs, lows, closes)

            atr_series = _atr(highs, lows, closes)
            valid_atr = atr_series[~np.isnan(atr_series)]
            tolerance = float(valid_atr[-1]) * _TOLERANCE_ATR_FRACTION if len(valid_atr) > 0 else 0.0
            if tolerance <= 0:
                # Degenerate/flat data -- fall back to a tiny fraction of the
                # entry price so comparisons aren't all-or-nothing at zero.
                tolerance = position.entry_price * 0.0005

            entry_ts = int(position.entry_time.timestamp())
            exit_ts = int(position.exit_time.timestamp())

            entry_zone_type = cr.classify_entry_zone_type(structures, position.entry_price, entry_ts, tolerance)
            entry_confirmation = cr.classify_entry_confirmation(structures, entry_ts, _CONFIRMATION_WINDOW_SECONDS)
            exit_reason = cr.classify_exit_reason(
                position.exit_price, position.sl_price, position.tp_price, structures,
                exit_ts, position.direction, tolerance, _CONFIRMATION_WINDOW_SECONDS,
            )
            sl_placement_reason = cr.classify_sl_placement_reason(structures, swing_points, position.sl_price, tolerance)
            tp_placement_reason = cr.classify_tp_placement_reason(structures, position.tp_price, swing_points, tolerance)
            market_bias = cr.classify_market_bias(swing_points)

            results[position.position_id] = {
                "detectedStructures": _strip_for_json(structures),
                "entryZoneType": entry_zone_type,
                "entryConfirmation": entry_confirmation,
                "exitReason": exit_reason,
                "slPlacementReason": sl_placement_reason,
                "tpPlacementReason": tp_placement_reason,
                "marketBias": market_bias,
            }

    return results
