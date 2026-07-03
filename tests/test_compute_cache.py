from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.audit_store import AuditStore
from backend.compute_cache import ComputeCache


class ComputeCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = ComputeCache(Path(self.tmp.name) / "db.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_miss_on_empty(self) -> None:
        self.assertIsNone(self.cache.get("nav:acct", 1, "ds|day"))

    def test_hit_same_version_and_extra(self) -> None:
        payload = {"curve": [1, 2, 3], "meta": {"n": 3}}
        self.cache.put("nav:acct", 5, "rq|2026-07-03", payload)
        self.assertEqual(self.cache.get("nav:acct", 5, "rq|2026-07-03"), payload)

    def test_version_bump_is_miss(self) -> None:
        # 账本变了(version 变大)→ 旧缓存自动失效,视作 miss。
        self.cache.put("nav:acct", 5, "rq|2026-07-03", {"v": 5})
        self.assertIsNone(self.cache.get("nav:acct", 6, "rq|2026-07-03"))

    def test_extra_change_is_miss(self) -> None:
        # data_source / 当日日期 变了 → miss。
        self.cache.put("nav:acct", 5, "rq|2026-07-03", {"v": 5})
        self.assertIsNone(self.cache.get("nav:acct", 5, "tushare|2026-07-03"))
        self.assertIsNone(self.cache.get("nav:acct", 5, "rq|2026-07-04"))

    def test_put_upserts_latest_only(self) -> None:
        # 同 cache_key 只留最新一版:新版写入后,老版本 key 查不到。
        self.cache.put("nav:acct", 5, "rq|d", {"v": 5})
        self.cache.put("nav:acct", 6, "rq|d", {"v": 6})
        self.assertIsNone(self.cache.get("nav:acct", 5, "rq|d"))
        self.assertEqual(self.cache.get("nav:acct", 6, "rq|d"), {"v": 6})

    def test_invalidate_prefix(self) -> None:
        self.cache.put("nav:acct", 1, "e", {"v": 1})
        self.cache.put("trades:acct", 1, "e", {"v": 1})
        self.cache.put("nav:other", 1, "e", {"v": 1})
        self.cache.invalidate("nav:acct")
        self.assertIsNone(self.cache.get("nav:acct", 1, "e"))
        self.assertEqual(self.cache.get("trades:acct", 1, "e"), {"v": 1})
        self.assertEqual(self.cache.get("nav:other", 1, "e"), {"v": 1})

    def test_non_json_payload_is_silently_skipped(self) -> None:
        # 不可序列化的 payload → put 静默跳过,不抛异常、后续为 miss。
        self.cache.put("nav:acct", 1, "e", {"bad": {1, 2, 3}})  # set 不可 json
        self.assertIsNone(self.cache.get("nav:acct", 1, "e"))


class LedgerVersionInvalidationTest(unittest.TestCase):
    """用真实 AuditStore 验证「新成交自动让缓存失效」的端到端契约。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name) / "audit.sqlite3"
        self.store = AuditStore(base)
        self.cache = ComputeCache(base)  # 同一个 DB 文件,两张表共存

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _record_trade(self, ts: str) -> None:
        self.store.record_trade_settlement(
            account_id="acct",
            strategy_id="s",
            run_id="r",
            symbol="600519.SH",
            side="BUY",
            quantity=100,
            price=100.0,
            timestamp=ts,
            source_event_id=None,
            cash_before=1_000_000,
            position_before=0,
            avg_cost_before=0,
            commission=5,
            stamp_duty=0,
            slippage_cost=2,
        )

    def test_version_zero_when_empty(self) -> None:
        self.assertEqual(self.store.ledger_version("acct"), 0)

    def test_version_increases_after_trade(self) -> None:
        v0 = self.store.ledger_version("acct")
        self._record_trade("2026-06-10T09:35:00+08:00")
        v1 = self.store.ledger_version("acct")
        self.assertGreater(v1, v0)

    def test_version_is_per_account(self) -> None:
        self._record_trade("2026-06-10T09:35:00+08:00")
        self.assertGreater(self.store.ledger_version("acct"), 0)
        self.assertEqual(self.store.ledger_version("other"), 0)

    def test_cache_hits_until_new_trade_then_invalidates(self) -> None:
        # 1) 无新成交 → 同 version 命中
        v1 = self.store.ledger_version("acct")
        self.cache.put("nav:acct", v1, "rq|day", {"nav": 100})
        self.assertEqual(self.cache.get("nav:acct", v1, "rq|day"), {"nav": 100})
        # 2) 来了一笔新成交 → version 变大 → 用新 version 查旧缓存 = miss(自动失效)
        self._record_trade("2026-06-10T09:35:00+08:00")
        v2 = self.store.ledger_version("acct")
        self.assertGreater(v2, v1)
        self.assertIsNone(self.cache.get("nav:acct", v2, "rq|day"))


if __name__ == "__main__":
    unittest.main()
