from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


class NamesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PAPER_TRADING_HOME"] = self.tmp.name
        # 重置 names 缓存模块状态(它有 module 级缓存)。
        from backend import names
        names._cache = None
        self.names = names

    def tearDown(self) -> None:
        self.names._cache = None
        os.environ.pop("PAPER_TRADING_HOME", None)
        self.tmp.cleanup()

    def test_static_name_resolves(self) -> None:
        self.assertEqual(self.names.resolve("600519.SH"), "贵州茅台")
        self.assertEqual(self.names.resolve("000300.SH"), "沪深300")

    def test_unknown_falls_back_to_code(self) -> None:
        self.assertEqual(self.names.resolve("999999.SH"), "999999.SH")

    def test_cache_overrides_and_persists(self) -> None:
        self.names.update({"999999.SH": "测试新股"})
        self.assertEqual(self.names.resolve("999999.SH"), "测试新股")
        # 持久化:重置内存缓存后仍能从磁盘读回
        self.names._cache = None
        self.assertEqual(self.names.resolve("999999.SH"), "测试新股")

    def test_normalizes_case(self) -> None:
        self.assertEqual(self.names.resolve("600519.sh"), "贵州茅台")


if __name__ == "__main__":
    unittest.main()
