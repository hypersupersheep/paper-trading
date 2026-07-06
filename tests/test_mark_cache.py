from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from backend.mark_cache import MarkCache
from backend.server import _json_sanitize


class MarkCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = MarkCache(Path(self.tmp.name) / "db.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_empty(self) -> None:
        self.assertEqual(self.cache.load_all(), ({}, None))

    def test_put_load(self) -> None:
        marks = {
            "600519.SH": {"price": 1600.0, "timestamp": "2026-07-06T14:00:00", "prev_close": 1590.0, "data_source": "ricequant"},
            "000001.SZ": {"price": 10.5, "timestamp": "2026-07-06T14:00:00", "prev_close": 10.4, "data_source": "ricequant"},
        }
        self.cache.put(marks, "2026-07-06T14:00:05+08:00")
        got, as_of = self.cache.load_all()
        self.assertEqual(got["600519.SH"]["price"], 1600.0)
        self.assertEqual(got["000001.SZ"]["prev_close"], 10.4)
        self.assertEqual(as_of, "2026-07-06T14:00:05+08:00")

    def test_overwrite_and_max_as_of(self) -> None:
        self.cache.put({"X": {"price": 1.0}}, "2026-07-06T09:00:00+08:00")
        self.cache.put({"X": {"price": 2.0}, "Y": {"price": 3.0}}, "2026-07-06T15:00:00+08:00")
        got, as_of = self.cache.load_all()
        self.assertEqual(got["X"]["price"], 2.0)  # 覆盖
        self.assertEqual(as_of, "2026-07-06T15:00:00+08:00")  # 全局 = 最近

    def test_put_empty_noop(self) -> None:
        self.cache.put({}, "2026-07-06T15:00:00+08:00")
        self.assertEqual(self.cache.load_all(), ({}, None))


class JsonSanitizeTest(unittest.TestCase):
    def test_nan_inf_become_none(self) -> None:
        payload = {"equity": float("nan"), "curve": [1.0, float("inf"), -float("inf")], "ok": 3.5, "s": "x"}
        clean = _json_sanitize(payload)
        self.assertIsNone(clean["equity"])
        self.assertEqual(clean["curve"], [1.0, None, None])
        self.assertEqual(clean["ok"], 3.5)
        self.assertEqual(clean["s"], "x")

    def test_nested_and_scalars_preserved(self) -> None:
        payload = {"a": {"b": [{"c": float("nan")}]}, "n": 5, "b": True, "z": None}
        clean = _json_sanitize(payload)
        self.assertIsNone(clean["a"]["b"][0]["c"])
        self.assertEqual(clean["n"], 5)
        self.assertIs(clean["b"], True)
        self.assertIsNone(clean["z"])


if __name__ == "__main__":
    unittest.main()
