from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.audit_store import AuditStore
from backend.backtest_store import BacktestStore, _in_range
from backend.strategy_store import StrategyStore
from backend.timing_store import TimingStore
from backend.trading_store import TradingStore


class BacktestStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "bt.sqlite3"
        self.audit = AuditStore(self.db_path)
        self.trading = TradingStore(self.db_path, self.audit)
        self.timing = TimingStore(self.db_path, self.audit, self.trading, root / "timing")
        self.strategies = StrategyStore(self.db_path, self.audit, self.trading, root / "strategies", self.timing)
        self.backtest = BacktestStore(self.db_path, self.strategies, self.timing, self.strategies.connectors)
        self.strategies.create_strategy(
            {
                "id": "bt_buyhold",
                "name": "Buy Hold",
                "code": (
                    "def on_bar(ctx, bar):\n"
                    "    if bar['symbol'] == '000001.SZ' and bar['close'] > bar['open']:\n"
                    "        ctx.order_market(bar['symbol'], 100, side='BUY', reason='bt')\n"
                ),
            }
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_in_range_filter(self) -> None:
        self.assertTrue(_in_range("2026-06-10T07:00:00", None, None))
        self.assertTrue(_in_range("2026-06-10T07:00:00", "2026-06-01", "2026-06-30"))
        self.assertFalse(_in_range("2026-05-10", "2026-06-01", None))
        self.assertFalse(_in_range("2026-07-10", None, "2026-06-30"))

    def test_run_backtest_produces_curve_metrics_and_persists(self) -> None:
        result = self.backtest.run(
            {
                "name": "Demo BT",
                "strategy_id": "bt_buyhold",
                "symbols": "000001.SZ",
                "frequency": "1d",
                "data_source": "fixture",
                "initial_cash": 1_000_000,
            }
        )

        self.assertIn("id", result)
        self.assertGreaterEqual(len(result["curve"]), 3)
        self.assertIn("sharpe", result["metrics"])
        self.assertEqual(result["summary"]["initial_cash"], 1_000_000)
        # 起点净值应接近初始资金
        self.assertAlmostEqual(result["curve"][0]["equity"], 1_000_000, delta=1_000_000 * 0.05)
        # 持久化:可列出与取回
        runs = self.backtest.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["id"], result["id"])
        fetched = self.backtest.get_run(result["id"])
        self.assertEqual(fetched["summary"]["total_trades"], result["summary"]["total_trades"])

    def test_friction_reduces_cash_on_trades(self) -> None:
        result = self.backtest.run(
            {
                "strategy_id": "bt_buyhold",
                "symbols": "000001.SZ",
                "frequency": "1d",
                "data_source": "fixture",
                "initial_cash": 1_000_000,
                "commission_rate": 0.001,
                "stamp_duty_rate": 0.001,
                "slippage_value": 10,
            }
        )
        trades = result["trades"]
        self.assertTrue(trades, "应至少有一笔成交")
        buy = next(t for t in trades if t["side"] == "BUY")
        self.assertGreater(buy["commission"], 0)
        self.assertGreater(buy["slippage"], 0)
        self.assertEqual(buy["stamp_duty"], 0)  # 买入无印花税

    def test_backtest_over_historical_date_range(self) -> None:
        # fixture 现在支持任意历史区间:2015 年也能回测。
        result = self.backtest.run(
            {
                "strategy_id": "bt_buyhold",
                "symbols": "000001.SZ",
                "frequency": "1d",
                "data_source": "fixture",
                "start": "2015-03-13",
                "end": "2015-06-13",
                "initial_cash": 1_000_000,
            }
        )
        self.assertGreaterEqual(len(result["curve"]), 3)
        self.assertGreaterEqual(result["curve"][0]["time"], "2015-03-13")
        self.assertLessEqual(result["curve"][-1]["time"], "2015-06-13")
        self.assertEqual(result["summary"]["start"][:4], "2015")

    def test_too_few_points_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "数据点不足"):
            self.backtest.run(
                {
                    "strategy_id": "bt_buyhold",
                    "symbols": "000001.SZ",
                    "frequency": "1d",
                    "data_source": "fixture",
                    "start": "2015-12-31",  # 反向区间(start > end),无数据
                    "end": "2015-01-01",
                }
            )

    def test_timing_gate_blocks_orders_in_backtest(self) -> None:
        self.timing.create_timing_strategy(
            {
                "id": "bt_block",
                "name": "Block",
                "code": "def on_bar(ctx, bar):\n    ctx.set_decision(allow_open=False, position_policy='reduce_only')\n",
            }
        )
        base = {"strategy_id": "bt_buyhold", "symbols": "000001.SZ", "frequency": "1d", "data_source": "fixture", "benchmark": ""}
        no_timing = self.backtest.run({**base})
        with_timing = self.backtest.run({**base, "timing_strategy_id": "bt_block"})
        # 全禁止择时:成交应归零,被拒数等于原成交数。
        self.assertGreater(no_timing["summary"]["total_trades"], 0)
        self.assertEqual(with_timing["summary"]["total_trades"], 0)
        self.assertGreater(with_timing["summary"]["rejected_orders"], 0)
        self.assertGreater(with_timing["summary"]["timing_decisions"], 0)

    def test_failing_timing_raises_not_silently_ignored(self) -> None:
        self.timing.create_timing_strategy(
            {"id": "bt_bad", "name": "Bad", "code": "def on_bar(ctx, bar):\n    raise RuntimeError('boom')\n"}
        )
        with self.assertRaisesRegex(ValueError, "择时策略回测失败"):
            self.backtest.run(
                {"strategy_id": "bt_buyhold", "symbols": "000001.SZ", "frequency": "1d", "data_source": "fixture", "timing_strategy_id": "bt_bad"}
            )

    def test_unknown_strategy_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown strategy_id"):
            self.backtest.run({"strategy_id": "nope", "symbols": "000001.SZ"})


if __name__ == "__main__":
    unittest.main()
