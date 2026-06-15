from __future__ import annotations

import unittest

from backend import repo
from backend.data_connectors import FixtureDataConnector


class RepoTest(unittest.TestCase):
    def test_rate_from_close(self) -> None:
        self.assertEqual(repo.rate_from_close(1.85), 0.0185)
        self.assertIsNone(repo.rate_from_close(0))
        self.assertIsNone(repo.rate_from_close(500))  # 越界(把价格当利率)
        self.assertIsNone(repo.rate_from_close("x"))

    def test_term_days(self) -> None:
        self.assertEqual(repo.term_days("204001.SH"), 1)
        self.assertEqual(repo.term_days("204007.SH"), 7)
        self.assertEqual(repo.term_days("999999.SH"), 1)

    def test_is_repo_symbol(self) -> None:
        self.assertTrue(repo.is_repo_symbol("204001.SH"))
        self.assertTrue(repo.is_repo_symbol("131810.SZ"))
        self.assertFalse(repo.is_repo_symbol("600519.SH"))

    def test_fetch_latest_rate_from_fixture(self) -> None:
        # fixture 对逆回购返回 ~1.8 档 → 利率 ~0.018
        quote = repo.fetch_latest_rate(FixtureDataConnector(), "204001.SH")
        self.assertIsNotNone(quote)
        self.assertGreater(quote["annual_rate"], 0.005)
        self.assertLess(quote["annual_rate"], 0.05)

    def test_fetch_daily_rates_from_fixture(self) -> None:
        rates = repo.fetch_daily_rates(FixtureDataConnector(), "204001.SH", start="2026-05-01", end="2026-05-20")
        self.assertTrue(rates)
        for value in rates.values():
            self.assertGreater(value, 0.005)
            self.assertLess(value, 0.05)


if __name__ == "__main__":
    unittest.main()
