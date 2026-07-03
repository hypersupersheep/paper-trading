"""派生计算的持久缓存:把"算过一次就别再算"的东西(NAV 曲线、绩效、流水折叠、盈亏)
按「失效键」缓存起来。

失效键 = (ledger_version, extra):
  - ledger_version = 该账户审计账本 max(rowid)(见 audit_store.ledger_version)。任何新成交/现金/
    补录/作废都会让它变大 → 缓存自动失效。
  - extra = 其余影响结果的输入(如 data_source、当日日期、benchmark)拼成的字符串。

命中(version 和 extra 都不变)→ 直接返回上次 payload,不重算;否则 miss,调用方算完 put 回来。
全程 best-effort:缓存表读写任何异常都吞掉、当作 miss,绝不影响主请求(缓存只是加速,不是真相源)。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ComputeCache:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        try:
            with self._connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS compute_cache (
                        cache_key TEXT PRIMARY KEY,
                        ledger_version INTEGER NOT NULL,
                        extra TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
        except sqlite3.Error:
            pass  # 建不了缓存表也不影响主功能,后续 get/put 都会静默失败当 miss

    def get(self, cache_key: str, ledger_version: int, extra: str) -> Any | None:
        """命中(版本 + extra 都匹配)返回反序列化后的 payload;否则 None(视作 miss)。"""
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT payload FROM compute_cache WHERE cache_key = ? AND ledger_version = ? AND extra = ?",
                    (cache_key, int(ledger_version), extra),
                ).fetchone()
            if not row:
                return None
            return json.loads(row["payload"])
        except (sqlite3.Error, json.JSONDecodeError, ValueError):
            return None

    def put(self, cache_key: str, ledger_version: int, extra: str, payload: Any) -> None:
        """写入/覆盖缓存(同 cache_key 只留最新一版)。best-effort,失败静默。"""
        try:
            blob = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        try:
            with self._connection() as conn:
                conn.execute(
                    """
                    INSERT INTO compute_cache (cache_key, ledger_version, extra, payload, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        ledger_version = excluded.ledger_version,
                        extra = excluded.extra,
                        payload = excluded.payload,
                        updated_at = excluded.updated_at
                    """,
                    (cache_key, int(ledger_version), extra, blob, datetime.now(timezone.utc).isoformat()),
                )
        except sqlite3.Error:
            pass

    def invalidate(self, cache_key_prefix: str) -> None:
        """按前缀清缓存(如某账户被删时清它的所有缓存)。best-effort。"""
        try:
            with self._connection() as conn:
                conn.execute("DELETE FROM compute_cache WHERE cache_key LIKE ?", (cache_key_prefix + "%",))
        except sqlite3.Error:
            pass
