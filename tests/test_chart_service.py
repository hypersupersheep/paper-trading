from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.audit_store import AuditStore
from backend.chart_service import ChartService
from backend.data_connectors import DataConnectorRegistry
from backend.trading_store import TradingStore


class ChartServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chart.sqlite3"
        self.audit = AuditStore(self.db_path)
        self.trading = TradingStore(self.db_path, self.audit)
        self.charts = ChartService(self.audit, DataConnectorRegistry())
        self.trading.create_account({"id": "acct_chart", "name": "Chart Account", "initial_cash": 500_000})
        self.trading.create_sleeve(
            "acct_chart",
            {
                "id": "sleeve_chart",
                "name": "Chart Sleeve",
                "strategy_id": "strategy_chart",
                "allocated_cash": 300_000,
            },
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_bars_endpoint_shape_uses_connector(self) -> None:
        result = self.charts.get_bars({"symbol": "000001.SZ", "frequency": "5m", "data_source": "fixture", "limit": "3"})

        self.assertEqual(result["symbol"], "000001.SZ")
        self.assertEqual(result["frequency"], "5m")
        self.assertEqual(result["data_source"], "fixture")
        self.assertEqual(len(result["bars"]), 3)
        self.assertIn("open", result["bars"][0])
        self.assertIn("close", result["bars"][0])

    def test_trade_markers_are_derived_from_audit_trades(self) -> None:
        order = self.trading.place_order(
            {
                "account_id": "acct_chart",
                "sleeve_id": "sleeve_chart",
                "strategy_id": "strategy_chart",
                "run_id": "run_chart",
                "symbol": "000001.SZ",
                "side": "BUY",
                "quantity": 100,
                "signal_price": 10,
                "fill_price": 10.02,
                "source_event_id": "sig_chart",
            }
        )
        self.assertTrue(order["accepted"])

        markers = self.charts.get_markers({"symbol": "000001.SZ", "account_id": "acct_chart"})

        self.assertEqual(len(markers["markers"]), 1)
        self.assertEqual(markers["markers"][0]["side"], "BUY")
        self.assertEqual(markers["markers"][0]["price"], 10.02)
        self.assertEqual(markers["markers"][0]["source_event_id"], "sig_chart")


if __name__ == "__main__":
    unittest.main()
