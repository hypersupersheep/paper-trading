from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from backend.performance_store import PerformanceStore, _recent_trading_days


class PerformanceStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PerformanceStore(Path(self.tmp.name) / "perf.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_recent_trading_days_skips_weekends(self) -> None:
        days = _recent_trading_days(10)
        self.assertEqual(len(days), 10)
        self.assertEqual(days, sorted(days))  # 升序
        import datetime

        for day in days:
            self.assertLess(datetime.date.fromisoformat(day).weekday(), 5)

    def test_seed_endpoints_at_initial_and_current_equity(self) -> None:
        self.store.seed_demo("acct_x", equity_now=1_100_000, initial_cash=1_000_000, days=60)
        snaps = self.store.list_snapshots("acct_x")
        self.assertEqual(len(snaps), 60)
        self.assertAlmostEqual(snaps[0]["equity"], 1_000_000, delta=1.0)
        self.assertAlmostEqual(snaps[-1]["equity"], 1_100_000, delta=1.0)
        # 二次 seed 不重复
        self.store.seed_demo("acct_x", equity_now=1_100_000, initial_cash=1_000_000, days=60)
        self.assertEqual(len(self.store.list_snapshots("acct_x")), 60)

    def test_same_day_snapshot_overwrites(self) -> None:
        self.store.record_snapshot("a", equity=100, cash=100, market_value=0, pnl=0, pnl_pct=0, trade_date="2026-06-10")
        self.store.record_snapshot("a", equity=200, cash=200, market_value=0, pnl=100, pnl_pct=1, trade_date="2026-06-10")
        snaps = self.store.list_snapshots("a")
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["equity"], 200)

    def test_metrics_on_known_series(self) -> None:
        # 构造可手算的净值序列:100 -> 110 -> 99 -> 120
        equities = [100, 110, 99, 120]
        for i, equity in enumerate(equities):
            self.store.record_snapshot(
                "m", equity=equity, cash=equity, market_value=0, pnl=equity - 100, pnl_pct=equity / 100 - 1,
                trade_date=f"2026-06-{10 + i:02d}",
            )
        result = self.store.compute_metrics("m", initial_cash=100)
        metrics = result["metrics"]
        self.assertEqual(result["points"], 4)
        self.assertAlmostEqual(metrics["cumulative_return"], 0.20, places=4)
        # 最大回撤:110 -> 99 = -10%
        self.assertAlmostEqual(metrics["max_drawdown"], 99 / 110 - 1, places=4)
        # 日胜率:涨/跌/涨 → 2 胜 1 负 = 2/3
        self.assertAlmostEqual(metrics["daily_win_rate"], 2 / 3, places=4)
        self.assertGreater(metrics["sharpe"], 0)  # 正收益序列夏普为正
        self.assertEqual(metrics["trading_days"], 3)
        # 回撤序列末点应回到 0(120 是新高)
        self.assertEqual(result["curve"][-1]["drawdown"], 0.0)

    def test_benchmark_alignment_and_excess_return(self) -> None:
        curve = [
            {"time": "2026-06-08", "equity": 100.0},
            {"time": "2026-06-09", "equity": 110.0},
            {"time": "2026-06-10", "equity": 99.0},
            {"time": "2026-06-11", "equity": 120.0},
        ]
        bench_bars = [
            {"timestamp": "2026-06-08T07:00:00+00:00", "close": 10.0},
            {"timestamp": "2026-06-09T07:00:00+00:00", "close": 11.0},
            {"timestamp": "2026-06-10T07:00:00+00:00", "close": 10.5},
            {"timestamp": "2026-06-11T07:00:00+00:00", "close": 11.0},
        ]
        result = self.store.compute_benchmark(curve, bench_bars, "000300.SH")
        self.assertIsNotNone(result)
        self.assertEqual(len(result["series"]), 4)
        self.assertEqual(result["series"][0]["value"], 100.0)  # 归一化到策略起点
        # 策略累计 +20%，基准累计 +10% → 超额 +10%
        self.assertAlmostEqual(result["metrics"]["excess_return"], 0.10, places=4)
        self.assertAlmostEqual(result["metrics"]["benchmark_cumulative"], 0.10, places=4)
        self.assertIn("beta", result["metrics"])
        self.assertIn("information_ratio", result["metrics"])

    def test_benchmark_uses_nearest_prior_close_for_missing_day(self) -> None:
        curve = [
            {"time": "2026-06-08", "equity": 100.0},
            {"time": "2026-06-09", "equity": 105.0},
            {"time": "2026-06-10", "equity": 110.0},
        ]
        # 基准缺 06-10,应回退用 06-09 的收盘。
        bench_bars = [
            {"timestamp": "2026-06-08", "close": 10.0},
            {"timestamp": "2026-06-09", "close": 10.5},
        ]
        result = self.store.compute_benchmark(curve, bench_bars, "000300.SH")
        self.assertIsNotNone(result)
        self.assertEqual(len(result["series"]), 3)
        self.assertEqual(result["series"][2]["value"], result["series"][1]["value"])

    def test_benchmark_none_when_too_few(self) -> None:
        self.assertIsNone(self.store.compute_benchmark([{"time": "d", "equity": 1}], [{"timestamp": "d", "close": 1}], "X"))

    def test_too_few_points_returns_empty_metrics(self) -> None:
        self.store.record_snapshot("t", equity=100, cash=100, market_value=0, pnl=0, pnl_pct=0, trade_date="2026-06-10")
        result = self.store.compute_metrics("t", initial_cash=100)
        self.assertEqual(result["metrics"]["sharpe"], 0.0)
        self.assertEqual(result["metrics"]["trading_days"], 0)


if __name__ == "__main__":
    unittest.main()
