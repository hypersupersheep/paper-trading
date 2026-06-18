from __future__ import annotations

import os
import tempfile
import unittest


class IndustriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._saved_home = os.environ.get("PAPER_TRADING_HOME")
        os.environ["PAPER_TRADING_HOME"] = self.tmp.name
        from backend import industries
        industries._cache = None
        self.industries = industries

    def tearDown(self) -> None:
        self.industries._cache = None
        if self._saved_home is None:
            os.environ.pop("PAPER_TRADING_HOME", None)
        else:
            os.environ["PAPER_TRADING_HOME"] = self._saved_home
        self.tmp.cleanup()

    def test_static_industry_resolves(self) -> None:
        self.assertEqual(self.industries.resolve("600519.SH"), "食品饮料")
        self.assertEqual(self.industries.resolve("000001.SZ"), "银行")
        self.assertEqual(self.industries.resolve("688686.SH"), "机械设备")

    def test_unknown_is_unclassified(self) -> None:
        self.assertEqual(self.industries.resolve("999999.SH"), self.industries.UNCLASSIFIED)

    def test_cache_overrides_and_persists(self) -> None:
        self.industries.update({"999999.SH": "电力设备"})
        self.assertEqual(self.industries.resolve("999999.SH"), "电力设备")
        self.industries._cache = None  # 重置内存缓存,验证落盘
        self.assertEqual(self.industries.resolve("999999.SH"), "电力设备")

    def test_update_ignores_unclassified(self) -> None:
        self.industries.update({"888888.SH": self.industries.UNCLASSIFIED})
        self.assertEqual(self.industries.resolve("888888.SH"), self.industries.UNCLASSIFIED)


if __name__ == "__main__":
    unittest.main()
