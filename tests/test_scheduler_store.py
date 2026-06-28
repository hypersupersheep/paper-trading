from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.audit_store import AuditStore
from backend.scheduler_store import SchedulerStore
from backend.strategy_store import StrategyStore
from backend.timing_store import TimingStore
from backend.trading_store import TradingStore


class SchedulerStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "scheduler.sqlite3"
        self.audit = AuditStore(self.db_path)
        self.trading = TradingStore(self.db_path, self.audit)
        self.timing = TimingStore(self.db_path, self.audit, self.trading, root / "timing")
        self.strategies = StrategyStore(self.db_path, self.audit, self.trading, root / "strategies", self.timing)
        self.scheduler = SchedulerStore(self.db_path, self.audit, self.trading, self.strategies, self.timing)
        self.trading.create_account({"id": "acct_sched", "name": "Scheduler Account", "initial_cash": 1_000_000})
        self.timing_strategy = self.timing.create_timing_strategy(
            {
                "id": "timing_sched",
                "name": "Scheduler Timing",
                "code": """
def on_bar(ctx, bar):
    ctx.set_decision(
        allow_open=True,
        position_policy="hold",
        reason="scheduler unit test allow",
        metadata={"source": "scheduler_test"},
    )
""",
            }
        )
        self.stock_strategy = self.strategies.create_strategy(
            {
                "id": "strategy_sched",
                "name": "Scheduler Strategy",
                "code": """
def on_bar(ctx, bar):
    if bar["symbol"] == "000001.SZ" and bar["close"] > bar["open"]:
        ctx.order_market(bar["symbol"], 100, side="BUY", reason="scheduler unit test buy")
""",
            }
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_task_auto_binds_timing_strategy(self) -> None:
        task = self._create_task("sched_bind")

        self.assertEqual(task["status"], "stopped")
        self.assertEqual(task["symbols"], ["000001.SZ"])
        bindings = self.timing.list_bindings({"timing_strategy_id": "timing_sched", "strategy_id": "strategy_sched"})
        self.assertEqual(len(bindings), 1)
        self.assertTrue(bindings[0]["active"])
        events = self.audit.list_events({"event_type": "scheduler_task_created"})
        self.assertEqual(events[0]["metadata"]["task_id"], "sched_bind")

    def test_tick_once_runs_timing_then_stock_strategy_and_records_audit(self) -> None:
        self._create_task("sched_tick")

        tick = self.scheduler.tick_once("sched_tick", now="2026-06-10T02:00:00+00:00")

        self.assertEqual(tick["status"], "completed")
        self.assertTrue(tick["timing_run_id"].endswith("_timing"))
        self.assertTrue(tick["strategy_run_id"].endswith("_strategy"))
        self.assertGreater(tick["decisions_recorded"], 0)
        self.assertGreater(tick["orders_submitted"], 0)
        task = self.scheduler.get_task("sched_tick")
        self.assertEqual(task["ticks_started"], 1)
        self.assertEqual(task["ticks_completed"], 1)
        self.assertIsNone(task["next_tick_at"])
        positions = self.trading.list_positions("acct_sched")
        self.assertEqual(positions[0]["symbol"], "000001.SZ")
        completed_events = self.audit.list_events({"event_type": "scheduler_tick_completed"})
        self.assertEqual(completed_events[0]["metadata"]["strategy_run_id"], tick["strategy_run_id"])
        self.assertIsNotNone(completed_events[0]["metadata"]["bar_key"])

    def test_duplicate_bar_is_skipped_after_watermark(self) -> None:
        self._create_task("sched_dedupe")
        first = self.scheduler.tick_once("sched_dedupe", now="2026-06-10T02:00:00+00:00")
        second = self.scheduler.tick_once("sched_dedupe", now="2026-06-10T02:01:00+00:00")

        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["skip_reason"], "duplicate_bar")
        task = self.scheduler.get_task("sched_dedupe")
        self.assertEqual(task["ticks_started"], 2)
        self.assertEqual(task["ticks_completed"], 1)
        self.assertEqual(task["ticks_skipped"], 1)
        self.assertEqual(len(self.trading.list_positions("acct_sched")), 1)

    def test_outside_trading_session_is_skipped_without_force(self) -> None:
        self._create_task("sched_calendar")

        tick = self.scheduler.tick_once("sched_calendar", now="2026-06-10T12:00:00+00:00")

        self.assertEqual(tick["status"], "skipped")
        self.assertEqual(tick["skip_reason"], "outside_trading_session")
        self.assertEqual(tick["orders_submitted"], 0)
        self.assertEqual(self.trading.list_positions("acct_sched"), [])
        skipped_events = self.audit.list_events({"event_type": "scheduler_tick_skipped"})
        self.assertEqual(skipped_events[0]["reason"], "outside_trading_session")

    def test_start_and_stop_task_update_status(self) -> None:
        self._create_task("sched_loop", interval_seconds=60)

        running = self.scheduler.start_task("sched_loop")
        stopped = self.scheduler.stop_task("sched_loop")

        self.assertEqual(running["status"], "running")
        self.assertEqual(stopped["status"], "stopped")
        start_events = self.audit.list_events({"event_type": "scheduler_task_started"})
        stop_events = self.audit.list_events({"event_type": "scheduler_task_stopped"})
        self.assertEqual(start_events[0]["metadata"]["task_id"], "sched_loop")
        self.assertEqual(stop_events[0]["metadata"]["task_id"], "sched_loop")

    def _create_task(self, task_id: str, interval_seconds: int = 300) -> dict:
        return self.scheduler.create_task(
            {
                "id": task_id,
                "name": task_id,
                "account_id": "acct_sched",
                "strategy_id": self.stock_strategy["id"],
                "timing_strategy_id": self.timing_strategy["id"],
                "data_source": "fixture",
                "symbols": "000001.SZ",
                "frequency": "5m",
                "interval_seconds": interval_seconds,
                "bar_limit": 2,
            }
        )


if __name__ == "__main__":
    unittest.main()
