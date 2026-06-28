from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.audit_store import AuditStore
from backend.strategy_adapter import adapt_strategy_code
from backend.strategy_store import StrategyStore
from backend.timing_store import TimingStore
from backend.trading_store import TradingStore


FIXTURE_BARS = [
    {"symbol": "000001.SZ", "timestamp": "2026-06-10T09:30:00+00:00", "frequency": "5m",
     "open": 10.0, "high": 10.2, "low": 9.95, "close": 10.12, "volume": 100000},
    {"symbol": "000001.SZ", "timestamp": "2026-06-10T09:35:00+00:00", "frequency": "5m",
     "open": 10.12, "high": 10.3, "low": 10.1, "close": 10.25, "volume": 120000},
]


class AdapterUnitTest(unittest.TestCase):
    def test_native_on_bar_is_unchanged(self) -> None:
        code = "def on_bar(ctx, bar):\n    pass\n"
        adapted = adapt_strategy_code(code)
        self.assertEqual(adapted["mode"], "native")
        self.assertEqual(adapted["code"], code)

    def test_alias_handle_bar_is_delegated(self) -> None:
        adapted = adapt_strategy_code("def handle_bar(ctx, bar):\n    pass\n")
        self.assertEqual(adapted["mode"], "alias_function")
        self.assertEqual(adapted["entry"], "handle_bar")
        self.assertIn("def on_bar(ctx, bar):", adapted["code"])
        self.assertIn("return handle_bar(ctx, bar)", adapted["code"])

    def test_single_function_any_name_is_delegated(self) -> None:
        adapted = adapt_strategy_code("def my_momentum(ctx, bar):\n    pass\n")
        self.assertEqual(adapted["mode"], "single_function")
        self.assertEqual(adapted["entry"], "my_momentum")

    def test_one_arg_function_becomes_signal_strategy(self) -> None:
        adapted = adapt_strategy_code("def decide(bar):\n    return 'BUY'\n")
        self.assertEqual(adapted["mode"], "signal_function")
        self.assertIn("_pt_apply_signal", adapted["code"])
        self.assertIn("decide(bar)", adapted["code"])

    def test_class_strategy_is_instantiated_and_delegated(self) -> None:
        adapted = adapt_strategy_code(
            "class MyStrategy:\n"
            "    def on_init(self, ctx):\n        pass\n"
            "    def on_bar(self, ctx, bar):\n        pass\n"
        )
        self.assertEqual(adapted["mode"], "class_method")
        self.assertEqual(adapted["entry"], "MyStrategy.on_bar")
        self.assertIn("_pt_strategy_instance = MyStrategy()", adapted["code"])
        self.assertIn("def on_init(ctx):", adapted["code"])

    def test_class_with_required_init_args_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "__init__"):
            adapt_strategy_code(
                "class S:\n"
                "    def __init__(self, window):\n        self.window = window\n"
                "    def on_bar(self, ctx, bar):\n        pass\n"
            )

    def test_unadaptable_file_lists_supported_conventions(self) -> None:
        with self.assertRaisesRegex(ValueError, "支持的写法"):
            adapt_strategy_code("def run(df):\n    pass\n\ndef main():\n    pass\n")

    def test_timing_flavor_uses_timing_signal_helper(self) -> None:
        adapted = adapt_strategy_code("def regime(bar):\n    return True\n", flavor="timing")
        self.assertIn("set_decision", adapted["code"])


class AdapterIntegrationTest(unittest.TestCase):
    """适配后的策略必须真的能被 worker 驱动并产生订单/决策。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "adapter.sqlite3"
        self.audit = AuditStore(self.db_path)
        self.trading = TradingStore(self.db_path, self.audit)
        self.timing = TimingStore(self.db_path, self.audit, self.trading, root / "timing")
        self.strategies = StrategyStore(self.db_path, self.audit, self.trading, root / "strategies", self.timing)
        self.trading.create_account({"id": "acct_adapter", "name": "Adapter Account", "initial_cash": 1_000_000})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, strategy_id: str) -> dict:
        return self.strategies.run_strategy(
            strategy_id,
            {"account_id": "acct_adapter", "bars": FIXTURE_BARS, "frequency": "5m"},
        )

    def test_handle_bar_strategy_places_orders(self) -> None:
        self.strategies.create_strategy(
            {
                "id": "strategy_handle_bar",
                "name": "Handle Bar Strategy",
                "code": (
                    "def handle_bar(ctx, bar):\n"
                    "    if bar[\"close\"] > bar[\"open\"]:\n"
                    "        ctx.order_market(bar[\"symbol\"], 100, side=\"BUY\", reason=\"alias adapter\")\n"
                ),
            }
        )
        result = self._run("strategy_handle_bar")
        self.assertEqual(result["status"], "completed")
        self.assertGreater(result["orders_submitted"], 0)
        self.assertGreater(self.trading.list_positions("acct_adapter")[0]["quantity"], 0)

    def test_signal_function_strategy_places_orders(self) -> None:
        self.strategies.create_strategy(
            {
                "id": "strategy_signal",
                "name": "Signal Strategy",
                "code": (
                    "def decide(bar):\n"
                    "    # 信号式：只看 bar，返回方向字符串。\n"
                    "    return \"BUY\" if bar[\"close\"] > bar[\"open\"] else None\n"
                ),
            }
        )
        result = self._run("strategy_signal")
        self.assertEqual(result["status"], "completed")
        self.assertGreater(result["orders_submitted"], 0)

    def test_class_strategy_places_orders(self) -> None:
        self.strategies.create_strategy(
            {
                "id": "strategy_class",
                "name": "Class Strategy",
                "code": (
                    "class Momentum:\n"
                    "    def __init__(self):\n"
                    "        self.count = 0\n"
                    "    def on_bar(self, ctx, bar):\n"
                    "        self.count += 1\n"
                    "        if bar[\"close\"] > bar[\"open\"]:\n"
                    "            ctx.order_market(bar[\"symbol\"], 100, side=\"BUY\", reason=\"class adapter\")\n"
                ),
            }
        )
        result = self._run("strategy_class")
        self.assertEqual(result["status"], "completed")
        self.assertGreater(result["orders_submitted"], 0)

    def test_signal_timing_strategy_records_decisions(self) -> None:
        self.timing.create_timing_strategy(
            {
                "id": "timing_signal",
                "name": "Signal Timing",
                "code": "def regime(bar):\n    return bar[\"close\"] > bar[\"open\"]\n",
            }
        )
        result = self.timing.run_timing_strategy(
            "timing_signal",
            {"account_id": "acct_adapter", "bars": FIXTURE_BARS, "frequency": "5m"},
        )
        self.assertEqual(result["status"], "completed")
        self.assertGreater(result["decisions_recorded"], 0)
        latest = result["latest_decision"]
        self.assertTrue(latest["allow_open"])
        self.assertEqual(latest["position_policy"], "hold")

    def test_adapter_mode_is_recorded_in_audit(self) -> None:
        self.strategies.create_strategy(
            {
                "id": "strategy_audit_adapter",
                "name": "Audit Adapter",
                "code": "def decide(bar):\n    return None\n",
            }
        )
        event = self.audit.list_events({"event_type": "strategy_imported"})[0]
        self.assertEqual(event["metadata"]["adapter_mode"], "signal_function")
        self.assertEqual(event["metadata"]["adapter_entry"], "decide")


if __name__ == "__main__":
    unittest.main()
