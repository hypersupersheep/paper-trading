from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import data_connectors
from backend.data_connectors import IFinDDataConnector


def _settings_file(token: str | None) -> Path:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump({"ifind": {"refresh_token": token}} if token else {}, tmp)
    tmp.close()
    return Path(tmp.name)


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class _FakeOpener:
    """冒充 urllib build_opener() 的返回:按端点回预置 JSON。"""

    def __init__(self, responses: dict[str, dict]):
        self.responses = responses

    def open(self, req, timeout=30):
        endpoint = req.full_url.rsplit("/", 1)[-1]
        return _FakeResp(json.dumps(self.responses[endpoint]).encode("utf-8"))


def _patch_opener(responses: dict[str, dict]):
    """patch data_connectors 里的 build_opener,让连接器的 HTTP 走假 opener。"""
    return mock.patch.object(
        data_connectors.urllib.request, "build_opener", lambda *a, **k: _FakeOpener(responses)
    )


class IFinDConnectorTest(unittest.TestCase):
    def test_not_configured_health(self) -> None:
        conn = IFinDDataConnector(settings_path=_settings_file(None))
        h = conn.healthcheck()
        self.assertEqual(h["name"], "ifind")
        self.assertEqual(h["status"], "not_configured")

    def test_get_bars_daily_parses(self) -> None:
        conn = IFinDDataConnector(settings_path=_settings_file("fake-refresh"))
        responses = {
            "get_access_token": {"data": {"access_token": "ACC"}},
            "cmd_history_quotation": {
                "errorcode": 0,
                "tables": [{
                    "thscode": "000001.SZ",
                    "time": ["2026-06-29", "2026-06-30"],
                    "table": {"open": [10.0, 10.2], "high": [10.5, 10.6], "low": [9.9, 10.1],
                              "close": [10.4, 10.5], "volume": [1000, 1100], "amount": [10400, 11000]},
                }],
            },
        }
        with _patch_opener(responses):
            bars = conn.get_bars(["000001.SZ"], frequency="1d", limit=5, start="2026-06-29", end="2026-06-30")
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[-1]["symbol"], "000001.SZ")
        self.assertEqual(bars[-1]["timestamp"], "2026-06-30T00:00:00")
        self.assertEqual(bars[-1]["close"], 10.5)
        self.assertEqual(bars[-1]["frequency"], "1d")

    def test_get_bars_minute_parses_and_uses_high_frequency(self) -> None:
        conn = IFinDDataConnector(settings_path=_settings_file("fake-refresh"))
        responses = {
            "get_access_token": {"data": {"access_token": "ACC"}},
            "high_frequency": {
                "errorcode": 0,
                "tables": [{
                    "thscode": "600000.SH",
                    "time": ["2026-06-30 09:35:00", "2026-06-30 09:40:00"],
                    "table": {"open": [8.0, 8.1], "high": [8.2, 8.2], "low": [7.9, 8.0],
                              "close": [8.1, 8.15], "volume": [500, 600], "amount": [4050, 4890]},
                }],
            },
        }
        with _patch_opener(responses):
            bars = conn.get_bars(["600000.SH"], frequency="5m", limit=10)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[-1]["timestamp"], "2026-06-30T09:40:00")
        self.assertEqual(bars[-1]["frequency"], "5m")

    def test_errorcode_raises_clean(self) -> None:
        conn = IFinDDataConnector(settings_path=_settings_file("fake-refresh"))
        responses = {
            "get_access_token": {"data": {"access_token": "ACC"}},
            "cmd_history_quotation": {"errorcode": -4309, "errmsg": "history limited to 1 year", "tables": []},
        }
        with _patch_opener(responses):
            with self.assertRaises(ValueError) as ctx:
                conn.get_bars(["000001.SZ"], frequency="1d", limit=5)
        self.assertIn("-4309", str(ctx.exception))

    def test_unsupported_frequency_raises(self) -> None:
        conn = IFinDDataConnector(settings_path=_settings_file("fake-refresh"))
        with self.assertRaises(ValueError):
            conn.get_bars(["000001.SZ"], frequency="tick")

    def test_registry_includes_ifind(self) -> None:
        reg = data_connectors.DataConnectorRegistry()
        self.assertIn("ifind", reg.names())
        self.assertIsInstance(reg.get("ifind"), IFinDDataConnector)


if __name__ == "__main__":
    unittest.main()
