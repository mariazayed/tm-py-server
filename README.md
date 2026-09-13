# tm-py-server

Pulls closed trades from a MetaTrader 5 terminal for a day or date range and
records them in TradeMind via the `tm-server` REST API, auto-filling as many
analysis fields as MT5's data actually supports.

## Setup

1. This only works on Windows, with the MetaTrader 5 desktop terminal
   installed (the `MetaTrader5` Python package talks to it via local IPC --
   there is no remote/network MT5 API).
2. `python -m venv venv` then `venv\Scripts\activate`
3. `pip install -r requirements.txt`
4. `copy .env.example .env` and fill in:
   - `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` -- optional. Leave blank to
     just attach to whatever account is already logged into a running MT5
     terminal instead.
   - `TM_API_BASE_URL` -- where `tm-server` is running (default
     `http://localhost:8080`).
   - `TM_DEFAULT_STRATEGY_ID` -- **required**. Every trade needs a
     `strategyId`; imported trades aren't auto-classified into a strategy,
     so pick an id to file them under (re-assign later from the app).
   - `ACCOUNT_HISTORY_START` -- only used with `--reconstruct-balance`, see
     below.
5. Make sure `tm-server` is actually running before you import.

## Usage

```
python import_trades.py --date 2026-09-08
python import_trades.py --from 2026-09-01 --to 2026-09-08
python import_trades.py --date 2026-09-08 --dry-run
python import_trades.py --date 2026-09-08 --reconstruct-balance
python import_trades.py --date 2026-09-08 --skip-volatility
python import_trades.py --date 2026-09-08 --import-balance-ops
```

- `--date` and `--from`/`--to` are mutually exclusive; one is required.
- `--dry-run` fetches everything and prints the payloads it *would* send,
  without calling the TradeMind API at all. Always do this first on a new
  setup.
- Re-running the same (or an overlapping) range is safe. Every trade is
  tagged with `externalId = <MT5 position_id>`, and `tm-server` dedupes on
  that server-side (`TradeRepository.findByExternalIdIn`) before creating
  anything -- so nothing gets double-booked.
- `--import-balance-ops` also imports deposit/withdrawal operations (MT5's
  `DEAL_TYPE_BALANCE` deals) as `Capital` rows, so the app's capital-over-time
  chart on the Dashboard reflects your real broker history instead of only
  manually-entered deposits/withdrawals. A trade's own PROFIT/LOSS is
  **always** booked automatically by `tm-server` the moment the trade itself
  is imported -- this flag is only about the deposit/withdrawal side, which
  otherwise has to be entered by hand in the app. Safe to re-run for the same
  reason as trades: each row is tagged `externalId = <MT5 deal ticket>` and
  deduped server-side (`CapitalRepository.findByExternalIdIn`).
  **Caveat:** if you've already manually logged some of these same
  real-world deposits/withdrawals in the app before turning this flag on,
  running it for a date range that covers them WILL double-book those
  specific ones -- a manual entry has no `externalId` to dedupe against.
  Either start using `--import-balance-ops` from a date going forward (and
  keep your pre-existing manual entries as-is), or delete the manual ones
  you're about to re-import from MT5 first.

## What gets filled in, and by what

| Field | Source |
|---|---|
| entryDateTime / exitDateTime / entryPrice / exitPrice / lotSize / pnl | MT5 deal history (volume-weighted average price if the position was scaled) |
| direction | MT5 deal type of the entry fill |
| slPrice / tpPrice | Best-effort: the SL/TP recorded on the position's *original entry order*. MT5 does not log a history of manual SL/TP edits on an already-open position, so if you moved your stop mid-trade, this will NOT reflect that -- it's the SL/TP that was set when the trade was opened. |
| pips | Computed from MT5 `symbol_info` (point size + digits, standard 10x adjustment for 3/5-digit brokers) |
| mae / mfe | Computed from M1 price bars between entry and exit (see below) |
| volatility | ATR(14) on M15 bars, bucketed into LOW/MEDIUM/HIGH against a 200-bar percentile distribution for that symbol. Skippable with `--skip-volatility`. |
| accountBalanceAtEntry | Only with `--reconstruct-balance` (see caveat below); otherwise left blank |
| externalId / source | Set by the importer (`source=AUTO_IMPORTED`) so these trades are distinguishable from manually-entered ones and safely re-importable |
| session, dayOfWeek, tradeType, riskPercent, plannedRiskReward, rMultiple | **Not sent by this script at all.** `TradeEnrichmentService` on the server always computes these from the fields above on every trade create, regardless of source, so there's exactly one place that logic lives. |
| Everything else (entryReason, marketBias, mistakeTags, disciplineScore, etc.) | Left null. These are subjective/analytical fields meant to be filled in later from the app's review flow (`PATCH /trade/{id}/review`) -- by you, or eventually an AI suggestion pass (a later phase). |

