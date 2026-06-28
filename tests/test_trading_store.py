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

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_buy_order_updates_cash_position_and_audit_chain(self) -> None:
        result = self.trading.place_order(
            {
                "account_id": "acct_test",
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
        position = self.trading.list_positions("acct_test")[0]
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
        position = account["positions"][0]
        self.assertEqual(account["market_value"], 10_100)
        self.assertEqual(account["equity"], 999_979.8)
        self.assertEqual(account["pnl"], -20.2)
        self.assertEqual(account["exposure"], 0.0101)
        self.assertEqual(account["cash"], 989_879.8)
        self.assertEqual(account["total_cash"], 989_879.8)
        self.assertEqual(position["symbol"], "600519.SH")
        self.assertEqual(position["market_value"], 10_100)
        self.assertEqual(position["unrealized_pnl"], 0)
        self.assertEqual(summary["totals"]["position_count"], 1)

    def test_portfolio_summary_can_mark_positions_from_connector_prices(self) -> None:
        self.trading.place_order(
            {
                "account_id": "acct_test",
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
        position = account["positions"][0]
        self.assertEqual(position["last_price"], 101)
        self.assertEqual(position["mark_price"], 110)
        self.assertEqual(position["price_source"], "fixture")
        self.assertEqual(position["market_value"], 11_000)
        self.assertEqual(position["unrealized_pnl"], 900)
        self.assertEqual(account["equity"], 1_000_879.8)
        self.assertEqual(account["pnl"], 879.8)
        self.assertEqual(summary["mark"]["mode"], "connector_close")
        self.assertEqual(self.trading.list_position_symbols("acct_test"), ["600519.SH"])

    def test_partial_fill_leaves_open_order_and_can_cancel(self) -> None:
        result = self.trading.place_order(
            {
                "account_id": "acct_test",
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
        self.assertEqual(self.trading.list_positions("acct_test")[0]["quantity"], 100)

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
        self.assertEqual(self.trading.list_positions("acct_test"), [])

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
        self.assertEqual(self.trading.list_positions("acct_test"), [])
        chain = self.audit.get_chain("sig_block_test")
        self.assertEqual(chain["timing_decision"]["event_type"], "timing_blocked")
        self.assertEqual(chain["order"]["event_type"], "order_rejected")
        self.assertIsNone(chain["trade"])

    def test_sell_order_records_stamp_duty(self) -> None:
        self.trading.place_order(
            {
                "account_id": "acct_test",
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

    def test_market_order_without_price_uses_connector_close(self) -> None:
        # 省略价格 = 市价单：按 fixture 最新 5m close 定价(000001.SZ limit=1 时 close=10.12)。
        self.trading.connectors = DataConnectorRegistry()
        result = self.trading.place_order(
            {
                "account_id": "acct_test",
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
        position = self.trading.list_positions("acct_test")[0]
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
                    "strategy_id": "strategy_test",
                    "run_id": "run_test",
                    "symbol": "000001.SZ",
                    "side": "BUY",
                    "quantity": 100,
                }
            )

    def test_reverse_repo_records_to_separate_ledger_and_adds_interest(self) -> None:
        before = self.trading.get_account("acct_test")["cash"]
        result = self.trading.run_reverse_repo(
            "acct_test", {"amount": 100_000, "annual_rate": 0.018, "trade_date": "2024-04-01"}
        )
        after = self.trading.get_account("acct_test")["cash"]

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
        cash_before = self.trading.get_account("acct_test")["cash"]
        result = self.trading.backfill_trade(
            {
                "account_id": "acct_test",
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

        position = self.trading.list_positions("acct_test")[0]
        self.assertEqual(position["quantity"], 200)
        self.assertEqual(position["avg_cost"], 100)

        # 现金 = 之前 - 本金 20000 - 佣金(20000*0.001=20)
        cash_after = self.trading.get_account("acct_test")["cash"]
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
        self.assertEqual(self.trading.list_positions("acct_test")[0]["quantity"], 200)

    def test_backfill_rejects_sell_before_buy_chronology(self) -> None:
        self.trading.backfill_trade(
            {
                "account_id": "acct_test",
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


    def test_delete_empty_account_removes_it(self) -> None:
        result = self.trading.delete_account("acct_test")
        self.assertTrue(result["deleted"])
        self.assertEqual(result["removed"]["positions"], 0)
        self.assertIsNone(self.trading.get_account("acct_test"))

    def test_delete_account_with_positions_requires_force(self) -> None:
        self.trading.backfill_trade(
            {
                "account_id": "acct_test",
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
        cash0 = self.trading.get_account("acct_test")["cash"]
        r1 = self.trading.run_reverse_repo(
            "acct_test", {"amount": 100_000, "rate_mode": "custom", "annual_rate": 0.018, "trade_date": "2024-06-03"}
        )
        cash1 = self.trading.get_account("acct_test")["cash"]
        self.assertEqual(round(cash1 - cash0, 2), r1["interest"])
        # 同一天重做(改利率)→ 只补利息差额,不重复计息;记录仍唯一(每日一条)。
        r2 = self.trading.run_reverse_repo(
            "acct_test", {"amount": 100_000, "rate_mode": "custom", "annual_rate": 0.036, "trade_date": "2024-06-03"}
        )
        cash2 = self.trading.get_account("acct_test")["cash"]
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

    def test_trade_summaries_collapse_chain_and_realized_pnl(self) -> None:
        # 一买一卖 → 折叠成两行(各带名称),卖出行结转已实现盈亏;个股看台汇总。
        self.trading.place_order(
            {
                "account_id": "acct_test",
                "strategy_id": "strategy_test",
                "run_id": "run_buy",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 100,
                "signal_price": 100,
                "fill_price": 100,
                "source_event_id": "sig_sum_buy",
            }
        )
        self.trading.place_order(
            {
                "account_id": "acct_test",
                "strategy_id": "strategy_test",
                "run_id": "run_sell",
                "symbol": "600519.SH",
                "side": "SELL",
                "quantity": 100,
                "signal_price": 120,
                "fill_price": 120,
                "source_event_id": "sig_sum_sell",
            }
        )

        summaries = self.audit.trade_summaries({"account_id": "acct_test"})
        trades = [s for s in summaries if s["kind"] == "trade" and s["symbol"] == "600519.SH"]
        self.assertEqual(len(trades), 2)
        for s in trades:
            self.assertEqual(s["name"], "贵州茅台")  # 代码之外标注名称
        buy = next(s for s in trades if s["side"] == "BUY")
        sell = next(s for s in trades if s["side"] == "SELL")
        self.assertIsNone(buy["realized_pnl"])  # 买入不结转盈亏
        self.assertIsNotNone(sell["realized_pnl"])
        self.assertGreater(sell["realized_pnl"], 0)  # 100→120 获利(扣费后仍为正)
        self.assertLess(sell["realized_pnl"], 2000)  # 已扣手续费/印花/滑点

        board = self.audit.realized_pnl_by_symbol({"account_id": "acct_test"})
        row = next(r for r in board["symbols"] if r["symbol"] == "600519.SH")
        self.assertEqual(row["name"], "贵州茅台")
        self.assertEqual(row["buy_quantity"], 100)
        self.assertEqual(row["sell_quantity"], 100)
        self.assertEqual(row["realized_pnl"], sell["realized_pnl"])
        self.assertEqual(board["total_realized_pnl"], sell["realized_pnl"])

    def test_strategy_description_text_and_files(self) -> None:
        # 文字
        self.trading.set_description("acct_test", "双均线择时 + 行业中性")
        d = self.trading.get_description("acct_test")
        self.assertEqual(d["description"], "双均线择时 + 行业中性")
        self.assertEqual(d["files"], [])
        self.assertEqual(self.trading.get_portfolio_summary("acct_test")["accounts"][0]["description"], "双均线择时 + 行业中性")
        # 上传文件
        meta = self.trading.add_file("acct_test", "说明.md", b"# strategy\n20/60 cross")
        self.assertEqual(meta["content_type"], "text/markdown; charset=utf-8")
        self.assertEqual(self.trading.get_file("acct_test", meta["id"])["content"], b"# strategy\n20/60 cross")
        self.assertEqual(len(self.trading.list_files("acct_test")), 1)
        # 类型/大小校验
        with self.assertRaises(ValueError):
            self.trading.add_file("acct_test", "x.exe", b"MZ")
        with self.assertRaises(ValueError):
            self.trading.add_file("acct_test", "big.pdf", b"x" * (26 * 1024 * 1024))
        # 删除
        self.assertTrue(self.trading.delete_file("acct_test", meta["id"])["deleted"])
        self.assertEqual(self.trading.list_files("acct_test"), [])

    def test_account_owner_defaults_to_name_and_is_editable(self) -> None:
        # 不传 owner → 回退账户名
        a = self.trading.create_account({"id": "acct_o1", "name": "Alice 主账户", "initial_cash": 1_000_000})
        self.assertEqual(a["owner"], "Alice 主账户")
        # 传 owner → 用之
        b = self.trading.create_account(
            {"id": "acct_o2", "name": "策略B", "owner": "Bob", "initial_cash": 1_000_000}
        )
        self.assertEqual(b["owner"], "Bob")
        # 可改 owner
        c = self.trading.update_account("acct_o2", {"owner": "Bob Chen"})
        self.assertEqual(c["owner"], "Bob Chen")
        # 组合概览也带 owner
        summary = self.trading.get_portfolio_summary("acct_o2")
        self.assertEqual(summary["accounts"][0]["owner"], "Bob Chen")

    def test_update_account_changes_config_keeps_initial_cash(self) -> None:
        updated = self.trading.update_account(
            "acct_test",
            {
                "name": "改名后",
                "slippage_model": "fixed_tick",
                "slippage_value": 0.0,
                "commission_rate": 0.0001,
                "initial_cash": 9_999,  # 应被忽略(不可改)
            },
        )
        self.assertEqual(updated["name"], "改名后")
        self.assertEqual(updated["slippage_model"], "fixed_tick")
        self.assertEqual(updated["slippage_value"], 0.0)
        self.assertEqual(updated["commission_rate"], 0.0001)
        self.assertEqual(updated["initial_cash"], 1_000_000)  # 初始资金锁定
        # 审计留痕
        events = self.audit.list_events({"account_id": "acct_test", "event_type": "account_updated"})
        self.assertEqual(len(events), 1)
        self.assertIn("slippage_model", events[0]["metadata"]["changed"])
        with self.assertRaises(ValueError):
            self.trading.update_account("acct_test", {"slippage_model": "bogus"})

    def test_void_trade_reverses_cash_and_position_and_records(self) -> None:
        cash0 = self.trading.get_account("acct_test")["cash"]
        self.trading.place_order(
            {
                "account_id": "acct_test",
                "strategy_id": "strategy_test",
                "run_id": "run_bad",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 100,
                "signal_price": 100,
                "fill_price": 100,
                "source_event_id": "sig_bad",
            }
        )
        self.assertEqual(self.trading.list_positions("acct_test")[0]["quantity"], 100)
        trade = next(t for t in self.audit.trade_summaries({"account_id": "acct_test"}) if t["kind"] == "trade")

        with self.assertRaises(ValueError):  # 必须填原因
            self.trading.void_trade("acct_test", trade["id"], "  ")
        res = self.trading.void_trade("acct_test", trade["id"], "agent 下错单,价格离谱")
        self.assertTrue(res["voided"])
        # 持仓清掉、现金(含费用)还原
        self.assertEqual(self.trading.list_positions("acct_test"), [])
        self.assertEqual(self.trading.get_account("acct_test")["cash"], cash0)
        # 作废留痕 + 不可重复作废
        self.assertIn(trade["id"], self.audit.voided_trade_event_ids("acct_test"))
        self.assertEqual(len(self.audit.list_events({"account_id": "acct_test", "event_type": "trade_voided"})), 1)
        with self.assertRaises(ValueError):
            self.trading.void_trade("acct_test", trade["id"], "再来一次")
        voided_row = next(t for t in self.audit.trade_summaries({"account_id": "acct_test"}) if t["id"] == trade["id"])
        self.assertTrue(voided_row["voided"])

    def test_realized_pnl_excludes_currently_held_symbols(self) -> None:
        # 600519 清仓 → 进历史;000001 仍持仓 → 不进历史。
        for side, sym, qty, px, sid in [
            ("BUY", "600519.SH", 100, 100, "h1"),
            ("SELL", "600519.SH", 100, 120, "h2"),
            ("BUY", "000001.SZ", 200, 10, "h3"),
            ("SELL", "000001.SZ", 100, 11, "h4"),
        ]:
            self.trading.place_order(
                {
                    "account_id": "acct_test",
                    "strategy_id": "strategy_test",
                    "run_id": sid,
                    "symbol": sym,
                    "side": side,
                    "quantity": qty,
                    "signal_price": px,
                    "fill_price": px,
                    "source_event_id": sid,
                }
            )
        held = set(self.trading.list_position_symbols("acct_test"))
        self.assertIn("000001.SZ", held)
        board = self.audit.realized_pnl_by_symbol({"account_id": "acct_test"}, exclude_symbols=held)
        syms = [r["symbol"] for r in board["symbols"]]
        self.assertIn("600519.SH", syms)
        self.assertNotIn("000001.SZ", syms)

    def test_day_pnl_from_prev_close(self) -> None:
        self.trading.place_order(
            {
                "account_id": "acct_test",
                "strategy_id": "strategy_test",
                "run_id": "d1",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 100,
                "signal_price": 100,
                "fill_price": 100,
                "source_event_id": "sig_d1",
            }
        )
        summary = self.trading.get_portfolio_summary(
            "acct_test", mark_prices={"600519.SH": {"price": 110, "prev_close": 105}}
        )
        account = summary["accounts"][0]
        self.assertEqual(account["holdings_day_pnl"], 500.0)  # 100*(110-105)
        self.assertEqual(account["positions"][0]["day_pnl"], 500.0)

    def test_sync_auto_repo_skips_today_and_self_heals_premature_record(self) -> None:
        from backend.trading_store import _today_cn

        today = _today_cn()
        cash0 = self.trading.get_account("acct_test")["cash"]
        # 模拟"当日被提前自动补"的脏记录(每账户每日仅一条):写 source=auto 当日记录,并按旧逻辑把利息记进未分配现金。
        with self.trading._connection() as conn:
            conn.execute(
                "UPDATE accounts SET cash = ROUND(cash + ?, 2) WHERE id = ?",
                (24.66, "acct_test"),
            )
        self.trading._upsert_repo_record(
            account_id="acct_test",
            trade_date=today,
            timestamp=f"{today}T14:30:00+08:00",
            invest_amount=500_000,
            annual_rate=0.018,
            interest=24.66,
            source="auto",
        )

        # reconcile:计划里即便混进当日也不补;并清掉当日的"自动"脏记录、把利息扣回。
        res = self.trading.sync_auto_repo(
            "acct_test",
            [{"trade_date": today, "principal": 500_000, "interest": 24.66, "annual_rate": 0.018}],
        )
        self.assertEqual(res["filled"], 0)
        self.assertEqual(res["removed_today"], 1)
        self.assertEqual(res["reverted_interest"], 24.66)

        # 现金回到注入脏记录前(自动那 24.66 被扣回);当日记录清空。
        cash1 = self.trading.get_account("acct_test")["cash"]
        self.assertEqual(round(cash1 - cash0, 2), 0.0)
        self.assertEqual(self.trading.list_reverse_repo("acct_test")["summary"]["days"], 0)

        # 手动当日记录则应保留(只清 source=auto)。
        self.trading._upsert_repo_record(
            account_id="acct_test",
            trade_date=today,
            timestamp=f"{today}T14:30:00+08:00",
            invest_amount=200_000,
            annual_rate=0.018,
            interest=9.86,
            source="manual",
        )
        res2 = self.trading.sync_auto_repo("acct_test", [])
        self.assertEqual(res2["removed_today"], 0)
        today_rows = [r for r in self.trading.list_reverse_repo("acct_test")["records"] if r["trade_date"] == today]
        self.assertEqual(len(today_rows), 1)
        self.assertEqual(today_rows[0]["source"], "manual")

if __name__ == "__main__":
    unittest.main()
