from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent"))

from review import format_review, review_backtest  # noqa: E402


def _result(metrics=None, summary=None, benchmark=None):
    return {"metrics": metrics or {}, "summary": summary or {}, "benchmark": benchmark}


class ReviewTest(unittest.TestCase):
    def test_zero_trades_flagged(self):
        r = review_backtest(_result(
            metrics={"sharpe": 0, "trading_days": 200, "max_drawdown": 0},
            summary={"total_trades": 0, "closed_trades": 0},
        ))
        self.assertTrue(any("零成交" in f for f in r["flags"]))
        self.assertEqual(r["verdict"], "需要改进")

    def test_only_buy_no_sell_flagged(self):
        r = review_backtest(_result(
            metrics={"sharpe": 0.7, "trading_days": 200, "max_drawdown": -0.05},
            summary={"total_trades": 50, "closed_trades": 0},
        ))
        self.assertTrue(any("只买不卖" in f for f in r["flags"]))

    def test_negative_sharpe_flagged(self):
        r = review_backtest(_result(
            metrics={"sharpe": -0.3, "trading_days": 250, "max_drawdown": -0.1},
            summary={"total_trades": 40, "closed_trades": 20, "trade_win_rate": 0.45},
        ))
        self.assertTrue(any("夏普" in f and "为负" in f for f in r["flags"]))
        self.assertEqual(r["verdict"], "需要改进")

    def test_underperforms_benchmark_flagged(self):
        r = review_backtest(_result(
            metrics={"sharpe": 0.6, "trading_days": 250, "max_drawdown": -0.08},
            summary={"total_trades": 30, "closed_trades": 15, "trade_win_rate": 0.5},
            benchmark={"symbol": "000300.SH", "metrics": {"excess_return": -0.08, "beta": 0.2, "information_ratio": -0.5}},
        ))
        self.assertTrue(any("跑输基准" in f for f in r["flags"]))
        self.assertEqual(r["verdict"], "需要改进")

    def test_deep_drawdown_and_high_turnover_flagged(self):
        r = review_backtest(_result(
            metrics={"sharpe": 0.8, "trading_days": 100, "max_drawdown": -0.42},
            summary={"total_trades": 90, "closed_trades": 45, "trade_win_rate": 0.5},
        ))
        self.assertTrue(any("最大回撤" in f for f in r["flags"]))
        self.assertTrue(any("换手过高" in f for f in r["flags"]))

    def test_short_sample_flagged(self):
        r = review_backtest(_result(
            metrics={"sharpe": 1.2, "trading_days": 30, "max_drawdown": -0.05},
            summary={"total_trades": 10, "closed_trades": 5, "trade_win_rate": 0.6},
        ))
        self.assertTrue(any("样本太短" in f for f in r["flags"]))

    def test_good_strategy_verdict(self):
        r = review_backtest(_result(
            metrics={"sharpe": 1.4, "trading_days": 250, "max_drawdown": -0.06, "cumulative_return": 0.25, "annualized_return": 0.25},
            summary={"total_trades": 40, "closed_trades": 20, "trade_win_rate": 0.55},
            benchmark={"symbol": "000300.SH", "metrics": {"excess_return": 0.12, "beta": 0.8, "information_ratio": 0.9}},
        ))
        self.assertEqual(r["verdict"], "表现良好")
        self.assertEqual(r["flags"], [])

    def test_format_is_readable_text(self):
        r = review_backtest(_result(
            metrics={"sharpe": 0.5, "trading_days": 250, "max_drawdown": -0.1},
            summary={"total_trades": 20, "closed_trades": 10, "trade_win_rate": 0.5},
        ))
        text = format_review(r)
        self.assertIn("总评", text)
        self.assertIsInstance(text, str)


if __name__ == "__main__":
    unittest.main()