## Structure detection & deterministic classification (Phase 5)

Two new modules run automatically on every import -- no flag needed, no LLM cost, pure arithmetic:

- **`structure_detection.py`** -- detects SMC/ICT-style price structures from M15 bars around each trade: swing highs/lows (fractals), break of structure (BOS) / change of character (CHoCH), order blocks, fair value gaps (FVGs), and liquidity sweeps. Fully unit-tested with synthetic OHLC data -- see `test_structure_detection.py` (run it yourself with `python3 -m unittest test_structure_detection.py -v`).
- **`classification_rules.py`** -- derives `entryZoneType`, `entryConfirmation`, `exitReason`, `slPlacementReason`, `tpPlacementReason`, and `marketBias` directly from the detected structures using proximity/recency rules (e.g. "the entry price sat inside a detected order block" -> `entryZoneType: OB`). These are geometric facts, not judgment calls, so they're computed here deterministically rather than asked of an LLM -- also unit-tested in the same file.

The raw structure list is also sent to the server as `detectedStructures` (JSON text) so it's available as context if you later ask tm-server's AI-suggestion endpoint (`POST /api/v1/trade/{id}/suggest`) to fill in the handful of fields that genuinely need holistic judgment across multiple signals: `entryReason`, `marketState`, `invalidationCondition`, and `probability` -- and, as a fallback, whichever of the fields above this deterministic pass couldn't determine. That endpoint is separate, explicit, and off by default (`ai.suggestions.enabled=false` in tm-server) -- see that project's `application-local.properties.example` to turn it on. It never touches a trade you've already reviewed, and never overwrites a field that's already set.

`entryZoneType: SK_LEVELS` is a valid value in the schema but its intended meaning isn't documented anywhere in the codebase, so this importer never emits it -- left for manual selection if you know what it's meant to represent.

## Known limitations (not bugs -- documented gaps in what MT5 exposes)

- **SL/TP edit history is not tracked.** `trade_steps` only auto-detects
  `SCALE_IN` (extra entry fills) and `SCALE_OUT` (partial-close fills),
  because those are visible as distinct deals. `MOVE_SL_TO_BE`,
  `SL_MODIFIED`, `TP_MODIFIED`, and `TRAILING_STOP_ADJUSTED` are NOT
  auto-detected -- MT5 does not keep a retrievable history of stop/target
  modifications made to an already-open position (those changes happen via
  `TRADE_ACTION_SLTP` and don't create a new deal or a lasting historical
  order record). If you want these recorded, add them manually from the app.
- **`--reconstruct-balance` assumes `ACCOUNT_HISTORY_START` genuinely
  predates the account's first deposit.** It works by summing every deal's
  P&L/swap/commission since that date into a running balance and reading
  off the value just before each trade's entry. If the start date is wrong
  (too late), every reconstructed balance will be off by whatever activity
  happened earlier. This is why it's opt-in rather than the default.
- **Broker server time vs. UTC.** MT5's `history_deals_get` is queried with
  UTC datetimes. If your broker's server clock isn't UTC, trades right at
  the edge of your requested day/range could be off by the server's offset.
  Widen the range with `--from`/`--to` if you suspect edge trades are
  missing.
- **This code has not been run against a live MT5 terminal** -- it was
  written and syntax-checked (`python -m py_compile`) in an environment
  with no MT5 installation available. Run `--dry-run` first, spot-check the
  printed payloads against a handful of trades you know the outcome of, and
  ideally test against a demo account before trusting it against your real
  account history.
- **`--import-balance-ops` only pulls raw balance operations, never a
  trade's P&L.** It filters strictly to `DEAL_TYPE_BALANCE` deals and
  ignores `DEAL_TYPE_BUY`/`DEAL_TYPE_SELL` ones -- those are already booked
  as PROFIT/LOSS `Capital` rows by `tm-server`'s `TradeService.create()` the
  moment the trade itself is imported. Don't add trade P&L handling to this
  path; it would double it.
