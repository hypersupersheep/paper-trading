from __future__ import annotations

import unittest

from backend.data_connectors import DataConnectorRegistry, FixtureDataConnector, _normalize_tdx_row, normalize_frequency


class DataConnectorTest(unittest.TestCase):
    def test_fixture_connector_returns_standard_bars(self) -> None:
        connector = FixtureDataConnector()
        bars = connector.get_bars(["000001.SZ"], frequency="5m", limit=2)

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["symbol"], "000001.SZ")
        self.assertEqual(bars[0]["frequency"], "5m")
        self.assertIn("open", bars[0])
        self.assertIn("amount", bars[0])

    def test_fixture_daily_bars_use_recent_business_days(self) -> None:
        import datetime

        connector = FixtureDataConnector()
        bars = connector.get_bars(["000300.SH"], frequency="1d", limit=5)
        self.assertEqual(len(bars), 5)
        days = [bar["timestamp"][:10] for bar in bars]
        self.assertEqual(days, sorted(days))  # 升序
        for day in days:
            self.assertLess(datetime.date.fromisoformat(day).weekday(), 5)  # 工作日

    def test_fixture_daily_respects_date_range(self) -> None:
        connector = FixtureDataConnector()
        bars = connector.get_bars(["000001.SZ"], frequency="1d", start="2015-03-13", end="2015-04-13")
        self.assertTrue(bars)
        days = [bar["timestamp"][:10] for bar in bars]
        self.assertGreaterEqual(days[0], "2015-03-13")
        self.assertLessEqual(days[-1], "2015-04-13")
        # 2015-03 到 04 约 20+ 个工作日
        self.assertGreater(len(bars), 15)

    def test_registry_rejects_unknown_connector(self) -> None:
        registry = DataConnectorRegistry()

        with self.assertRaisesRegex(ValueError, "unknown data source"):
            registry.get("missing")

    def test_frequency_aliases_are_normalized(self) -> None:
        self.assertEqual(normalize_frequency("5min"), "5m")
        self.assertEqual(normalize_frequency("daily"), "1d")

    def test_tdx_row_normalization(self) -> None:
        bar = _normalize_tdx_row(
            "000001.SZ",
            "5m",
            {
                "datetime": "2026-06-10 09:35:00",
                "open": 10,
                "high": 10.2,
                "low": 9.9,
                "close": 10.1,
                "volume": 1000,
                "amount": 10100,
            },
        )

        self.assertEqual(bar["symbol"], "000001.SZ")
        self.assertEqual(bar["close"], 10.1)
        self.assertEqual(bar["amount"], 10100)

    def test_tongdaxin_health_is_non_crashing(self) -> None:
        registry = DataConnectorRegistry()
        health = {item["name"]: item for item in registry.health()}

        self.assertIn("tongdaxin", health)
        self.assertIn(health["tongdaxin"]["status"], {"ok", "unavailable"})

    def test_tongdaxin_get_names_maps_code_to_name(self) -> None:
        import types

        import pandas as pd

        from backend.data_connectors import TongDaXinDataConnector

        sh = pd.DataFrame([{"code": "600229", "name": "城市传媒"}, {"code": "600519", "name": "贵州茅台"}])

        class _Client:
            def stocks(self, market):
                return sh if market == 1 else pd.DataFrame([], columns=["code", "name"])

        fake = types.SimpleNamespace(Quotes=types.SimpleNamespace(factory=lambda **kw: _Client()))
        connector = TongDaXinDataConnector()
        connector._import_mootdx = lambda: fake  # type: ignore[assignment]

        names = connector.get_names(["600229.SH", "600519.SH"])
        self.assertEqual(names, {"600229.SH": "城市传媒", "600519.SH": "贵州茅台"})

    def test_tongdaxin_get_names_swallows_errors(self) -> None:
        from backend.data_connectors import TongDaXinDataConnector

        connector = TongDaXinDataConnector()
        connector._import_mootdx = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[assignment]
        self.assertEqual(connector.get_names(["600229.SH"]), {})


if __name__ == "__main__":
    unittest.main()


