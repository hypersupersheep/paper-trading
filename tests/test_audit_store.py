from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from backend.audit_store import AUDIT_FIELDS, AuditEvent, AuditStore


class AuditStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AuditStore(Path(self.tmp.name) / "audit.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_trade_generates_trade_cash_position_and_portfolio_ledgers(self) -> None:
        signal_id = self.store.record_event(
            AuditEvent(
                event_id="sig_test",
                timestamp="2026-06-10T09:30:00+08:00",
                ledger_type="decision",
                event_type="strategy_signal",
                account_id="acct",
                strategy_id="strategy",
                run_id="run",
                symbol="600519.SH",
            )
        )
        self.store.record_trade_settlement(
            account_id="acct",
            strategy_id="strategy",
            run_id="run",
            symbol="600519.SH",
            side="BUY",
            quantity=100,
            price=100.0,
            timestamp="2026-06-10T09:35:00+08:00",
            source_event_id=signal_id,
            cash_before=100_000,
            position_before=0,
            avg_cost_before=0,
            commission=5,
            stamp_duty=0,
            slippage_cost=2,
        )

        chain = self.store.get_chain("sig_test")
        self.assertEqual(chain["trade"]["event_type"], "trade_filled")
        self.assertEqual(len(chain["cash_changes"]), 4)
        self.assertEqual(
            [event["event_type"] for event in chain["cash_changes"]],
            ["trade_principal", "commission", "stamp_duty", "slippage"],
        )
        self.assertEqual(chain["position_changes"][0]["event_type"], "position_update")
        self.assertEqual(chain["portfolio_snapshot"]["ledger_type"], "portfolio_snapshot")

    def test_cost_components_are_separate_cash_events(self) -> None:
        self.store.record_trade_settlement(
            account_id="acct",
            strategy_id="strategy",
            run_id="run",
            symbol="000001.SZ",
            side="SELL",
            quantity=200,
            price=10.0,
            timestamp="2026-06-10T10:00:00+08:00",
            source_event_id="sig_sell",
            cash_before=10_000,
            position_before=300,
            avg_cost_before=9.5,
            commission=5,
            stamp_duty=1,
            slippage_cost=3,
        )

        event_types = {event["event_type"] for event in self.store.list_events({"ledger_type": "cash"})}
        self.assertIn("commission", event_types)
        self.assertIn("stamp_duty", event_types)
        self.assertIn("slippage", event_types)

    def test_timing_block_is_queryable_in_decision_log(self) -> None:
        self.store.record_event(
            AuditEvent(
                event_id="sig_block",
                timestamp="2026-06-10T09:45:00+08:00",
                ledger_type="decision",
                event_type="timing_blocked",
                account_id="acct",
                strategy_id="timing",
                run_id="run",
                symbol="000858.SZ",
                reason="blocked by timing strategy",
                metadata={"blocked_strategy": "stock_strategy"},
            )
        )

        events = self.store.list_events({"ledger_type": "decision", "event_type": "timing_blocked"})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["metadata"]["blocked_strategy"], "stock_strategy")

    def test_reverse_repo_records_invest_and_interest_cash_ledgers(self) -> None:
        self.store.record_reverse_repo(
            account_id="acct",
            timestamp="2026-06-10T15:00:00+08:00",
            invest_cash=1_000_000,
            annual_rate=0.018,
            cash_before=2_000_000,
            source_event_id="repo_scan",
        )

        event_types = [event["event_type"] for event in self.store.list_events({"ledger_type": "cash"})]
        self.assertIn("reverse_repo_invest", event_types)
        self.assertIn("reverse_repo_interest", event_types)

    def test_export_csv_has_stable_pandas_friendly_fields(self) -> None:
        self.store.record_event(
            AuditEvent(
                event_id="evt_export",
                timestamp="2026-06-10T09:30:00+08:00",
                ledger_type="system",
                event_type="connector_health",
                account_id="acct",
                metadata={"connector": "TongDaXin"},
            )
        )

        content_type, body = self.store.export_events({}, "csv")
        rows = list(csv.DictReader(body.splitlines()))
        self.assertEqual(content_type, "text/csv; charset=utf-8")
        self.assertEqual(list(rows[0].keys()), AUDIT_FIELDS)
        self.assertEqual(json.loads(rows[0]["metadata"])["connector"], "TongDaXin")


if __name__ == "__main__":
    unittest.main()


class BarVolatilityTest(unittest.TestCase):
    def test_volatility_of_constant_series_is_zero(self) -> None:
        from backend.server import _bar_volatility

        self.assertEqual(_bar_volatility([10.0, 10.0, 10.0, 10.0]), 0.0)

    def test_volatility_requires_enough_samples(self) -> None:
        from backend.server import _bar_volatility

        self.assertIsNone(_bar_volatility([10.0, 10.1]))

    def test_volatility_matches_sample_stdev(self) -> None:
        import statistics

        from backend.server import _bar_volatility

        closes = [10.0, 10.2, 10.1, 10.4, 10.3]
        returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        self.assertAlmostEqual(_bar_volatility(closes), round(statistics.stdev(returns), 6))
