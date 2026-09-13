"""
Phase 7: exercises import_trades.build_trade_payload() -- the pure function
that turns one MT5 ClosedPosition into the exact JSON body sent to
POST /api/v1/trade. Genuinely executable (no MT5 terminal needed: neither
`import_trades` nor `mt5_source` imports the MetaTrader5 package at module
level, only inside the functions that actually talk to a running terminal),
unlike anything that calls into MetaTrader5 itself.

This is the client-side half of "re-running an overlapping import range is
safe": build_trade_payload must be a pure, deterministic function of its
inputs (in particular externalId = str(position_id)), so the exact same
position always produces the exact same payload. The server-side half --
that a duplicate externalId is actually rejected -- is covered by
tm-server's TradeServiceTest (filterNewRequests).

Run with: python3 -m unittest test_import_trades.py -v
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

import import_trades as it
from mt5_source import BalanceOperation, ClosedPosition


def _sample_position(**overrides) -> ClosedPosition:
    defaults = dict(
        position_id=123456789,
        symbol="EURUSD",
        direction="LONG",
        entry_time=datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc),
        exit_time=datetime(2026, 9, 8, 11, 30, tzinfo=timezone.utc),
        entry_price=1.1000,
        exit_price=1.1050,
        volume=0.5,
        pnl=25.0,
        sl_price=1.0950,
        tp_price=1.1100,
    )
    defaults.update(overrides)
    return ClosedPosition(**defaults)


class BuildTradePayloadTest(unittest.TestCase):
    def test_maps_core_fields(self):
        payload = it.build_trade_payload(
            _sample_position(), asset_id=7,
            mae_mfe_vol={"mae": -5.0, "mfe": 12.0, "volatility": "HIGH"},
            balance_at_entry=10000.0,
            pips=50.0,
            structure_fields={},
        )
        self.assertEqual(payload["asset"], 7)
        self.assertEqual(payload["direction"], 1)  # LONG -> TradeDirectionEnum.LONG
        self.assertEqual(payload["externalId"], "123456789")
        self.assertEqual(payload["source"], 2)  # TradeSourceEnum.AUTO_IMPORTED
        self.assertEqual(payload["pips"], 50.0)
        self.assertEqual(payload["mae"], -5.0)
        self.assertEqual(payload["mfe"], 12.0)
        self.assertEqual(payload["volatility"], 3)  # VolatilityEnum.HIGH
        self.assertEqual(payload["accountBalanceAtEntry"], 10000.0)
        self.assertEqual(payload["entryDateTime"], "2026-09-08T10:00:00")
        self.assertEqual(payload["exitDateTime"], "2026-09-08T11:30:00")

    def test_short_direction_maps_to_two(self):
        payload = it.build_trade_payload(
            _sample_position(direction="SHORT"), asset_id=1,
            mae_mfe_vol={}, balance_at_entry=None, pips=-10.0, structure_fields={},
        )
        self.assertEqual(payload["direction"], 2)

    def test_structure_fields_mapped_to_java_enum_ints(self):
        structure_fields = {
            "entryZoneType": "OB",
            "entryConfirmation": "BOS",
            "exitReason": "TP_HIT",
            "slPlacementReason": "STRUCTURE",
            "tpPlacementReason": "FVG_FILL",
            "marketBias": "BULLISH",
            "detectedStructures": [{"type": "BOS", "time": 1757325600, "price": 1.099}],
        }
        payload = it.build_trade_payload(
            _sample_position(), asset_id=1, mae_mfe_vol={}, balance_at_entry=None,
            pips=50.0, structure_fields=structure_fields,
        )
        self.assertEqual(payload["entryZoneType"], 1)
        self.assertEqual(payload["entryConfirmation"], 2)
        self.assertEqual(payload["exitReason"], 1)
        self.assertEqual(payload["slPlacementReason"], 1)
        self.assertEqual(payload["tpPlacementReason"], 1)
        self.assertEqual(payload["marketBias"], 1)
        self.assertEqual(
            json.loads(payload["detectedStructures"]),
            structure_fields["detectedStructures"],
        )

    def test_missing_structure_fields_are_null_not_keyerror(self):
        payload = it.build_trade_payload(
            _sample_position(), asset_id=1, mae_mfe_vol={}, balance_at_entry=None,
            pips=0.0, structure_fields={},
        )
        for key in ("entryZoneType", "entryConfirmation", "exitReason",
                    "slPlacementReason", "tpPlacementReason", "marketBias",
                    "detectedStructures", "volatility"):
            self.assertIsNone(payload[key], f"{key} should be None, not missing/raise")

    def test_never_sends_server_computed_fields(self):
        # TradeEnrichmentService always (re)computes these server-side --
        # sending them from the importer would be pointless (and immediately
        # overwritten), so build_trade_payload must never include them.
        payload = it.build_trade_payload(
            _sample_position(), asset_id=1, mae_mfe_vol={}, balance_at_entry=None,
            pips=0.0, structure_fields={},
        )
        for key in ("session", "dayOfWeek", "tradeType", "riskPercent",
                    "plannedRiskReward", "rMultiple"):
            self.assertNotIn(key, payload)

    def test_llm_only_fields_are_never_set_by_importer(self):
        # entryReason/marketState/invalidationCondition/probability are only
        # ever filled server-side by AiSuggestionService's explicit /suggest
        # call -- the importer must never claim to know them, even if a
        # caller mistakenly stuffed one into structure_fields.
        payload = it.build_trade_payload(
            _sample_position(), asset_id=1, mae_mfe_vol={}, balance_at_entry=None,
            pips=0.0, structure_fields={"entryReason": "BREAKOUT"},
        )
        for key in ("entryReason", "marketState", "invalidationCondition", "probability"):
            self.assertNotIn(key, payload)

    def test_idempotent_reimport_produces_identical_payload_and_external_id(self):
        # The concrete guarantee behind "re-running an overlapping import
        # range is safe": the exact same MT5 position data must always
        # produce the exact same payload, in particular the same
        # externalId, so the server's findByExternalIdIn dedupe (see
        # tm-server's TradeServiceTest) actually catches it as a duplicate
        # on a re-run instead of silently drifting.
        position = _sample_position()
        mae_mfe_vol = {"mae": -3.0, "mfe": 9.0, "volatility": "MEDIUM"}
        structure_fields = {"entryZoneType": "FVG", "marketBias": "BEARISH"}

        first = it.build_trade_payload(position, 7, mae_mfe_vol, 10000.0, 42.0, structure_fields)
        second = it.build_trade_payload(position, 7, mae_mfe_vol, 10000.0, 42.0, structure_fields)

        self.assertEqual(first, second)
        self.assertEqual(first["externalId"], str(position.position_id))


class BuildCapitalPayloadTest(unittest.TestCase):
    """Exercises import_trades.build_capital_payload() -- the pure function
    that turns one MT5 balance-operation deal into the exact JSON body sent
    to POST /api/v1/capital. Same "genuinely executable, no MT5 terminal
    needed" property as BuildTradePayloadTest above."""

    def test_positive_amount_maps_to_deposit_with_positive_magnitude(self):
        op = BalanceOperation(
            ticket=555, time=datetime(2026, 9, 1, 8, 30, tzinfo=timezone.utc),
            amount=500.0, comment="Deposit",
        )
        payload = it.build_capital_payload(op)
        self.assertEqual(payload["reason"], it.CAPITAL_REASON_DEPOSIT)
        self.assertEqual(payload["amount"], 500.0)
        self.assertEqual(payload["externalId"], "555")
        self.assertEqual(payload["notes"], "Deposit")

    def test_negative_amount_maps_to_withdraw_with_positive_magnitude(self):
        # NewCapitalRequest.amount is a positive magnitude with `reason`
        # carrying the direction (unlike PROFIT/LOSS, which is signed) --
        # see tm_dashboard's CapitalDashboardHelper.capitalOverTime(), the
        # one place that negates WITHDRAW. A raw MT5 withdrawal deal has a
        # *negative* `profit`, so this must flip to a positive amount here.
        op = BalanceOperation(
            ticket=556, time=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc),
            amount=-200.0, comment="Withdrawal",
        )
        payload = it.build_capital_payload(op)
        self.assertEqual(payload["reason"], it.CAPITAL_REASON_WITHDRAW)
        self.assertEqual(payload["amount"], 200.0)

    def test_date_format_is_space_separated_not_iso(self):
        # NewCapitalRequest.dateTime declares a custom @JsonFormat
        # ("yyyy-MM-dd HH:mm:ss", no 'T') -- different from NewTradeRequest's
        # plain ISO LocalDateTime fields. Sending .isoformat() here would
        # send the wrong wire format and fail server-side deserialization.
        op = BalanceOperation(
            ticket=1, time=datetime(2026, 9, 8, 10, 5, 30, tzinfo=timezone.utc),
            amount=100.0, comment="",
        )
        payload = it.build_capital_payload(op)
        self.assertEqual(payload["dateTime"], "2026-09-08 10:05:30")
        self.assertNotIn("T", payload["dateTime"])

    def test_empty_comment_becomes_none_not_empty_string(self):
        op = BalanceOperation(
            ticket=2, time=datetime(2026, 9, 8, tzinfo=timezone.utc),
            amount=100.0, comment="",
        )
        payload = it.build_capital_payload(op)
        self.assertIsNone(payload["notes"])

    def test_idempotent_rebuild_produces_identical_payload(self):
        # Same guarantee as trades: re-running an overlapping date range must
        # build the exact same payload (in particular the same externalId)
        # so the server's findByExternalIdIn dedupe (CapitalServiceTest,
        # once added) actually catches it as a duplicate.
        op = BalanceOperation(
            ticket=999, time=datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc),
            amount=-75.5, comment="Withdrawal completed",
        )
        first = it.build_capital_payload(op)
        second = it.build_capital_payload(op)
        self.assertEqual(first, second)
        self.assertEqual(first["externalId"], str(op.ticket))


if __name__ == "__main__":
    unittest.main()
