from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.audit_store import AuditStore
from backend.strategy_store import StrategyStore
from backend.trading_store import TradingStore


class StrategyStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "strategy.sqlite3"
        self.audit = AuditStore(self.db_path)
        self.trading = TradingStore(self.db_path, self.audit)
        self.strategy_store = StrategyStore(self.db_path, self.audit, self.trading, root / "strategies")
        self.trading.create_account({"id": "acct_strategy", "name": "Strategy Account", "initial_cash": 1_000_000})
        self.trading.create_sleeve(
            "acct_strategy",
            {
                "id": "sleeve_strategy",
                "name": "Strategy Sleeve",
                "strategy_id": "strategy_test",
                "allocated_cash": 500_000,
            },
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_imported_strategy_records_source_filename(self) -> None:
        self.strategy_store.create_strategy(
            {
                "id": "strategy_file_import",
                "name": "File Import Strategy",
                "source_filename": "alpha_rotation.py",
                "code": """
def on_bar(ctx, bar):
    pass
""",
            }
        )

        event = self.audit.list_events({"event_type": "strategy_imported"})[0]
        self.assertEqual(event["metadata"]["source_filename"], "alpha_rotation.py")

    def test_imported_strategy_runs_in_subprocess_and_places_orders(self) -> None:
        strategy = self.strategy_store.create_strategy(
            {
                "id": "strategy_test",
                "name": "Momentum Test",
                "code": """
def on_bar(ctx, bar):
    if bar["symbol"] == "000001.SZ" and bar["close"] > bar["open"]:
        ctx.order_market(bar["symbol"], 100, side="BUY", reason="unit test momentum")
""",
            }
        )

        result = self.strategy_store.run_strategy(
            strategy["id"],
            {
                "account_id": "acct_strategy",
                "sleeve_id": "sleeve_strategy",
                "symbols": "000001.SZ",
                "frequency": "5m",
                "data_source": "fixture",
                "bar_limit": 4,
            },
        )

        self.assertEqual(result["status"], "completed")
        self.assertGreater(result["orders_submitted"], 0)
        positions = self.trading.list_positions("sleeve_strategy")
        self.assertEqual(positions[0]["symbol"], "000001.SZ")
        self.assertGreater(positions[0]["quantity"], 0)
        chain = self.audit.get_chain(result["source_event_ids"][0])
        self.assertEqual(chain["signal"]["strategy_id"], "strategy_test")
        self.assertEqual(chain["trade"]["event_type"], "trade_filled")
        started = self.audit.list_events({"event_type": "strategy_run_started"})[0]
        self.assertEqual(started["metadata"]["connector"]["name"], "fixture")

    def test_paused_sleeve_refuses_strategy_run(self) -> None:
        strategy = self.strategy_store.create_strategy(
            {
                "id": "strategy_paused_run",
                "name": "Paused Run",
                "code": """
def on_bar(ctx, bar):
    pass
""",
            }
        )
        self.trading.set_sleeve_active("sleeve_strategy", {"active": False})

        with self.assertRaisesRegex(ValueError, "已停用"):
            self.strategy_store.run_strategy(
                strategy["id"],
                {"account_id": "acct_strategy", "sleeve_id": "sleeve_strategy", "bar_limit": 2},
            )

    def test_unknown_data_source_is_rejected(self) -> None:
        strategy = self.strategy_store.create_strategy(
            {
                "id": "strategy_unknown_source",
                "name": "Unknown Source Strategy",
                "code": """
def on_bar(ctx, bar):
    pass
""",
            }
        )

        with self.assertRaisesRegex(ValueError, "unknown data source"):
            self.strategy_store.run_strategy(
                strategy["id"],
                {
                    "account_id": "acct_strategy",
                    "sleeve_id": "sleeve_strategy",
                    "symbols": "000001.SZ",
                    "frequency": "5m",
                    "data_source": "missing",
                    "bar_limit": 2,
                },
            )

    def test_strategy_failure_is_recorded_without_trade(self) -> None:
        strategy = self.strategy_store.create_strategy(
            {
                "id": "strategy_broken",
                "name": "Broken Strategy",
                "code": """
def on_bar(ctx, bar):
    raise RuntimeError("boom")
""",
            }
        )

        result = self.strategy_store.run_strategy(
            strategy["id"],
            {
                "account_id": "acct_strategy",
                "sleeve_id": "sleeve_strategy",
                "symbols": "000001.SZ",
                "frequency": "5m",
                "bar_limit": 2,
            },
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("boom", result["error"])
        failures = self.audit.list_events({"event_type": "strategy_run_failed"})
        self.assertEqual(len(failures), 1)
        self.assertEqual(self.audit.list_events({"ledger_type": "trade"}), [])


if __name__ == "__main__":
    unittest.main()
