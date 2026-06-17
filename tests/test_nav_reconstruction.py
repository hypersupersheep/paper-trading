from __future__ import annotations

import unittest

from backend.nav_reconstruction import prev_trading_day, reconstruct


class NavReconstructionTest(unittest.TestCase):
    def test_empty_without_fills(self) -> None:
        out = reconstruct(
            initial_cash=1_000_000, fills=[], cash_flows=[], daily_closes={},
            repo_annual_rate=0.018, today="2024-01-05",
        )
        self.assertEqual(out["curve"], [])
        self.assertIsNone(out["start_date"])

    def test_marks_positions_daily_no_repo(self) -> None:
        out = reconstruct(
            initial_cash=1_000_000,
            fills=[{"timestamp": "2024-01-01T09:30:00+08:00", "symbol": "X", "side": "BUY", "quantity": 1000, "price": 10}],
            cash_flows=[{"timestamp": "2024-01-01T09:30:00+08:00", "amount": -10_000}],
            daily_closes={"X": {"2024-01-01": 10.0, "2024-01-02": 11.0}},
            repo_annual_rate=0.0,  # 关掉逆回购,纯盯市
            today="2024-01-02",
        )
        curve = out["curve"]
        self.assertEqual(out["start_date"], "2024-01-01")
        self.assertEqual(len(curve), 2)  # 周一、周二
        # day1: 现金 990000 + 持仓 1000*10 = 1,000,000
        self.assertEqual(curve[0]["equity"], 1_000_000.0)
        # day2: 现金 990000 + 持仓 1000*11 = 1,001,000
        self.assertEqual(curve[1]["equity"], 1_001_000.0)

    def test_repo_accrues_on_idle_cash(self) -> None:
        out = reconstruct(
            initial_cash=1_000_000,
            fills=[{"timestamp": "2024-01-01T09:30:00+08:00", "symbol": "X", "side": "BUY", "quantity": 1000, "price": 10}],
            cash_flows=[{"timestamp": "2024-01-01T09:30:00+08:00", "amount": -10_000}],
            daily_closes={"X": {"2024-01-01": 10.0, "2024-01-02": 10.0}},
            repo_annual_rate=0.018,
            today="2024-01-02",
        )
        schedule = out["repo_schedule"]
        # 只计提"当日以前":today=2024-01-02 当天不计提(盘后才成交),仅 day1(01-01)进计划。
        self.assertEqual(len(schedule), 1)
        self.assertNotIn("2024-01-02", [s["trade_date"] for s in schedule])
        # day1 闲置现金 990000,到周二 gap=1 → 利息 990000*0.018/365 = 48.82
        self.assertEqual(schedule[0]["principal"], 990_000.0)
        self.assertEqual(schedule[0]["interest"], round(990_000 * 0.018 / 365, 2))
        # day1 equity = 990000 + 48.82(利息) + 10000(持仓) = 1,000,048.82
        self.assertEqual(out["curve"][0]["equity"], round(990_000 + schedule[0]["interest"] + 10_000, 2))

    def test_weekend_gap_earns_more_interest(self) -> None:
        # 2024-01-05 是周五;到下个交易日周一 gap=3 天
        out = reconstruct(
            initial_cash=1_000_000,
            fills=[{"timestamp": "2024-01-05T09:30:00+08:00", "symbol": "X", "side": "BUY", "quantity": 100, "price": 10}],
            cash_flows=[{"timestamp": "2024-01-05T09:30:00+08:00", "amount": -1_000}],
            daily_closes={"X": {"2024-01-05": 10.0, "2024-01-08": 10.0}},
            repo_annual_rate=0.018,
            today="2024-01-08",
        )
        fri = out["repo_schedule"][0]
        self.assertEqual(fri["trade_date"], "2024-01-05")
        # 周五利息按 3 天计:999000*0.018*3/365
        self.assertEqual(fri["interest"], round(999_000 * 0.018 * 3 / 365, 2))


    def test_prev_trading_day_skips_weekend(self) -> None:
        self.assertEqual(prev_trading_day("2024-01-09"), "2024-01-08")  # 周二 -> 周一
        self.assertEqual(prev_trading_day("2024-01-08"), "2024-01-05")  # 周一 -> 上周五


if __name__ == "__main__":
    unittest.main()
