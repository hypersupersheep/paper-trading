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

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _order(self, **overrides) -> dict:
        payload = {
            "account_id": "acct_risk",
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

    def test_upsert_account_config_merge_and_update(self) -> None:
        self.risk.upsert_config(
            {"account_id": "acct_risk", "max_order_notional": 100_000, "max_orders_per_day": 10}
        )

        configs = self.risk.list_configs("acct_risk")
        self.assertEqual({config["scope_type"] for config in configs}, {"account"})
        limits = self.risk.resolve_limits("acct_risk")
        self.assertEqual(limits["max_order_notional"], 100_000)
        self.assertEqual(limits["max_orders_per_day"], 10)
        self.assertEqual(limits["sources"]["max_order_notional"], "account:acct_risk")
        self.assertEqual(limits["sources"]["max_orders_per_day"], "account:acct_risk")
        events = self.audit.list_events({"event_type": "risk_config_updated"})
        self.assertEqual(len(events), 1)

        # 重复 upsert 同一账户是更新而不是新增(字段整体覆盖)。
        self.risk.upsert_config({"account_id": "acct_risk", "max_order_notional": 50_000})
        self.assertEqual(len(self.risk.list_configs("acct_risk")), 1)
        self.assertEqual(self.risk.resolve_limits("acct_risk")["max_order_notional"], 50_000)
        self.assertEqual(self.risk.resolve_limits("acct_risk")["max_orders_per_day"], None)

    def test_disabled_config_is_ignored(self) -> None:
        self.risk.upsert_config({"account_id": "acct_risk", "max_order_notional": 1, "enabled": False})
        self.assertIsNone(self.risk.resolve_limits("acct_risk"))
        result = self._order()
        self.assertTrue(result["accepted"])

    def test_max_order_notional_rejects_and_writes_audit_chain(self) -> None:
        self.risk.upsert_config({"account_id": "acct_risk", "max_order_notional": 50_000})

        result = self._order(quantity=600, source_event_id="sig_notional_test")

        self.assertFalse(result["accepted"])
        self.assertEqual(result["risk"]["rule"], "max_order_notional")
        self.assertIn("max_order_notional", result["reason"])
        self.assertEqual(self.trading.list_positions("acct_risk"), [])
        chain = self.audit.get_chain("sig_notional_test")
        self.assertEqual(chain["signal"]["event_type"], "strategy_signal")
        self.assertEqual(chain["risk_decision"]["event_type"], "risk_blocked")
        self.assertEqual(chain["risk_decision"]["metadata"]["rule"], "max_order_notional")
        self.assertEqual(chain["risk_decision"]["metadata"]["limit"], 50_000)
        self.assertEqual(chain["order"]["event_type"], "order_rejected")
        self.assertIsNone(chain["trade"])
        order = self.trading.list_orders({"account_id": "acct_risk"})[0]
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
        self.assertEqual(self.trading.list_positions("acct_risk")[0]["quantity"], 200)

    def test_max_exposure_rejects_buy_over_limit(self) -> None:
        self.risk.upsert_config({"account_id": "acct_risk", "max_exposure": 0.1})

        # 100w 现金买入 20w 市值,敞口 0.2 > 0.1,应拒单。
        result = self._order(quantity=200, signal_price=1000, fill_price=1000)

        self.assertFalse(result["accepted"])
        self.assertEqual(result["risk"]["rule"], "max_exposure")
        self.assertAlmostEqual(result["risk"]["observed"], 0.2)

    def test_min_cash_buffer_rejects_buy_that_drains_cash(self) -> None:
        self.risk.upsert_config({"account_id": "acct_risk", "min_cash_buffer": 950_000})

        # gross 10w + 手续费/滑点后现金约 89.98w < 95w buffer。
        result = self._order(quantity=100, signal_price=1000, fill_price=1000)

        self.assertFalse(result["accepted"])
        self.assertEqual(result["risk"]["rule"], "min_cash_buffer")
        self.assertLess(result["risk"]["observed"], 950_000)

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
                "max_exposure": 0.0001,
            }
        )

        sell = self._order(side="SELL", quantity=100, signal_price=110, fill_price=110)

        self.assertTrue(sell["accepted"])
        self.assertEqual(self.trading.list_positions("acct_risk")[0]["quantity"], 100)

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
                "symbols": "000001.SZ",
                "frequency": "5m",
                "data_source": "fixture",
                "bar_limit": 4,
            },
        )

        self.assertEqual(result["status"], "completed_with_rejections")
        self.assertGreater(len(result["rejections"]), 0)
        self.assertIn("max_order_notional", result["rejections"][0]["reason"])
        self.assertEqual(self.trading.list_positions("acct_risk"), [])
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
        self.assertEqual(self.trading.list_positions("acct_risk"), [])
        blocked = self.audit.list_events({"event_type": "risk_blocked"})
        self.assertGreater(len(blocked), 0)
        rejected = self.audit.list_events({"event_type": "order_rejected"})
        self.assertGreater(len(rejected), 0)
        self.assertIn("max_order_notional", rejected[0]["reason"])

    def test_legacy_sleeve_risk_configs_folded_up_not_dropped(self) -> None:
        """老库升级:sleeve 级风控限额逐字段取最严折叠到账户级,不静默丢失(P1-1)。"""
        import sqlite3

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "legacy_risk.sqlite3"
        audit = AuditStore(db)
        trading = TradingStore(db, audit)
        trading.create_account({"id": "acct_f", "name": "F", "initial_cash": 1_000_000})
        # 手工建老结构(含 max_sleeve_exposure + sleeve scope 行),绕过新 RiskStore。
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE risk_configs (
                id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
                account_id TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                max_order_notional REAL, max_sleeve_exposure REAL, max_symbol_position INTEGER,
                min_cash_buffer REAL, max_orders_per_tick INTEGER, max_orders_per_day INTEGER,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(scope_type, scope_id)
            )
            """
        )
        # 账户级只设了 notional;sleeve 级设了 exposure + cash_buffer(老模型完全合法)。
        conn.execute(
            "INSERT INTO risk_configs VALUES ('r1','account','acct_f','acct_f',1,500000,NULL,NULL,NULL,NULL,NULL,'t','t')"
        )
        conn.execute(
            "INSERT INTO risk_configs VALUES ('r2','sleeve','slv_x','acct_f',1,NULL,0.8,NULL,1000,NULL,NULL,'t','t')"
        )
        # 第二个 sleeve:更严的 exposure 0.5 → 折叠应取 min(0.8, 0.5)=0.5。
        conn.execute(
            "INSERT INTO risk_configs VALUES ('r3','sleeve','slv_y','acct_f',1,NULL,0.5,NULL,NULL,NULL,NULL,'t','t')"
        )
        conn.commit()
        conn.close()

        # 构造 RiskStore 触发迁移(列重命名 + 折叠)。
        risk = RiskStore(db, audit, trading)
        limits = risk.resolve_limits("acct_f")
        self.assertIsNotNone(limits)
        self.assertEqual(limits["max_order_notional"], 500000)  # 账户级保留
        self.assertEqual(limits["max_exposure"], 0.5)  # 两 sleeve 取最严(min)
        self.assertEqual(limits["min_cash_buffer"], 1000)  # sleeve 级提升,未丢
        # sleeve 行已清,只剩账户级。
        configs = risk.list_configs("acct_f")
        self.assertEqual({c["scope_type"] for c in configs}, {"account"})
        # 迁移留痕(可追溯,非静默)。
        migr = audit.list_events({"event_type": "risk_config_migrated", "account_id": "acct_f"})
        self.assertEqual(len(migr), 1)
        self.assertEqual(migr[0]["metadata"]["folded_sleeve_configs"], 2)


if __name__ == "__main__":
    unittest.main()
