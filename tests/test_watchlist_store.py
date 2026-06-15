from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.watchlist_store import WatchlistStore


class WatchlistStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = WatchlistStore(Path(self.tmp.name) / "wl.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_seed_then_no_duplicate_seed(self) -> None:
        self.store.seed_demo()
        first = [item["symbol"] for item in self.store.list_symbols()]
        self.assertIn("000001.SZ", first)
        self.store.seed_demo()  # 二次 seed 不应翻倍
        self.assertEqual(len(self.store.list_symbols()), len(first))

    def test_add_normalizes_and_dedupes(self) -> None:
        self.store.add(" 600519.sh ")
        self.store.add("600519.SH")  # 同标的不重复
        symbols = [item["symbol"] for item in self.store.list_symbols()]
        self.assertEqual(symbols, ["600519.SH"])

    def test_add_preserves_insertion_order(self) -> None:
        for symbol in ["000001.SZ", "600519.SH", "000858.SZ"]:
            self.store.add(symbol)
        self.assertEqual(
            [item["symbol"] for item in self.store.list_symbols()],
            ["000001.SZ", "600519.SH", "000858.SZ"],
        )

    def test_remove(self) -> None:
        self.store.add("000001.SZ")
        self.assertTrue(self.store.remove("000001.sz"))
        self.assertEqual(self.store.list_symbols(), [])
        self.assertFalse(self.store.remove("000001.SZ"))

    def test_empty_symbol_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "symbol is required"):
            self.store.add("   ")


if __name__ == "__main__":
    unittest.main()
