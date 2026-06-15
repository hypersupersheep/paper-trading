from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.audit_store import AuditStore
from backend.risk_store import RiskStore
from backend.scheduler_store import SchedulerStore
from backend.strategy_store import StrategyStore
from backend.timing_store import TimingStore
from backend.trading_store import TradingStore


class RiskStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "risk.sqlite3"
        self.audit = AuditStore(self.db_path)
        self.trading = TradingStore(self.db_path, self.audit)
        self.risk = RiskStore(self.db_path, self.audit, self.trading)
        # 与 server 装配一致：风控注入 broker，订单提交前自动检查。
        self.trading.risk_store = self.risk
        self.trading.create_account(
            {
                "id": "acct_risk",
                "name": "Risk Account",
                "initial_cash": 1_000_000,
                "commission_rate": 0.001,
                "min_commission": 5,
                "stamp_duty_rate": 0.001,
                "slippage_model": "bps",
                "slippage_value": 10,
            }
        )
        self.trading.create_sleeve(
            "acct_risk",
            {
                "id": "sleeve_risk",
                "name": "Risk Sleeve",
                "strategy_id": "strategy_risk",
                "allocated_cash": 500_000,
            },
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _order(self, **overrides) -> dict:
        payload = {
            "account_id": "acct_risk",
            "sleeve_id": "sleeve_risk",
            "strategy_id": "strategy_risk",
            "run_id": "run_risk",
            "symbol": "600519.SH",
            "side": "BUY",
            "quantity": 100,
            "signal_price": 100,
            "fill_price": 100,
        }
        payload.update(overrides)
        return self.trading.place_order(payload)

    def test_upsert_and_sleeve_override_merge(self) -> None:
        self.risk.upsert_config(
            {"account_id": "acct_risk", "max_order_notional": 100_000, "max_orders_per_day": 10}
        )
        self.risk.upsert_config(
            {"account_id": "acct_risk", "sleeve_id": "sleeve_risk", "max_order_notional": 10_000}
        )

        configs = self.risk.list_configs("acct_risk")
        self.assertEqual({config["scope_type"] for config in configs}, {"account", "sleeve"})
        limits = self.risk.resolve_limits("acct_risk", "sleeve_risk")
        self.assertEqual(limits["max_order_notional"], 10_000)
        self.assertEqual(limits["max_orders_per_day"], 10)
        self.assertEqual(limits["sources"]["max_order_notional"], "sleeve:sleeve_risk")
        self.assertEqual(limits["sources"]["max_orders_per_day"], "account:acct_risk")
        events = self.audit.list_events({"event_type": "risk_config_updated"})
        self.assertEqual(len(events), 2)

        # 重复 upsert 同一 scope 是更新而不是新增。
        self.risk.upsert_config({"account_id": "acct_risk", "max_order_notional": 50_000})
        self.assertEqual(len(self.risk.list_configs("acct_risk")), 2)
        self.assertEqual(self.risk.resolve_limits("acct_risk", "sleeve_risk")["max_orders_per_day"], None)

    def test_disabled_config_is_ignored(self) -> None:
        self.risk.upsert_config({"account_id": "acct_risk", "max_order_notional": 1, "enabled": False})
        self.assertIsNone(self.risk.resolve_limits("acct_risk", "sleeve_risk"))
        result = self._order()
        self.assertTrue(result["accepted"])

    def test_max_order_notional_rejects_and_writes_audit_chain(self) -> None:
        self.risk.upsert_config({"account_id": "acct_risk", "max_order_notional": 50_000})

        result = self._order(quantity=600, source_event_id="sig_notional_test")

        self.assertFalse(result["accepted"])
        self.assertEqual(result["risk"]["rule"], "max_order_notional")
        self.assertIn("max_order_notional", result["reason"])
        self.assertEqual(self.trading.list_positions("sleeve_risk"), [])
        chain = self.audit.get_chain("sig_notional_test")
        self.assertEqual(chain["signal"]["event_type"], "strategy_signal")
        self.assertEqual(chain["risk_decision"]["event_type"], "risk_blocked")
        self.assertEqual(chain["risk_decision"]["metadata"]["rule"], "max_order_notional")
        self.assertEqual(chain["risk_decision"]["metadata"]["limit"], 50_000)
        self.assertEqual(chain["order"]["event_type"], "order_rejected")
        self.assertIsNone(chain["trade"])
        order = self.trading.list_orders({"sleeve_id": "sleeve_risk"})[0]
        self.assertEqual(order["status"], "rejected")
        self.assertIn("max_order_notional", order["reason"])

    def test_max_symbol_position_rejects_buy_over_limit(self) -> None:
        self.risk.upsert_config({"account_id": "acct_risk", "max_symbol_position": 200})

        first = self._order(quantity=200)
        second = self._order(quantity=100, source_event_id="sig_position_test")

        self.assertTrue(first["accepted"])
        self.assertFalse(second["accepted"])
        self.assertEqual(second["risk"]["rule"], "max_symbol_position")
        self.assertEqual(second["risk"]["observed"], 300)
        self.assertEqual(self.trading.list_positions("sleeve_risk")[0]["quantity"], 200)

    def test_max_sleeve_exposure_rejects_buy_over_limit(self) -> None:
        self.risk.upsert_config({"account_id": "acct_risk", "max_sleeve_exposure": 0.1})

        # 50w 现金买入 10w 市值，敞口 0.2 > 0.1，应拒单。
        result = self._order(quantity=100, signal_price=1000, fill_price=1000)

        self.assertFalse(result["accepted"])
        self.assertEqual(result["risk"]["rule"], "max_sleeve_exposure")
        self.assertAlmostEqual(result["risk"]["observed"], 0.2)

    def test_min_cash_buffer_rejects_buy_that_drains_cash(self) -> None:
        self.risk.upsert_config({"account_id": "acct_risk", "min_cash_buffer": 450_000})

        # gross 10w + 手续费/滑点后剩约 39.98w < 45w buffer。
        result = self._order(quantity=100, signal_price=1000, fill_price=1000)

        self.assertFalse(result["accepted"])
        self.assertEqual(result["risk"]["rule"], "min_cash_buffer")
        self.assertLess(result["risk"]["observed"], 450_000)

    def test_max_orders_per_tick_counts_same_run(self) -> None:
        self.risk.upsert_config({"account_id": "acct_risk", "max_orders_per_tick": 1})

        first = self._order(run_id="run_tick_1")
        second = self._order(run_id="run_tick_1", symbol="000001.SZ", signal_price=10, fill_price=10)
        other_run = self._order(run_id="run_tick_2", symbol="000002.SZ", signal_price=12, fill_price=12)

        self.assertTrue(first["accepted"])
        self.assertFalse(second["accepted"])
        self.assertEqual(second["risk"]["rule"], "max_orders_per_tick")
        # 不同 run(tick) 重新计数。
        self.assertTrue(other_run["accepted"])

    def test_max_orders_per_day_counts_cn_trade_date(self) -> None:
        self.risk.upsert_config({"account_id": "acct_risk", "max_orders_per_day": 2})

        # 固定时间戳, 避免依赖运行当天的系统时间。
        day_one = "2026-06-11T09:31:00+08:00"
        first = self._order(run_id="run_day_1", timestamp=day_one)
        second = self._order(run_id="run_day_2", symbol="000001.SZ", signal_price=10, fill_price=10, timestamp=day_one)
        third = self._order(run_id="run_day_3", symbol="000002.SZ", signal_price=12, fill_price=12, timestamp=day_one)

        self.assertTrue(first["accepted"])
        self.assertTrue(second["accepted"])
        self.assertFalse(third["accepted"])
        self.assertEqual(third["risk"]["rule"], "max_orders_per_day")
        # 被拒订单不占当日额度：换一个交易日(CN 日期不同) 应重新放行。
        next_day = self._order(
            run_id="run_day_4",
            symbol="000002.SZ",
            signal_price=12,
            fill_price=12,
            timestamp="2026-06-12T09:31:00+08:00",
        )
        self.assertTrue(next_day["accepted"])

    def test_sell_skips_open_risk_checks(self) -> None:
        buy = self._order(quantity=200)
        self.assertTrue(buy["accepted"])
        # 持仓上限/现金缓冲/敞口都是开仓方向检查，SELL 应放行。
        self.risk.upsert_config(
            {
                "account_id": "acct_risk",
                "max_symbol_position": 100,
                "min_cash_buffer": 10_000_000,
                "max_sleeve_exposure": 0.0001,
            }
        )

        sell = self._order(side="SELL", quantity=100, signal_price=110, fill_price=110)

        self.assertTrue(sell["accepted"])
        self.assertEqual(self.trading.list_positions("sleeve_risk")[0]["quantity"], 100)

    def test_strategy_run_orders_are_risk_blocked(self) -> None:
        strategy_store = StrategyStore(self.db_path, self.audit, self.trading, self.root / "strategies")
        strategy = strategy_store.create_strategy(
            {
                "id": "strategy_risk",
                "name": "Risk Gate Strategy",
                "code": """
def on_bar(ctx, bar):
    if bar["symbol"] == "000001.SZ" and bar["close"] > bar["open"]:
        ctx.order_market(bar["symbol"], 100, side="BUY", reason="risk gate unit test buy")
""",
            }
        )
        self.risk.upsert_config({"account_id": "acct_risk", "max_order_notional": 100})

        result = strategy_store.run_strategy(
            strategy["id"],
            {
                "account_id": "acct_risk",
                "sleeve_id": "sleeve_risk",
                "symbols": "000001.SZ",
                "frequency": "5m",
                "data_source": "fixture",
                "bar_limit": 4,
            },
        )

        self.assertEqual(result["status"], "completed_with_rejections")
        self.assertGreater(len(result["rejections"]), 0)
        self.assertIn("max_order_notional", result["rejections"][0]["reason"])
        self.assertEqual(self.trading.list_positions("sleeve_risk"), [])
        blocked = self.audit.list_events({"event_type": "risk_blocked"})
        self.assertGreater(len(blocked), 0)
        chain = self.audit.get_chain(blocked[0]["source_event_id"])
        self.assertEqual(chain["signal"]["strategy_id"], "strategy_risk")
        self.assertEqual(chain["risk_decision"]["event_type"], "risk_blocked")
        self.assertEqual(chain["order"]["event_type"], "order_rejected")

    def test_scheduler_tick_orders_are_risk_blocked(self) -> None:
        timing_store = TimingStore(self.db_path, self.audit, self.trading, self.root / "timing")
        strategy_store = StrategyStore(self.db_path, self.audit, self.trading, self.root / "strategies", timing_store)
        scheduler = SchedulerStore(self.db_path, self.audit, self.trading, strategy_store, timing_store)
        strategy_store.create_strategy(
            {
                "id": "strategy_risk",
                "name": "Scheduler Risk Strategy",
                "code": """
def on_bar(ctx, bar):
    if bar["symbol"] == "000001.SZ" and bar["close"] > bar["open"]:
        ctx.order_market(bar["symbol"], 100, side="BUY", reason="scheduler risk gate buy")
""",
            }
        )
        scheduler.create_task(
            {
                "id": "sched_risk",
                "name": "Risk Gate Tick",
                "account_id": "acct_risk",
                "sleeve_id": "sleeve_risk",
                "strategy_id": "strategy_risk",
                "data_source": "fixture",
                "symbols": "000001.SZ",
                "frequency": "5m",
                "interval_seconds": 300,
                "bar_limit": 2,
            }
        )
        self.risk.upsert_config({"account_id": "acct_risk", "max_order_notional": 100})

        tick = scheduler.tick_once("sched_risk", now="2026-06-10T02:00:00+00:00")

        self.assertEqual(tick["status"], "completed")
        self.assertEqual(self.trading.list_positions("sleeve_risk"), [])
        blocked = self.audit.list_events({"event_type": "risk_blocked"})
        self.assertGreater(len(blocked), 0)
        rejected = self.audit.list_events({"event_type": "order_rejected"})
        self.assertGreater(len(rejected), 0)
        self.assertIn("max_order_notional", rejected[0]["reason"])


if __name__ == "__main__":
    unittest.main()
