#!/usr/bin/env python3
"""
Entry point for the MT5 -> TradeMind importer.

Usage:
    python import_trades.py --date 2026-09-08
    python import_trades.py --from 2026-09-01 --to 2026-09-08
    python import_trades.py --date 2026-09-08 --reconstruct-balance
    python import_trades.py --date 2026-09-08 --skip-volatility
    python import_trades.py --date 2026-09-08 --dry-run

See README.md for the full explanation of what is/isn't computed here vs.
by the server (TradeEnrichmentService), and for the documented gaps in
what MT5's historical API can tell us (SL/TP edit history, in particular).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
import mt5_source
from tm_api_client import TmApiClient

# NewTradeRequest.direction / .source and NewTradeStepRequest.type are all
# Java enums whose JSON wire format is the integer declared next to each
# constant (@JsonValue) -- these dicts mirror those exact integers. If the
# Java enums are ever renumbered, update these too.
DIRECTION_TO_INT = {"LONG": 1, "SHORT": 2}
SOURCE_AUTO_IMPORTED = 2  # TradeSourceEnum.AUTO_IMPORTED
VOLATILITY_TO_INT = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}  # VolatilityEnum
STEP_TYPE_SCALE_IN = 5    # TradeUpdateEnum.SCALE_IN
STEP_TYPE_SCALE_OUT = 6   # TradeUpdateEnum.SCALE_OUT
STEP_TYPE_TO_INT = {"SCALE_IN": STEP_TYPE_SCALE_IN, "SCALE_OUT": STEP_TYPE_SCALE_OUT}

# Phase 5: the deterministic classification fields from
# mt5_source.fetch_structures_and_classifications(), mapped to their
# matching Java enums' @JsonValue integers. entryReason/marketState/
# invalidationCondition/probability are NOT here -- those are only ever
# filled server-side by AiSuggestionService (LLM judgment call), never by
# this importer.
ENTRY_ZONE_TYPE_TO_INT = {"OB": 1, "FVG": 2, "LIQUIDITY": 3, "SUPPORT_RESISTANCE": 4, "SK_LEVELS": 5}
ENTRY_CONFIRMATION_TO_INT = {
    "CANDLE_PATTERN": 1, "BOS": 2, "CHOCH": 3, "BREAKOUT": 4,
    "RETEST": 5, "LIQUIDITY_SWEEP": 6, "INDICATOR_CONFIRMATION": 7, "NONE": 8,
}
EXIT_REASON_TO_INT = {
    "TP_HIT": 1, "SL_HIT": 2, "MANUAL_EXIT": 3,
    "STRUCTURE_BREAK": 4, "PSYCHOLOGICAL_EXIT": 5, "NEWS_IMPACT": 6,
}
SL_PLACEMENT_REASON_TO_INT = {
    "STRUCTURE": 1, "VOLATILITY_BASED": 2, "PREVIOUS_HIGH": 3,
    "PREVIOUS_LOW": 4, "OB_BOUNDARY": 5, "FVG_BOUNDARY": 6,
}
TP_PLACEMENT_REASON_TO_INT = {
    "FVG_FILL": 1, "LIQUIDITY_SWEEP": 2, "MAJOR_LEVEL": 3,
    "RISK_MULTIPLE_ACHIEVED": 4, "OPPOSING_OB": 5,
}
MARKET_BIAS_TO_INT = {"BULLISH": 1, "BEARISH": 2, "NEUTRAL": 3}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import closed MT5 trades into TradeMind.")
    range_group = parser.add_mutually_exclusive_group(required=True)
    range_group.add_argument("--date", help="Single day to import, YYYY-MM-DD.")
    range_group.add_argument("--from", dest="date_from", help="Range start, YYYY-MM-DD (use with --to).")
    parser.add_argument("--to", dest="date_to", help="Range end, YYYY-MM-DD (inclusive, use with --from).")
    parser.add_argument("--reconstruct-balance", action="store_true",
                         help="Also reconstruct accountBalanceAtEntry from full deal history "
                              "(see README -- assumes ACCOUNT_HISTORY_START predates the account).")
    parser.add_argument("--skip-volatility", action="store_true",
                         help="Skip the ATR-based volatility classification (fewer MT5 calls, faster).")
    parser.add_argument("--dry-run", action="store_true",
                         help="Fetch and print what would be imported without calling the TradeMind API.")
    args = parser.parse_args()

    if args.date_from and not args.date_to:
        parser.error("--from requires --to")
    if args.date_to and not args.date_from:
        parser.error("--to requires --from")
    if not args.date and not args.date_from:
        parser.error("either --date or --from/--to is required")
    return args


def resolve_range(args: argparse.Namespace) -> tuple[datetime, datetime]:
    """MT5's history_deals_get treats the datetimes you pass it as UTC. If
    your broker's server time is not UTC, deals near midnight on the
    boundary dates may be off by the server's UTC offset -- widen the
    range with --from/--to if you suspect edge trades are being missed."""
    if args.date:
        day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return day, day + timedelta(days=1)

    start = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    return start, end


def compute_pips(symbol_info, entry_price: float, exit_price: float, direction: str) -> float:
    """Standard pip-size convention: 1 pip = 10 points on 3- or 5-digit
    brokers (fractional pip pricing), 1 pip = 1 point otherwise."""
    digits = symbol_info.digits
    pip_size = symbol_info.point * (10 if digits in (3, 5) else 1)
    if pip_size == 0:
        return 0.0
    diff = exit_price - entry_price if direction == "LONG" else entry_price - exit_price
    return diff / pip_size


def ensure_asset_ids(client: TmApiClient, symbols: set[str]) -> dict[str, dict]:
    """Returns {symbol: assetResponseDict}. Creates any missing asset (using
    MT5's trade_contract_size), and backfills contractSize on any existing
    asset that doesn't have one set yet -- this directly feeds Phase 3's
    server-side risk enrichment (TradeEnrichmentService), which silently
    skips risk math when contractSize is null."""
    import MetaTrader5 as mt5  # local import: only needed here, keeps module import order simple

    existing = {a["symbol"]: a for a in client.get_assets()}
    result: dict[str, dict] = {}

    for symbol in symbols:
        info = mt5.symbol_info(symbol)
        contract_size = float(info.trade_contract_size) if info is not None else None

        asset = existing.get(symbol)
        if asset is None:
            print(f"  Asset '{symbol}' not found in TradeMind -- creating it "
                  f"(contractSize={contract_size}).")
            asset = client.create_asset(name=symbol, symbol=symbol, contract_size=contract_size)
        elif asset.get("contractSize") is None and contract_size is not None:
            print(f"  Asset '{symbol}' exists but has no contractSize -- "
                  f"backfilling with {contract_size} from MT5.")
            asset = client.backfill_contract_size(asset["id"], contract_size)

        result[symbol] = asset

    return result


def build_trade_payload(position: mt5_source.ClosedPosition, asset_id: int,
                         mae_mfe_vol: dict, balance_at_entry: Optional[float],
                         pips: float, structure_fields: dict) -> dict:
    """Builds a NewTradeRequest JSON payload. Deliberately omits session,
    dayOfWeek, tradeType, riskPercent, plannedRiskReward and rMultiple --
    TradeEnrichmentService always (re)computes those server-side on create,
    so sending them here would be pointless (and immediately overwritten).

    `structure_fields` comes from
    mt5_source.fetch_structures_and_classifications() -- the deterministic,
    rule-based Phase 5 fields. entryReason/marketState/invalidationCondition/
    probability are intentionally not set here; those are filled later,
    server-side, only if still null, by the explicit /suggest endpoint
    (LLM judgment call, see tm-server's AiSuggestionService)."""
    volatility = mae_mfe_vol.get("volatility")
    entry_zone_type = structure_fields.get("entryZoneType")
    entry_confirmation = structure_fields.get("entryConfirmation")
    exit_reason = structure_fields.get("exitReason")
    sl_placement_reason = structure_fields.get("slPlacementReason")
    tp_placement_reason = structure_fields.get("tpPlacementReason")
    market_bias = structure_fields.get("marketBias")
    detected_structures = structure_fields.get("detectedStructures")

    return {
        "asset": asset_id,
        "entryDateTime": position.entry_time.replace(tzinfo=None).isoformat(),
        "exitDateTime": position.exit_time.replace(tzinfo=None).isoformat(),
        "entryPrice": position.entry_price,
        "exitPrice": position.exit_price,
        "slPrice": position.sl_price or 0.0,
        "tpPrice": position.tp_price or 0.0,
        "direction": DIRECTION_TO_INT[position.direction],
        "lotSize": position.volume,
        "pips": pips,
        "pnl": position.pnl,
        "strategy": None,
        "notes": None,
        "externalId": str(position.position_id),
        "source": SOURCE_AUTO_IMPORTED,
        "accountBalanceAtEntry": balance_at_entry,
        "mae": mae_mfe_vol.get("mae"),
        "mfe": mae_mfe_vol.get("mfe"),
        "volatility": VOLATILITY_TO_INT.get(volatility) if volatility else None,
        "entryZoneType": ENTRY_ZONE_TYPE_TO_INT.get(entry_zone_type) if entry_zone_type else None,
        "entryConfirmation": ENTRY_CONFIRMATION_TO_INT.get(entry_confirmation) if entry_confirmation else None,
        "exitReason": EXIT_REASON_TO_INT.get(exit_reason) if exit_reason else None,
        "slPlacementReason": SL_PLACEMENT_REASON_TO_INT.get(sl_placement_reason) if sl_placement_reason else None,
        "tpPlacementReason": TP_PLACEMENT_REASON_TO_INT.get(tp_placement_reason) if tp_placement_reason else None,
        "marketBias": MARKET_BIAS_TO_INT.get(market_bias) if market_bias else None,
        "detectedStructures": json.dumps(detected_structures) if detected_structures else None,
    }


def main() -> None:
    args = parse_args()
    settings = config.load_settings()
    date_from, date_to = resolve_range(args)

    print(f"Importing MT5 trades from {date_from.isoformat()} to {date_to.isoformat()} (UTC)...")

    mt5_source.connect(settings.mt5_login, settings.mt5_password, settings.mt5_server,
                        settings.mt5_terminal_path)
    try:
        import MetaTrader5 as mt5

        positions, deals_by_position = mt5_source.fetch_closed_positions(date_from, date_to)
        print(f"Found {len(positions)} closed position(s) in range.")
        if not positions:
            return

        mae_mfe_vol = mt5_source.fetch_mae_mfe_and_volatility(positions, args.skip_volatility)

        print("Detecting price structures (swings, BOS/CHoCH, order blocks, FVGs, "
              "liquidity sweeps) and deriving analysis fields deterministically...")
        structure_results = mt5_source.fetch_structures_and_classifications(positions)

        balances: dict[int, Optional[float]] = {}
        if args.reconstruct_balance:
            print(f"Reconstructing account balance history from {settings.account_history_start} "
                  f"(assumes this predates the account's real inception -- see README)...")
            balances = mt5_source.reconstruct_balance_at_entry(positions, settings.account_history_start)

        symbols = {p.symbol for p in positions}
        symbol_infos = {s: mt5.symbol_info(s) for s in symbols}

        client = TmApiClient(settings.tm_api_base_url, settings.tm_api_token)

        print(f"Resolving {len(symbols)} asset(s) against TradeMind...")
        assets_by_symbol = ensure_asset_ids(client, symbols)

        trade_payloads = []
        for position in positions:
            asset = assets_by_symbol[position.symbol]
            info = symbol_infos[position.symbol]
            pips = compute_pips(info, position.entry_price, position.exit_price, position.direction) \
                if info is not None else 0.0
            trade_payloads.append(build_trade_payload(
                position, asset["id"], mae_mfe_vol.get(position.position_id, {}),
                balances.get(position.position_id), pips,
                structure_results.get(position.position_id, {}),
            ))

        if args.dry_run:
            print("\n--dry-run: the following trades would be sent to TradeMind "
                  "(no API calls made):\n")
            for payload in trade_payloads:
                print(payload)
            return

        print(f"Sending {len(trade_payloads)} trade(s) to TradeMind...")
        created_trades = client.create_trades(trade_payloads)

        # Map externalId -> created trade id, so we know which positions are
        # genuinely new (vs. skipped as duplicates by server-side dedupe) --
        # only genuinely-new trades should get their scale-event steps posted.
        created_by_external_id = {t["externalId"]: t["id"] for t in created_trades if t.get("externalId")}

        step_payloads = []
        for position in positions:
            trade_id = created_by_external_id.get(str(position.position_id))
            if trade_id is None:
                continue  # duplicate (already imported before) or failed -- skip its steps
            for step in mt5_source.detect_scale_events(position):
                step_payloads.append({
                    "trade": trade_id,
                    "type": STEP_TYPE_TO_INT[step["type"]],
                    "occurredAt": datetime.fromisoformat(step["occurredAt"]).replace(tzinfo=None).isoformat(),
                    "priceAtEvent": step["priceAtEvent"],
                    "notes": step["notes"],
                })

        if step_payloads:
            print(f"Sending {len(step_payloads)} trade step(s) (scale-in/scale-out events)...")
            client.create_trade_steps(step_payloads)

        requested = len(positions)
        imported = len(created_by_external_id)
        skipped_or_failed = requested - imported
        print(f"\nRun summary: {requested} closed position(s) found, "
              f"{imported} imported, {skipped_or_failed} skipped as duplicates or failed "
              f"(see warnings above for any real failures).")
        print("NOTE: trade_steps only cover SCALE_IN / SCALE_OUT. MT5's history API does not "
              "reliably expose SL/TP edit history for already-open positions, so "
              "MOVE_SL_TO_BE / SL_MODIFIED / TP_MODIFIED / TRAILING_STOP_ADJUSTED steps are "
              "NOT auto-detected -- add those manually from the app if you want them recorded.")
    finally:
        mt5_source.disconnect()


if __name__ == "__main__":
    main()