class ConnectorSettingsTest(unittest.TestCase):
    def test_save_load_and_mask(self) -> None:
        import tempfile
        from pathlib import Path

        from backend.connector_settings import (
            get_connector_settings,
            mask_secret,
            save_connector_settings,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            save_connector_settings("ricequant", {"license_key": "abcd1234efgh5678"}, path)
            loaded = get_connector_settings("ricequant", path)
            self.assertEqual(loaded["license_key"], "abcd1234efgh5678")
            self.assertIn("updated_at", loaded)
        self.assertEqual(mask_secret("abcd1234efgh5678"), "abcd***5678")
        self.assertEqual(mask_secret("short"), "*****")
        self.assertIsNone(mask_secret(None))


class RiceQuantConnectorTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.tmp.name) / "settings.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _connector(self):
        from backend.data_connectors import RiceQuantDataConnector

        return RiceQuantDataConnector(settings_path=self.settings_path)

    def test_health_not_configured_without_key(self) -> None:
        health = self._connector().healthcheck()
        self.assertEqual(health["status"], "not_configured")

    def test_health_configured_after_key_saved(self) -> None:
        from backend.connector_settings import save_connector_settings

        save_connector_settings("ricequant", {"license_key": "abcd1234efgh5678"}, self.settings_path)
        health = self._connector().healthcheck()
        self.assertEqual(health["status"], "configured")
        self.assertEqual(health["license_key_masked"], "abcd***5678")

    def test_symbol_conversion_round_trip(self) -> None:
        from backend.data_connectors import _from_rq_symbol, _rq_symbol

        self.assertEqual(_rq_symbol("000001.SZ"), "000001.XSHE")
        self.assertEqual(_rq_symbol("600519.SH"), "600519.XSHG")
        self.assertEqual(_rq_symbol("600519"), "600519.XSHG")
        self.assertEqual(_from_rq_symbol("000001.XSHE"), "000001.SZ")
        self.assertEqual(_from_rq_symbol("600519.XSHG"), "600519.SH")

    def test_get_bars_inits_license_and_normalizes_frame(self) -> None:
        import sys
        import types

        import pandas as pd

        from backend.connector_settings import save_connector_settings

        save_connector_settings("ricequant", {"license_key": "test_license_key_001"}, self.settings_path)

        calls = {"init": [], "get_price": []}
        fake = types.ModuleType("rqdatac")

        def fake_init(*args, **kwargs):
            calls["init"].append((args, kwargs))

        def fake_get_price(order_book_ids, **kwargs):
            calls["get_price"].append((order_book_ids, kwargs))
            index = pd.MultiIndex.from_tuples(
                [
                    ("000001.XSHE", pd.Timestamp("2026-06-09")),
                    ("000001.XSHE", pd.Timestamp("2026-06-10")),
                    ("000001.XSHE", pd.Timestamp("2026-06-11")),
                ],
                names=["order_book_id", "date"],
            )
            return pd.DataFrame(
                {
                    "open": [10.0, 10.5, 11.0],
                    "high": [10.6, 11.0, 11.4],
                    "low": [9.9, 10.4, 10.9],
                    "close": [10.5, 10.9, 11.3],
                    "volume": [1000.0, 1100.0, 1200.0],
                    "total_turnover": [10500.0, 11990.0, 13560.0],
                },
                index=index,
            )

        fake.init = fake_init
        fake.get_price = fake_get_price
        original = sys.modules.get("rqdatac")
        sys.modules["rqdatac"] = fake
        self.addCleanup(lambda: sys.modules.__setitem__("rqdatac", original) if original else sys.modules.pop("rqdatac", None))

        connector = self._connector()
        bars = connector.get_bars(["000001.SZ"], frequency="1d", limit=2)

        self.assertEqual(calls["init"][0][0], ("license", "test_license_key_001"))
        self.assertEqual(calls["get_price"][0][0], ["000001.XSHE"])
        self.assertEqual(calls["get_price"][0][1]["frequency"], "1d")
        # limit=2: 只保留最近两根, symbol 转回 .SZ 格式。
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[-1]["symbol"], "000001.SZ")
        self.assertEqual(bars[-1]["close"], 11.3)
        self.assertEqual(bars[-1]["timestamp"], "2026-06-11T00:00:00")
        # 连接成功后 health 升级为 ok; 再次取数不重复 init。
        self.assertEqual(connector.healthcheck()["status"], "ok")
        connector.get_bars(["000001.SZ"], frequency="1d", limit=2)
        self.assertEqual(len(calls["init"]), 1)

    def test_get_bars_with_date_range_passes_through_and_returns_full_window(self) -> None:
        import datetime
        import sys
        import types

        import pandas as pd

        from backend.connector_settings import save_connector_settings

        save_connector_settings("ricequant", {"license_key": "k"}, self.settings_path)
        captured = {}
        fake = types.ModuleType("rqdatac")
        fake.init = lambda *a, **k: None

        def fake_get_price(order_book_ids, **kwargs):
            captured.update(kwargs)
            idx = pd.MultiIndex.from_tuples(
                [("000001.XSHE", pd.Timestamp("2015-03-13")),
                 ("000001.XSHE", pd.Timestamp("2015-03-16")),
                 ("000001.XSHE", pd.Timestamp("2015-03-17"))],
                names=["order_book_id", "date"],
            )
            return pd.DataFrame(
                {"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3],
                 "close": [10, 11, 12], "volume": [1, 1, 1], "total_turnover": [1, 1, 1]},
                index=idx,
            )

        fake.get_price = fake_get_price
        original = sys.modules.get("rqdatac")
        sys.modules["rqdatac"] = fake
        self.addCleanup(lambda: sys.modules.__setitem__("rqdatac", original) if original else sys.modules.pop("rqdatac", None))

        bars = self._connector().get_bars(["000001.SZ"], frequency="1d", limit=2, start="2015-03-13", end="2015-03-17")
        # 显式区间:start_date/end_date 透传给 rqdatac,且返回整段(不被 limit=2 截断)。
        self.assertEqual(captured["start_date"], datetime.date(2015, 3, 13))
        self.assertEqual(captured["end_date"], datetime.date(2015, 3, 17))
        self.assertEqual(len(bars), 3)

    def test_get_bars_without_key_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "license key"):
            self._connector().get_bars(["000001.SZ"], frequency="1d", limit=2)


class WindConnectorTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.tmp.name) / "settings.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _connector(self):
        from backend.data_connectors import WindDataConnector

        return WindDataConnector(settings_path=self.settings_path)

    def _save(self, **over) -> None:
        from backend.connector_settings import save_connector_settings

        config = {"host": "h", "port": 3306, "user": "u", "password": "p", "database": "wind_data"}
        config.update(over)
        save_connector_settings("wind", config, self.settings_path)

    def test_health_not_configured(self) -> None:
        self.assertEqual(self._connector().healthcheck()["status"], "not_configured")

    def test_health_configured_hides_password(self) -> None:
        self._save()
        health = self._connector().healthcheck()
        self.assertEqual(health["status"], "configured")
        self.assertEqual(health["host"], "h")
        self.assertNotIn("password", health)  # 密码绝不回传

    def test_intraday_rejected(self) -> None:
        self._save()
        with self.assertRaisesRegex(ValueError, "日频"):
            self._connector().get_bars(["000001.SZ"], frequency="5m")

    def test_get_bars_queries_both_tables_and_normalizes(self) -> None:
        import sys
        import types

        self._save()
        calls = {"sql": [], "connect": []}
        fake = types.ModuleType("pymysql")

        class FakeCursor:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def execute(self_inner, sql, params=None):
                calls["sql"].append((sql, params))

            def fetchall(self_inner):
                last = calls["sql"][-1][0]
                if "ASHAREEODPRICES" in last:
                    return [{
                        "S_INFO_WINDCODE": "000001.SZ", "TRADE_DT": "20260611",
                        "S_DQ_OPEN": 10, "S_DQ_HIGH": 10.5, "S_DQ_LOW": 9.9,
                        "S_DQ_CLOSE": 10.2, "S_DQ_VOLUME": 1000, "S_DQ_AMOUNT": 10200,
                    }]
                return []

        class FakeConn:
            def cursor(self_inner):
                return FakeCursor()

            def close(self_inner):
                pass

        def fake_connect(**kw):
            calls["connect"].append(kw)
            return FakeConn()

        fake.connect = fake_connect
        fake.cursors = types.SimpleNamespace(DictCursor=object)
        original = sys.modules.get("pymysql")
        sys.modules["pymysql"] = fake
        self.addCleanup(lambda: sys.modules.__setitem__("pymysql", original) if original else sys.modules.pop("pymysql", None))

        bars = self._connector().get_bars(["000001.SZ"], frequency="1d", start="2026-06-01", end="2026-06-11")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["symbol"], "000001.SZ")
        self.assertEqual(bars[0]["timestamp"], "2026-06-11T00:00:00")
        self.assertEqual(bars[0]["close"], 10.2)
        sqls = [c[0] for c in calls["sql"]]
        self.assertTrue(any("ASHAREEODPRICES" in s for s in sqls))
        self.assertTrue(any("AINDEXEODPRICES" in s for s in sqls))  # 股票表+指数表都查
        self.assertEqual(calls["connect"][0]["host"], "h")
        self.assertEqual(calls["connect"][0]["database"], "wind_data")
        # TRADE_DT 用 YYYYMMDD 区间
        ashare_params = next(p for s, p in calls["sql"] if "ASHAREEODPRICES" in s)
        self.assertEqual(ashare_params[0], "20260601")
        self.assertEqual(ashare_params[1], "20260611")
