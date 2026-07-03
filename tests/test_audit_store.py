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

    def test_trade_summaries_start_filter_folds_only_that_window(self) -> None:
        # 当日盈亏优化依赖:trade_summaries 传 start 时只折该时点之后的成交,且每笔 SELL 的
        # 已实现盈亏仍能由其自身链(持仓子事件里的 avg_cost)算出,不需要折全账本。
        self.store.record_trade_settlement(
            account_id="acct", strategy_id="s", run_id="r", symbol="600000.SH", side="SELL",
            quantity=100, price=12.0, timestamp="2026-07-02T14:00:00+08:00",  # 昨天卖
            source_event_id="sig_yday", cash_before=50_000, position_before=100, avg_cost_before=10.0,
            commission=0, stamp_duty=0, slippage_cost=0,
        )
        self.store.record_trade_settlement(
            account_id="acct", strategy_id="s", run_id="r", symbol="600000.SH", side="SELL",
            quantity=100, price=15.0, timestamp="2026-07-03T10:00:00+08:00",  # 今天卖
            source_event_id="sig_today", cash_before=51_200, position_before=100, avg_cost_before=10.0,
            commission=0, stamp_duty=0, slippage_cost=0,
        )
        full = [r for r in self.store.trade_summaries({"account_id": "acct"}) if r.get("kind") == "trade"]
        today = [r for r in self.store.trade_summaries({"account_id": "acct", "start": "2026-07-03"}) if r.get("kind") == "trade"]
        self.assertEqual(len(full), 2)
        self.assertEqual(len(today), 1)  # start=today 只折出今天那笔
        self.assertEqual(today[0]["timestamp"][:10], "2026-07-03")
        # 今天那笔的已实现 = 100*(15-10) = 500,与全量折叠里同一笔一致(链自足,不受过滤影响)
        self.assertEqual(today[0]["realized_pnl"], 500.0)
        same = next(r for r in full if r["timestamp"][:10] == "2026-07-03")
        self.assertEqual(same["realized_pnl"], today[0]["realized_pnl"])

    def test_count_events_matches_manual_count(self) -> None:
        for i in range(3):
            self.store.record_trade_settlement(
                account_id="acct", strategy_id="s", run_id="r", symbol="000001.SZ", side="BUY",
                quantity=100, price=10.0, timestamp=f"2026-07-03T10:0{i}:00+08:00",
                source_event_id=None, cash_before=1_000_000, position_before=0, avg_cost_before=0,
                commission=1, stamp_duty=0, slippage_cost=0,
            )
        self.assertEqual(self.store.count_events("trade_filled", "acct"), 3)
        self.assertEqual(self.store.count_events("trade_filled", "other"), 0)

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
