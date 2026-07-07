from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.audit_store import AuditStore
from backend.strategy_store import StrategyStore
from backend.timing_store import TimingStore
from backend.trading_store import TradingStore


class TimingStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "timing.sqlite3"
        self.audit = AuditStore(self.db_path)
        self.trading = TradingStore(self.db_path, self.audit)
        self.timing = TimingStore(self.db_path, self.audit, self.trading, root / "timing")
        self.strategy_store = StrategyStore(self.db_path, self.audit, self.trading, root / "strategies", self.timing)
        self.trading.create_account({"id": "acct_timing", "name": "Timing Account", "initial_cash": 1_000_000})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_imported_timing_strategy_records_source_filename(self) -> None:
        self.timing.create_timing_strategy(
            {
                "id": "timing_file_import",
                "name": "File Import Timing",
                "source_filename": "market_gate.py",
                "code": """
def on_bar(ctx, bar):
    pass
""",
            }
        )

        event = self.audit.list_events({"event_type": "timing_strategy_imported"})[0]
        self.assertEqual(event["metadata"]["source_filename"], "market_gate.py")

    def test_timing_strategy_run_records_standard_decision(self) -> None:
        timing_strategy = self._create_risk_off_timing_strategy()
        result = self.timing.run_timing_strategy(
            timing_strategy["id"],
            {
                "account_id": "acct_timing",
                "strategy_id": "strategy_timed",
                "symbols": "000001.SZ",
                "frequency": "5m",
                "bar_limit": 2,
                "data_source": "fixture",  # 合成行情,别依赖 tongdaxin 实网
            },
        )

        self.assertEqual(result["status"], "completed")
        self.assertGreater(result["decisions_recorded"], 0)
        latest = result["latest_decision"]
        self.assertFalse(latest["allow_open"])
        self.assertEqual(latest["position_policy"], "reduce_only")
        event = self.audit.get_event(latest["audit_event_id"])
        self.assertEqual(event["event_type"], "timing_decision")
        self.assertEqual(event["strategy_id"], timing_strategy["id"])

    def test_bound_timing_decision_blocks_stock_strategy_buy(self) -> None:
        timing_strategy = self._create_risk_off_timing_strategy()
        stock_strategy = self._create_buy_strategy("strategy_timed")
        self.timing.bind_strategy(
            timing_strategy["id"],
            {
                "strategy_id": stock_strategy["id"],
                "account_id": "acct_timing",
            },
        )
        self.timing.run_timing_strategy(
            timing_strategy["id"],
            {
                "account_id": "acct_timing",
                "strategy_id": stock_strategy["id"],
                "symbols": "000001.SZ",
                "frequency": "5m",
                "bar_limit": 2,
                "data_source": "fixture",
            },
        )

        result = self.strategy_store.run_strategy(
            stock_strategy["id"],
            {
                "account_id": "acct_timing",
                "symbols": "000001.SZ",
                "frequency": "5m",
                "bar_limit": 2,
                "data_source": "fixture",
            },
        )

        self.assertEqual(result["status"], "completed_with_rejections")
        self.assertEqual(self.trading.list_positions("acct_timing"), [])
        chain = self.audit.get_chain(result["source_event_ids"][0])
        self.assertEqual(chain["timing_decision"]["event_type"], "timing_blocked")
        self.assertIsNotNone(chain["timing_decision"]["metadata"]["timing_decision_id"])
        self.assertEqual(chain["order"]["event_type"], "order_rejected")
        self.assertIsNone(chain["trade"])

    def test_unbound_stock_strategy_is_not_gated(self) -> None:
        timing_strategy = self._create_risk_off_timing_strategy()
        bound_strategy = self._create_buy_strategy("strategy_timed")
        free_strategy = self._create_buy_strategy("strategy_free")
        self.timing.bind_strategy(
            timing_strategy["id"],
            {
                "strategy_id": bound_strategy["id"],
                "account_id": "acct_timing",
            },
        )

        result = self.strategy_store.run_strategy(
            free_strategy["id"],
            {
                "account_id": "acct_timing",
                "symbols": "000001.SZ",
                "frequency": "5m",
                "bar_limit": 2,
                "data_source": "fixture",
            },
        )

        self.assertEqual(result["status"], "completed")
        self.assertGreater(result["orders_submitted"], 0)
        positions = self.trading.list_positions("acct_timing")
        self.assertEqual(positions[0]["symbol"], "000001.SZ")

    def _create_risk_off_timing_strategy(self) -> dict:
        return self.timing.create_timing_strategy(
            {
                "id": "timing_risk_off",
                "name": "Risk Off Timing",
                "code": """
def on_bar(ctx, bar):
    ctx.set_decision(
        allow_open=False,
        position_policy="reduce_only",
        reason="unit test market regime risk-off",
        metadata={"source": "unit_test"},
    )
""",
            }
        )

    def _create_buy_strategy(self, strategy_id: str) -> dict:
        return self.strategy_store.create_strategy(
            {
                "id": strategy_id,
                "name": strategy_id,
                "code": """
def on_bar(ctx, bar):
    if bar["symbol"] == "000001.SZ" and bar["close"] > bar["open"]:
        ctx.order_market(bar["symbol"], 100, side="BUY", reason="unit test always buys")
""",
            }
        )


if __name__ == "__main__":
    unittest.main()
