from __future__ import annotations

import unittest

from backend.attribution import brinson_fachler, build_brinson_rows


class BrinsonTest(unittest.TestCase):
    def test_decomposition_reconciles_to_excess(self) -> None:
        # 两行业:组合超配行业A且选股更好。配置+选股+交互 应等于 组合-基准。
        rows = [
            {"sector": "A", "wP": 0.7, "wB": 0.5, "rP": 0.10, "rB": 0.06},
            {"sector": "B", "wP": 0.3, "wB": 0.5, "rP": 0.02, "rB": 0.04},
        ]
        out = brinson_fachler(rows)
        self.assertAlmostEqual(
            out["allocation"] + out["selection"] + out["interaction"], out["total_excess"], places=9
        )
        rp = 0.7 * 0.10 + 0.3 * 0.02
        rb = 0.5 * 0.06 + 0.5 * 0.04
        self.assertAlmostEqual(out["portfolio_return"], rp, places=9)
        self.assertAlmostEqual(out["benchmark_return"], rb, places=9)
        self.assertAlmostEqual(out["total_excess"], rp - rb, places=9)

    def test_build_rows_aggregates_by_sector(self) -> None:
        rows = build_brinson_rows(
            holdings={"600519.SH": {"weight": 0.6}, "000001.SZ": {"weight": 0.4}},
            bench_weights={"600519.SH": 0.05, "000001.SZ": 0.03, "600036.SH": 0.04},
            industries={"600519.SH": "食品饮料", "000001.SZ": "银行", "600036.SH": "银行"},
            returns={"600519.SH": 0.10, "000001.SZ": -0.05, "600036.SH": 0.02},
        )
        by = {r["sector"]: r for r in rows}
        # 银行:组合权重=0.4(只持平安);基准权重=0.03+0.04=0.07
        self.assertAlmostEqual(by["银行"]["wP"], 0.4, places=6)
        self.assertAlmostEqual(by["银行"]["wB"], 0.07, places=6)
        # 食品饮料:组合 rP=0.10
        self.assertAlmostEqual(by["食品饮料"]["rP"], 0.10, places=6)

    def test_unheld_benchmark_sector_has_zero_portfolio_weight(self) -> None:
        rows = build_brinson_rows(
            holdings={"600519.SH": {"weight": 1.0}},
            bench_weights={"600519.SH": 0.05, "000001.SZ": 0.03},
            industries={"600519.SH": "食品饮料", "000001.SZ": "银行"},
            returns={"600519.SH": 0.1, "000001.SZ": 0.2},
        )
        bank = next(r for r in rows if r["sector"] == "银行")
        self.assertEqual(bank["wP"], 0.0)  # 没持有银行
        self.assertGreater(bank["wB"], 0.0)


if __name__ == "__main__":
    unittest.main()
