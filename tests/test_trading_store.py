from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.audit_store import AuditStore
from backend.data_connectors import DataConnectorRegistry
from backend.trading_store import TradingStore


class TradingStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "trading.sqlite3"
        self.audit = AuditStore(self.db_path)
        self.trading = TradingStore(self.db_path, self.audit)
        self.account = self.trading.create_account(
            {
                "id": "acct_test",
                "name": "Test Account",
                "initial_cash": 1_000_000,
                "commission_rate": 0.001,
                "min_commission": 5,
                "stamp_duty_rate": 0.001,
                "slippage_model": "bps",
                "slippage_value": 10,
                "reverse_repo_annual_rate": 0.018,
            }
        )
        self.sleeve = self.trading.create_sleeve(
            "acct_test",
            {
                "id": "sleeve_test",
                "name": "Test Sleeve",
                "strategy_id": "strategy_test",
                "allocated_cash": 500_000,
            },
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_buy_order_updates_cash_position_and_audit_chain(self) -> None:
        result = self.trading.place_order(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "strategy_id": "strategy_test",
                "run_id": "run_test",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 100,
                "signal_price": 100,
                "fill_price": 101,
                "source_event_id": "sig_buy_test",
            }
        )

        self.assertTrue(result["accepted"])
        position = self.trading.list_positions("sleeve_test")[0]
        self.assertEqual(position["quantity"], 100)
        self.assertEqual(position["avg_cost"], 101)
        chain = self.audit.get_chain("sig_buy_test")
        self.assertEqual(chain["order"]["event_type"], "order_submitted")
        self.assertEqual(
            [event["event_type"] for event in chain["order_events"]],
            ["order_created", "order_submitted", "order_filled"],
        )
        self.assertEqual(chain["trade"]["event_type"], "trade_filled")
        self.assertEqual(
            [event["event_type"] for event in chain["cash_changes"]],
            ["trade_principal", "commission", "stamp_duty", "slippage"],
        )
        order = self.trading.get_order(result["order_id"])
        self.assertEqual(order["status"], "filled")
        self.assertEqual(order["filled_quantity"], 100)
        self.assertEqual(order["remaining_quantity"], 0)

    def test_portfolio_summary_marks_cash_positions_and_pnl(self) -> None:
        before = self.trading.get_portfolio_summary("acct_test")["accounts"][0]
        self.assertEqual(before["equity"], 1_000_000)
        self.assertEqual(before["total_cash"], 1_000_000)
        self.assertEqual(before["market_value"], 0)
        self.assertEqual(before["pnl"], 0)

        self.trading.place_order(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "strategy_id": "strategy_test",
                "run_id": "run_test",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 100,
                "signal_price": 100,
                "fill_price": 101,
                "source_event_id": "sig_portfolio_test",
            }
        )

        summary = self.trading.get_portfolio_summary("acct_test")
        account = summary["accounts"][0]
        sleeve = account["sleeves"][0]
        position = account["positions"][0]
        self.assertEqual(account["market_value"], 10_100)
        self.assertEqual(account["equity"], 999_979.8)
        self.assertEqual(account["pnl"], -20.2)
        self.assertEqual(account["exposure"], 0.0101)
        self.assertEqual(sleeve["equity"], 499_979.8)
        self.assertEqual(sleeve["pnl"], -20.2)
        self.assertEqual(position["symbol"], "600519.SH")
        self.assertEqual(position["market_value"], 10_100)
        self.assertEqual(position["unrealized_pnl"], 0)
        self.assertEqual(summary["totals"]["position_count"], 1)

    def test_portfolio_summary_can_mark_positions_from_connector_prices(self) -> None:
        self.trading.place_order(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "strategy_id": "strategy_test",
                "run_id": "run_test",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 100,
                "signal_price": 100,
                "fill_price": 101,
                "source_event_id": "sig_mark_test",
            }
        )

        summary = self.trading.get_portfolio_summary(
            "acct_test",
            mark_prices={
                "600519.SH": {
                    "price": 110,
                    "timestamp": "2026-06-10T10:00:00+00:00",
                    "data_source": "fixture",
                    "frequency": "5m",
                }
            },
            mark_metadata={"mode": "connector_close", "data_source": "fixture", "frequency": "5m"},
        )

        account = summary["accounts"][0]
        sleeve = account["sleeves"][0]
        position = account["positions"][0]
        self.assertEqual(position["last_price"], 101)
        self.assertEqual(position["mark_price"], 110)
        self.assertEqual(position["price_source"], "fixture")
        self.assertEqual(position["market_value"], 11_000)
        self.assertEqual(position["unrealized_pnl"], 900)
        self.assertEqual(sleeve["equity"], 500_879.8)
        self.assertEqual(account["equity"], 1_000_879.8)
        self.assertEqual(account["pnl"], 879.8)
        self.assertEqual(summary["mark"]["mode"], "connector_close")
        self.assertEqual(self.trading.list_position_symbols("acct_test"), ["600519.SH"])

    def test_partial_fill_leaves_open_order_and_can_cancel(self) -> None:
        result = self.trading.place_order(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "strategy_id": "strategy_test",
                "run_id": "run_test",
                "symbol": "000001.SZ",
                "side": "BUY",
                "quantity": 200,
                "signal_price": 10,
                "fill_price": 10,
                "fill_quantity": 100,
                "source_event_id": "sig_partial_test",
            }
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["order_status"], "partially_filled")
        self.assertEqual(result["filled_quantity"], 100)
        self.assertEqual(result["remaining_quantity"], 100)
        self.assertEqual(self.trading.list_positions("sleeve_test")[0]["quantity"], 100)

        open_orders = self.trading.list_orders({"status": "partially_filled"})
        self.assertEqual(open_orders[0]["id"], result["order_id"])
        cancel = self.trading.cancel_order(result["order_id"], {"reason": "unit test cancel"})
        self.assertTrue(cancel["cancelled"])
        self.assertEqual(cancel["order"]["status"], "cancelled")

        chain = self.audit.get_chain("sig_partial_test")
        self.assertEqual(
            [event["event_type"] for event in chain["order_events"]],
            ["order_created", "order_submitted", "order_partially_filled", "order_cancelled"],
        )

    def test_zero_fill_submits_without_trade_and_can_cancel(self) -> None:
        result = self.trading.place_order(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "strategy_id": "strategy_test",
                "run_id": "run_test",
                "symbol": "000001.SZ",
                "side": "BUY",
                "quantity": 100,
                "signal_price": 10,
                "fill_price": 10,
                "fill_quantity": 0,
                "source_event_id": "sig_zero_fill_test",
            }
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["order_status"], "submitted")
        self.assertEqual(result["filled_quantity"], 0)
        self.assertEqual(result["remaining_quantity"], 100)
        self.assertEqual(self.trading.list_positions("sleeve_test"), [])

        chain = self.audit.get_chain("sig_zero_fill_test")
        self.assertIsNone(chain["trade"])
        self.assertEqual(chain["cash_changes"], [])
        self.assertEqual([event["event_type"] for event in chain["order_events"]], ["order_created", "order_submitted"])

        cancel = self.trading.cancel_order(result["order_id"])
        self.assertEqual(cancel["order"]["status"], "cancelled")

    def test_timing_block_rejects_buy_without_position_change(self) -> None:
        result = self.trading.place_order(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "strategy_id": "strategy_test",
                "run_id": "run_test",
                "symbol": "000858.SZ",
                "side": "BUY",
                "quantity": 100,
                "signal_price": 100,
                "fill_price": 100,
                "allow_open": False,
                "timing_strategy_id": "timing_test",
                "source_event_id": "sig_block_test",
            }
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(self.trading.list_positions("sleeve_test"), [])
        chain = self.audit.get_chain("sig_block_test")
        self.assertEqual(chain["timing_decision"]["event_type"], "timing_blocked")
        self.assertEqual(chain["order"]["event_type"], "order_rejected")
        self.assertIsNone(chain["trade"])

    def test_sell_order_records_stamp_duty(self) -> None:
        self.trading.place_order(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "strategy_id": "strategy_test",
                "run_id": "run_test",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 200,
                "signal_price": 100,
                "fill_price": 100,
            }
        )
        result = self.trading.place_order(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "strategy_id": "strategy_test",
                "run_id": "run_test",
                "symbol": "600519.SH",
                "side": "SELL",
                "quantity": 100,
                "signal_price": 110,
                "fill_price": 110,
                "source_event_id": "sig_sell_test",
            }
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["costs"]["stamp_duty"], 11)
        chain = self.audit.get_chain("sig_sell_test")
        stamp_duty = [event for event in chain["cash_changes"] if event["event_type"] == "stamp_duty"][0]
        self.assertEqual(stamp_duty["amount"], -11)

    def test_paused_sleeve_blocks_buy_allows_sell(self) -> None:
        self.trading.place_order(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "strategy_id": "strategy_test",
                "run_id": "run_test",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 200,
                "signal_price": 100,
                "fill_price": 100,
            }
        )
        self.trading.set_sleeve_active("sleeve_test", {"active": False})
        self.assertFalse(self.trading.get_sleeve("sleeve_test")["active"])

        buy = self.trading.place_order(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "strategy_id": "strategy_test",
                "run_id": "run_test",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 100,
                "signal_price": 100,
                "fill_price": 100,
                "source_event_id": "sig_paused_buy",
            }
        )
        sell = self.trading.place_order(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "strategy_id": "strategy_test",
                "run_id": "run_test",
                "symbol": "600519.SH",
                "side": "SELL",
                "quantity": 100,
                "signal_price": 105,
                "fill_price": 105,
            }
        )

        self.assertFalse(buy["accepted"])
        self.assertIn("paused", buy["reason"])
        self.assertTrue(sell["accepted"])
        chain = self.audit.get_chain("sig_paused_buy")
        self.assertEqual(chain["order"]["event_type"], "order_rejected")
        blocked = [e for e in chain["all_events"] if e["event_type"] == "sleeve_paused_blocked"]
        self.assertEqual(len(blocked), 1)

        # 重新启用后恢复正常。
        self.trading.set_sleeve_active("sleeve_test", {"active": True})
        again = self.trading.place_order(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "strategy_id": "strategy_test",
                "run_id": "run_test",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 100,
                "signal_price": 100,
                "fill_price": 100,
            }
        )
        self.assertTrue(again["accepted"])

    def test_adjust_sleeve_allocation_moves_cash_both_ways(self) -> None:
        # 初始: account 100w, sleeve 分配 50w。调到 60%(60w): 未分配 -10w。
        sleeve = self.trading.adjust_sleeve_allocation("sleeve_test", {"percent": 60})
        account = self.trading.get_account("acct_test")
        self.assertEqual(sleeve["allocated_cash"], 600_000)
        self.assertEqual(sleeve["available_cash"], 600_000)
        self.assertEqual(account["unallocated_cash"], 400_000)

        # 降回 40%: 退 20w 回账户。
        sleeve = self.trading.adjust_sleeve_allocation("sleeve_test", {"percent": 40})
        account = self.trading.get_account("acct_test")
        self.assertEqual(sleeve["allocated_cash"], 400_000)
        self.assertEqual(account["unallocated_cash"], 600_000)
        events = self.audit.list_events({"event_type": "sleeve_allocation_adjusted"})
        self.assertEqual(len(events), 2)

    def test_adjust_sleeve_allocation_validates_limits(self) -> None:
        # 目标 120w 超过账户总现金(未分配仅 50w) → 增量不足。
        with self.assertRaisesRegex(ValueError, "未分配现金不足"):
            self.trading.adjust_sleeve_allocation("sleeve_test", {"allocated_cash": 1_200_000})
        self.trading.place_order(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "strategy_id": "strategy_test",
                "run_id": "run_test",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 4900,
                "signal_price": 100,
                "fill_price": 100,
            }
        )
        # 持仓占用 49w 后, 可用现金不足以退回到 0。
        with self.assertRaisesRegex(ValueError, "可退现金不足"):
            self.trading.adjust_sleeve_allocation("sleeve_test", {"percent": 0})
        with self.assertRaisesRegex(ValueError, "percent"):
            self.trading.adjust_sleeve_allocation("sleeve_test", {"percent": 120})

    def test_market_order_without_price_uses_connector_close(self) -> None:
        # 省略价格 = 市价单：按 fixture 最新 5m close 定价(000001.SZ limit=1 时 close=10.12)。
        self.trading.connectors = DataConnectorRegistry()
        result = self.trading.place_order(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "strategy_id": "strategy_test",
                "run_id": "run_test",
                "symbol": "000001.SZ",
                "side": "BUY",
                "quantity": 100,
                "data_source": "fixture",
                "frequency": "5m",
                "source_event_id": "sig_market_price_test",
            }
        )

        self.assertTrue(result["accepted"])
        position = self.trading.list_positions("sleeve_test")[0]
        self.assertEqual(position["quantity"], 100)
        self.assertEqual(position["avg_cost"], 10.12)
        signal = self.audit.get_chain("sig_market_price_test")["signal"]
        self.assertEqual(signal["price"], 10.12)
        self.assertEqual(signal["metadata"]["price_source"], "fixture_close")

    def test_order_without_price_and_connectors_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "price"):
            self.trading.place_order(
                {
                    "account_id": "acct_test",
                    "sleeve_id": "sleeve_test",
                    "strategy_id": "strategy_test",
                    "run_id": "run_test",
                    "symbol": "000001.SZ",
                    "side": "BUY",
                    "quantity": 100,
                }
            )

    def test_reverse_repo_records_to_separate_ledger_and_adds_interest(self) -> None:
        before = self.trading.get_account("acct_test")["unallocated_cash"]
        result = self.trading.run_reverse_repo(
            "acct_test", {"amount": 100_000, "annual_rate": 0.018, "trade_date": "2024-04-01"}
        )
        after = self.trading.get_account("acct_test")["unallocated_cash"]

        self.assertEqual(result["interest"], 4.93)
        self.assertEqual(round(after - before, 2), 4.93)
        # 默认时间 14:30
        self.assertEqual(result["timestamp"][11:16], "14:30")
        # 进独立逆回购账本,不进主审计流水
        repo = self.trading.list_reverse_repo("acct_test")
        self.assertEqual(repo["summary"]["days"], 1)
        self.assertEqual(repo["records"][0]["interest"], 4.93)
        self.assertEqual(repo["records"][0]["source"], "manual")
        repo_events = [e for e in self.audit.list_events({"account_id": "acct_test"}) if "repo" in e["event_type"]]
        self.assertEqual(repo_events, [])


    def test_backfill_buy_updates_position_cash_and_marks_backfill(self) -> None:
        cash_before = self.trading.get_sleeve("sleeve_test")["available_cash"]
        result = self.trading.backfill_trade(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 200,
                "price": 100,
                "trade_date": "2024-03-01",
            }
        )

        self.assertTrue(result["accepted"])
        self.assertTrue(result["backfill"])
        self.assertEqual(result["timestamp"][:10], "2024-03-01")

        position = self.trading.list_positions("sleeve_test")[0]
        self.assertEqual(position["quantity"], 200)
        self.assertEqual(position["avg_cost"], 100)

        # 现金 = 之前 - 本金 20000 - 佣金(20000*0.001=20)
        cash_after = self.trading.get_sleeve("sleeve_test")["available_cash"]
        self.assertEqual(round(cash_before - cash_after, 2), 20_020.0)

        # 订单被标为 backfill,审计链根是补录声明事件
        order = self.trading.get_order(result["order_id"])
        self.assertEqual(order["order_type"], "backfill")
        self.assertEqual(order["status"], "filled")
        self.assertTrue(order["metadata"]["backfill"])
        chain = self.audit.get_chain(result["source_event_id"])
        self.assertIn("trade_backfill_declared", [e["event_type"] for e in chain["all_events"]])
        self.assertEqual(chain["trade"]["event_type"], "trade_filled")

    def test_backfill_requires_price_and_quantity(self) -> None:
        base = {
            "account_id": "acct_test",
            "sleeve_id": "sleeve_test",
            "symbol": "600519.SH",
            "side": "BUY",
            "trade_date": "2024-03-01",
        }
        with self.assertRaises(ValueError):
            self.trading.backfill_trade({**base, "quantity": 100})  # 缺 price
        with self.assertRaises(ValueError):
            self.trading.backfill_trade({**base, "price": 100})  # 缺 quantity
        with self.assertRaises(ValueError):
            self.trading.backfill_trade({**base, "price": 100, "quantity": 100, "trade_date": ""})  # 缺日期

    def test_backfill_sell_cannot_exceed_position(self) -> None:
        with self.assertRaises(ValueError):
            self.trading.backfill_trade(
                {
                    "account_id": "acct_test",
                    "sleeve_id": "sleeve_test",
                    "symbol": "600519.SH",
                    "side": "SELL",
                    "quantity": 100,
                    "price": 100,
                    "trade_date": "2024-03-01",
                }
            )

    def test_place_order_rejects_sell_before_buy_chronology(self) -> None:
        # 复现"凭空造现金"漏洞:先按 6/16 买入,再补一笔时间更早(6/15)的卖出。
        # 卖出应按"当时(6/15)持仓"校验——那时还没买,持仓为 0,必须拒单。
        buy = self.trading.place_order(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "strategy_id": "strategy_test",
                "run_id": "manual_open_buy",
                "symbol": "300066.SZ",
                "side": "BUY",
                "quantity": 200,
                "signal_price": 6.03,
                "fill_price": 6.03,
                "timestamp": "2026-06-16T09:30:00",
                "source_event_id": "sig_backdate_buy",
            }
        )
        self.assertTrue(buy["accepted"])

        sell = self.trading.place_order(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "strategy_id": "strategy_test",
                "run_id": "open_liquidation_monitor",
                "symbol": "300066.SZ",
                "side": "SELL",
                "quantity": 200,
                "signal_price": 16.38,
                "fill_price": 16.38,
                "timestamp": "2026-06-15T09:30:00",
                "source_event_id": "sig_backdate_sell",
            }
        )
        self.assertFalse(sell["accepted"])
        self.assertIn("时序", sell["reason"])
        # 持仓仍为买入的 200 股,不应被这笔非法卖出改写。
        self.assertEqual(self.trading.list_positions("sleeve_test")[0]["quantity"], 200)

    def test_backfill_rejects_sell_before_buy_chronology(self) -> None:
        self.trading.backfill_trade(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "symbol": "300066.SZ",
                "side": "BUY",
                "quantity": 200,
                "price": 6.03,
                "trade_date": "2026-06-16",
            }
        )
        with self.assertRaises(ValueError):
            self.trading.backfill_trade(
                {
                    "account_id": "acct_test",
                    "sleeve_id": "sleeve_test",
                    "symbol": "300066.SZ",
                    "side": "SELL",
                    "quantity": 200,
                    "price": 16.38,
                    "trade_date": "2026-06-15",
                }
            )

    def test_backfill_price_sanity_rejects_absurd_price(self) -> None:
        # 接上 fixture 行情后,补录价偏离当日行情过大(此处约 10 元 vs 录入 9999)应被拦下。
        self.trading.connectors = DataConnectorRegistry()
        with self.assertRaises(ValueError):
            self.trading.backfill_trade(
                {
                    "account_id": "acct_test",
                    "sleeve_id": "sleeve_test",
                    "symbol": "000001.SZ",
                    "side": "BUY",
                    "quantity": 100,
                    "price": 9999,
                    "trade_date": "2026-06-10",
                    "data_source": "fixture",
                }
            )

    def test_backfill_skips_risk_and_timing_gates(self) -> None:
        # 即便不带任何择时/风控字段,补录也应直接成交(它绕过门控)。
        result = self.trading.backfill_trade(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "symbol": "000001.SZ",
                "side": "BUY",
                "quantity": 300,
                "price": 12.5,
                "trade_date": "2023-12-15",
                "trade_time": "10:30",
                "apply_fees": False,
            }
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["costs"]["commission"], 0.0)
        self.assertEqual(result["timestamp"][11:16], "10:30")


    def test_delete_empty_account_removes_it_and_sleeves(self) -> None:
        result = self.trading.delete_account("acct_test")
        self.assertTrue(result["deleted"])
        self.assertEqual(result["removed"]["sleeves"], 1)
        self.assertIsNone(self.trading.get_account("acct_test"))
        self.assertEqual(self.trading.list_sleeves("acct_test"), [])

    def test_delete_account_with_positions_requires_force(self) -> None:
        self.trading.backfill_trade(
            {
                "account_id": "acct_test",
                "sleeve_id": "sleeve_test",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 100,
                "price": 100,
                "trade_date": "2024-01-02",
            }
        )
        with self.assertRaises(ValueError):
            self.trading.delete_account("acct_test")  # 有持仓,默认拒绝
        result = self.trading.delete_account("acct_test", {"force": True})
        self.assertTrue(result["deleted"])
        self.assertEqual(result["removed"]["positions"], 1)
        self.assertIsNone(self.trading.get_account("acct_test"))

    def test_delete_unknown_account_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.trading.delete_account("acct_nope")

    def test_reverse_repo_market_rate_uses_live_quote(self) -> None:
        from backend.data_connectors import DataConnectorRegistry

        self.trading.connectors = DataConnectorRegistry()
        result = self.trading.run_reverse_repo(
            "acct_test", {"amount": 400_000, "rate_mode": "market", "data_source": "fixture", "trade_date": "2024-05-06"}
        )
        # fixture GC001 ~1.8% → 利率约 0.018,来源标记 market
        self.assertGreater(result["annual_rate"], 0.005)
        self.assertLess(result["annual_rate"], 0.05)
        self.assertTrue(result["rate_source"].startswith("market"))
        repo_rec = self.trading.list_reverse_repo("acct_test")["records"][0]
        self.assertTrue(repo_rec["rate_source"].startswith("market"))

    def test_reverse_repo_same_day_is_idempotent_credits_only_delta(self) -> None:
        cash0 = self.trading.get_account("acct_test")["unallocated_cash"]
        r1 = self.trading.run_reverse_repo(
            "acct_test", {"amount": 100_000, "rate_mode": "custom", "annual_rate": 0.018, "trade_date": "2024-06-03"}
        )
        cash1 = self.trading.get_account("acct_test")["unallocated_cash"]
        self.assertEqual(round(cash1 - cash0, 2), r1["interest"])
        # 同一天重做(改利率)→ 只补利息差额,不重复计息;记录仍唯一(每日一条)。
        r2 = self.trading.run_reverse_repo(
            "acct_test", {"amount": 100_000, "rate_mode": "custom", "annual_rate": 0.036, "trade_date": "2024-06-03"}
        )
        cash2 = self.trading.get_account("acct_test")["unallocated_cash"]
        self.assertEqual(round(cash2 - cash0, 2), r2["interest"])
        self.assertTrue(r2["replaced"])
        self.assertEqual(self.trading.list_reverse_repo("acct_test")["summary"]["days"], 1)

    def test_reverse_repo_market_falls_back_to_custom_without_connectors(self) -> None:
        # 没有 connectors 时 market 模式回退到自定义/账户默认,不报错
        result = self.trading.run_reverse_repo(
            "acct_test", {"amount": 100_000, "rate_mode": "market", "annual_rate": 0.02}
        )
        self.assertEqual(result["annual_rate"], 0.02)
        self.assertEqual(result["rate_source"], "custom")

    def test_backfill_without_sleeve_uses_existing_default(self) -> None:
        # 账户已有一个 sleeve(setUp 建的),不指定 sleeve_id 应自动落到它,不新建。
        result = self.trading.backfill_trade(
            {
                "account_id": "acct_test",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 100,
                "price": 100,
                "trade_date": "2024-02-05",
            }
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(len(self.trading.list_sleeves("acct_test")), 1)
        position = self.trading.list_positions("sleeve_test")[0]
        self.assertEqual(position["quantity"], 100)

    def test_backfill_auto_creates_main_sleeve_when_none(self) -> None:
        # 全新账户、无 sleeve:补录时自动建"主仓"并落账,agent 无需关心 sleeve。
        self.trading.create_account({"id": "acct_bare", "name": "Bare", "initial_cash": 500_000})
        result = self.trading.backfill_trade(
            {
                "account_id": "acct_bare",
                "symbol": "000001.SZ",
                "side": "BUY",
                "quantity": 200,
                "price": 12,
                "trade_date": "2024-03-01",
            }
        )
        self.assertTrue(result["accepted"])
        sleeves = self.trading.list_sleeves("acct_bare")
        self.assertEqual(len(sleeves), 1)
        self.assertEqual(sleeves[0]["name"], "主仓")


if __name__ == "__main__":
    unittest.main()
