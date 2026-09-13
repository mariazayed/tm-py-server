# Integration testing & rollout runbook (Phase 7)

This is the part of Phase 7 that genuinely cannot be automated from a cloud sandbox or scripted against your machine on your behalf: a real MT5 terminal, logged into a real (ideally demo) account, with `tm-server` running against a real Postgres database. Every unit-testable piece of this project (structure detection, classification rules, enrichment math, dedupe filtering, static-data integrity, the AI-suggestion prompt/parser/guard logic, the importer's payload-building logic) already has real, executable tests — see "What's already covered" below. This document is the checklist for the remaining ~20%: does the whole thing actually work end to end, against a live terminal, once.

**Important context:** Phases 0 through 6 of this project were all built in one continuous session, rather than the gradual "ship 1-4, use it for a few weeks, then build 5" rollout the original plan recommended. That's not wrong, but it does mean none of this has been validated against real usage yet. Treat this runbook as the thing that closes that gap before you trust any of it with real trading data.

## 0. Before you start

- [ ] `pg_dump` your database. If anything here goes sideways, you want a clean restore point.
- [ ] Confirm `tm-server` starts cleanly against that real database and Flyway applies all seven migrations (`V1`...`V7`) without error. This is the first time they've run against anything but the stub verification DB used during development.
- [ ] Confirm `tm-py-server`'s `.env` is filled in (`MT5_LOGIN`/`PASSWORD`/`SERVER` or a running terminal already logged in, `TM_API_BASE_URL`).
- [ ] Run the automated test suites for real, on your machine (both need network access to Maven Central / PyPI this sandbox doesn't have):
  - `tm-server`: `mvnw test` — should include `TradeEnrichmentServiceTest`, `AiSuggestionResponseParserTest`, `AiSuggestionServiceTest`, `TradeServiceTest`, `StaticDataTest`.
  - `tm-py-server`: `python3 -m unittest discover -v` — should include `test_structure_detection.py` (22 tests) and `test_import_trades.py` (7 tests).
  - Both should pass clean before you touch a live terminal.

## 1. Dry run first

```
python import_trades.py --date <a day you remember trading> --dry-run
```

Don't skip this. Pick a day where you can remember roughly what happened. For each printed payload:
- [ ] `direction`, `entryPrice`, `exitPrice`, `lotSize`, `pnl` match what MT5's own History tab shows for that position.
- [ ] `pips` looks right for the symbol (watch for the 3/5-digit broker pip-size adjustment mentioned in the importer's README).
- [ ] `slPrice`/`tpPrice` match the *original entry order* — remember this is best-effort and won't reflect a stop you moved mid-trade (documented limitation, not a bug).
- [ ] `externalId` is present and looks like a real MT5 position id.
- [ ] `detectedStructures` is either `null` or a plausible-looking JSON structure list — spot check one against the actual chart for that symbol/time if you want real confidence in the structure detector.

## 2. Real run

Same command without `--dry-run`. Check the printed run summary (`N found, N imported, N skipped`) matches what you expect, then confirm in the app that the trades showed up with sensible values.

- [ ] Trade count in the app matches "imported" count from the summary.
- [ ] No unexpected duplicate Capital rows were created (check the Risk Management / capital history screen).

## 3. Idempotency — the actual "re-running is safe" guarantee

This is the specific thing Phase 7's plan called out: *"re-running the same range must add zero new rows."* `TradeServiceTest.reimportingAnOverlappingRangeKeepsOnlyTheGenuinelyNewPositions` and `test_import_trades.py`'s idempotency test both verify the two halves of this mechanism in isolation (server-side dedupe filtering, client-side payload determinism) — this step verifies they actually compose correctly against a real database.

```
python import_trades.py --date <the same day as step 2>
```

- [ ] Run summary reports **0 imported**, all positions skipped as duplicates.
- [ ] Trade count in the app is unchanged from step 2.
- [ ] No new Capital rows were created (the dedupe filter runs *before* Capital rows are built, specifically to prevent this).

Also try an **overlapping but not identical** range (e.g. yesterday + today, when yesterday was already imported) and confirm only today's trades get added.

## 4. AI suggestions (optional — costs money, off by default)

Only if you want to validate this path before relying on it:

- [ ] Set `ai.suggestions.enabled=true` and a real `ai.provider.api-key` in `application-local.properties`.
- [ ] `POST /api/v1/trade/{id}/suggest` for one imported, not-yet-reviewed trade. Confirm `entryReason`/`marketState`/`invalidationCondition`/`probability` (and any of the six deterministic fields the structure pass left null) come back filled with plausible values.
- [ ] Call it again on the same trade — confirm it doesn't change anything already set (the guard re-checks null-ness per field at write time, not just once).
- [ ] `PATCH /trade/{id}/review` that trade (or confirm it from the Flutter review screen), then call `/suggest` on it again — confirm you now get a **409 Conflict** with a clear "already been reviewed" message, not a generic 500. This is the specific bug Phase 6 fixed in `AiSuggestionService`/`GlobalExceptionHandler`.
- [ ] With `ai.suggestions.enabled=false`, confirm `/suggest` returns a **400 Bad Request** with the "AI suggestions are disabled" message, again not a generic 500.

## 5. Flutter review screen

- [ ] Open **Trade Review** from the sidebar. Confirm the queue shows only trades with `reviewedAt` unset, and only those.
- [ ] Confirm chips are pre-filled where the deterministic importer pass set them (no need to tap anything for those).
- [ ] Tap **Get AI Suggestions** (only if step 4 is set up) — confirm only the chips that were actually still empty get filled, and that they're visually marked as AI-suggested.
- [ ] Move the discipline slider, pick mistake tags, tap **Confirm**. Confirm the trade drops out of the queue and `reviewedAt` is now set (check via the API or DB) — and that `mistakeTags` was actually written even if you left it empty (it's always sent explicitly, precisely so clearing it works).
- [ ] Tap **Skip** on a trade — confirm it comes back around in the queue rather than vanishing.

## What's already covered by real, executable automated tests

No need to re-verify these by hand — they're checked into the repo and run in step 0:

| Area | Test file | What it catches |
|---|---|---|
| Enrichment math (session/dayOfWeek/tradeType/risk) | `TradeEnrichmentServiceTest` | Divide-by-zero, boundary hours, bad-data (exit-before-entry) guards |
| Dedupe-on-overlapping-import | `TradeServiceTest` | The exact filtering logic behind "re-running is safe" |
| Static-data integrity | `StaticDataTest` | Empty lists (the `SCORE_LIST` bug) and duplicate `order` values (the `TRADE_DIRECTIONS`/`PROBABILITIES`/`VOLATILITIES`/`TAKE_PROFIT_PLACEMENT_REASONS` bug), for every current and future static-data list |
| AI-suggestion field selection | `AiSuggestionServiceTest` | Never re-asks about an already-filled field; asks about exactly the right ten when blank |
| AI response parsing | `AiSuggestionResponseParserTest` | Markdown-fenced JSON, prose-wrapped JSON, garbage text, hallucinated enum values |
| Structure detection (swings/BOS/CHoCH/OBs/FVGs/sweeps) | `test_structure_detection.py` (22 tests) | Off-by-one errors in swing absorption, boundary conditions |
| Deterministic classification rules | `test_structure_detection.py` (same file) | Floating-point tolerance boundary bugs |
| Importer payload construction | `test_import_trades.py` (7 tests) | Enum-to-int mapping correctness, never sending server-computed or LLM-only fields, payload determinism (the client-side half of idempotency) |

## Rollout order

The original plan's recommendation was: ship Phases 1-4 (schema + computed fields + importer) first and use them for a few weeks before building Phase 5 (the AI layer), so the data model gets validated against real usage before investing further. That didn't happen here — all six phases were built together. This runbook is how you retroactively get that validation before trusting any of it with real trading data:

1. Do steps 0-3 above first, with `ai.suggestions.enabled=false` (the default). This validates Phases 0-4 (and the Phase 5 deterministic half) against a real terminal and real data.
2. Use the app for real journaling for a while on that foundation alone before turning AI suggestions on.
3. Only then do step 4 (AI suggestions) and step 5 (the Flutter review screen) for real, and only with a strategy budget you're comfortable spending on LLM calls — it's a per-request cost, off by default, and never automatic.
