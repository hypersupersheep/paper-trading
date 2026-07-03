from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.daily_bar_cache import DailyBarCache, fetch_with_cache


class FakeConnector:
    """记录每次 get_bars 被要求的 start,并按内置日线返回 [start, end] 的收盘价。"""

    def __init__(self, series: dict[str, dict[str, float]]):
        self.series = series  # {symbol: {date: close}}
        self.calls: list[tuple[str, str]] = []  # (start, end)

    def get_bars(self, symbols, frequency="1d", limit=2000, start=None, end=None):
        self.calls.append((start, end))
        out = []
        for sym in symbols:
            for d, c in sorted(self.series.get(sym.upper(), {}).items()):
                if (start is None or d >= start) and (end is None or d <= end):
                    out.append({"symbol": sym, "timestamp": f"{d}T00:00:00+08:00", "close": c})
        return out


class DailyBarCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = DailyBarCache(Path(self.tmp.name) / "bars.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_put_load_roundtrip(self) -> None:
        self.cache.put_many("rq", {"600519.SH": {"2026-06-01": 10.0, "2026-06-02": 11.0}})
        got = self.cache.load("rq", ["600519.SH"], "2026-06-01", "2026-07-01")
        self.assertEqual(got, {"600519.SH": {"2026-06-01": 10.0, "2026-06-02": 11.0}})

    def test_load_excludes_before_date(self) -> None:
        # before_date 排他:等于 before_date 的行不返(今天那根不该从缓存出)
        self.cache.put_many("rq", {"X": {"2026-06-01": 1.0, "2026-06-30": 2.0}})
        got = self.cache.load("rq", ["X"], "2026-06-01", "2026-06-30")
        self.assertEqual(got, {"X": {"2026-06-01": 1.0}})

    def test_data_source_isolation(self) -> None:
        self.cache.put_many("rq", {"X": {"2026-06-01": 1.0}})
        self.assertEqual(self.cache.load("wind", ["X"], "2026-06-01", "2026-07-01"), {})


class FetchWithCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = DailyBarCache(Path(self.tmp.name) / "bars.sqlite3")
        self.series = {
            "A": {"2026-06-01": 10.0, "2026-06-02": 11.0, "2026-06-03": 12.0},
            "B": {"2026-06-01": 20.0, "2026-06-02": 21.0, "2026-06-03": 22.0},
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cold_then_warm_only_fetches_frontier(self) -> None:
        conn = FakeConnector(self.series)
        # 冷:缓存空 → 从 start_date 全量拉,today=06-03(排他,故今天不缓存)
        r1 = fetch_with_cache(self.cache, conn, "rq", ["A", "B"], "2026-06-01", "2026-06-03")
        self.assertEqual(conn.calls[-1], ("2026-06-01", "2026-06-03"))
        # 06-01/06-02 应已入缓存(<today),06-03 是 today 不缓存
        cached = self.cache.load("rq", ["A", "B"], "2026-06-01", "2026-06-03")
        self.assertEqual(cached["A"], {"2026-06-01": 10.0, "2026-06-02": 11.0})

        # 暖:同一天再调 → fetch_start = 缓存前沿 06-02(不再回到 06-01)
        conn2 = FakeConnector(self.series)
        r2 = fetch_with_cache(self.cache, conn2, "rq", ["A", "B"], "2026-06-01", "2026-06-03")
        self.assertEqual(conn2.calls[-1][0], "2026-06-02")  # 只补拉前沿→今天
        # 结果与冷路径完全一致(缓存历史 + 现拉今天,合并无缝)
        self.assertEqual(r1, r2)
        self.assertEqual(r2["A"], {"2026-06-01": 10.0, "2026-06-02": 11.0, "2026-06-03": 12.0})

    def test_new_symbol_forces_full_history(self) -> None:
        conn = FakeConnector(self.series)
        fetch_with_cache(self.cache, conn, "rq", ["A"], "2026-06-01", "2026-06-03")
        # A 已缓存;现在引入全新标的 B → 必须回到 start_date 补 B 的历史
        conn2 = FakeConnector(self.series)
        fetch_with_cache(self.cache, conn2, "rq", ["A", "B"], "2026-06-01", "2026-06-03")
        self.assertEqual(conn2.calls[-1][0], "2026-06-01")

    def test_connector_failure_returns_cached_history(self) -> None:
        # 先暖好缓存
        fetch_with_cache(self.cache, FakeConnector(self.series), "rq", ["A"], "2026-06-01", "2026-06-03")

        class Boom:
            def get_bars(self, *a, **k):
                raise RuntimeError("network down")

        r = fetch_with_cache(self.cache, Boom(), "rq", ["A"], "2026-06-01", "2026-06-03")
        # 拉取失败仍返缓存里的历史(06-01/06-02),不整段丢
        self.assertEqual(r["A"], {"2026-06-01": 10.0, "2026-06-02": 11.0})

    def test_today_bar_never_cached(self) -> None:
        fetch_with_cache(self.cache, FakeConnector(self.series), "rq", ["A"], "2026-06-01", "2026-06-03")
        # 直接查缓存里有没有 today(06-03)那根 → 不该有
        raw = self.cache.load("rq", ["A"], "2026-06-03", "2026-06-04")
        self.assertEqual(raw, {})


if __name__ == "__main__":
    unittest.main()
