from __future__ import annotations

import unittest

from backend import friction


def _bars(n: int, *, volume: float = 1_000_000, close: float = 10.0, high: float = 10.2, low: float = 9.8):
    return [{"symbol": "X", "volume": volume, "close": close, "high": high, "low": low} for _ in range(n)]


class FrictionTest(unittest.TestCase):
    def test_adaptive_square_root_impact_value(self) -> None:
        # ADV=10,000,000;下单额=100,000 → 参与率 0.01;σ=(10.2-9.8)/10=0.04;η=1
        # fraction = 1 * 0.04 * sqrt(0.01) = 0.004 → 成本 = 100000 * 0.004 = 400
        cost = friction.adaptive_slippage_cost(10_000, 10.0, _bars(3), coefficient=1.0)
        self.assertEqual(cost, 400.0)

    def test_adaptive_is_superlinear_in_size(self) -> None:
        small = friction.adaptive_slippage_cost(10_000, 10.0, _bars(3), coefficient=1.0)
        big = friction.adaptive_slippage_cost(40_000, 10.0, _bars(3), coefficient=1.0)
        # 平方根冲击:成本 ∝ 金额^1.5,4 倍规模 → 约 8 倍成本
        self.assertGreater(big, small * 6)

    def test_adaptive_without_volume_falls_back_to_bps(self) -> None:
        bars = [{"symbol": "X", "close": 10.0, "high": 10.2, "low": 9.8}]  # 无 volume
        cost = friction.adaptive_slippage_cost(10_000, 10.0, bars, coefficient=1.0)
        # 退化为 2bps:100000 * 2/10000 = 20
        self.assertEqual(cost, 20.0)

    def test_adaptive_caps_extreme_participation(self) -> None:
        # 超大单(参与率极高)被封顶在 MAX_SLIPPAGE_FRACTION
        cost = friction.adaptive_slippage_cost(100_000_000, 10.0, _bars(3), coefficient=1.0)
        notional = 100_000_000 * 10.0
        self.assertLessEqual(cost, round(notional * friction.MAX_SLIPPAGE_FRACTION, 2))

    def test_dispatch_bps_and_fixed_tick(self) -> None:
        bps = friction.slippage_cost("bps", quantity=100, fill_price=10.0, slippage_value=2.0)
        self.assertEqual(bps, round(100 * 10.0 * 2.0 / 10_000, 2))
        tick = friction.slippage_cost("fixed_tick", quantity=100, fill_price=10.0, slippage_value=0.01)
        self.assertEqual(tick, 1.0)


if __name__ == "__main__":
    unittest.main()
