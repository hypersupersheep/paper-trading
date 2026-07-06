"""节点最近一次算好的实时盯市价缓存(供 Admin 白读,零数据源额度消耗)。

契约 v9(见 协作交接.md):节点是盯市的唯一主人——它为自己界面刷新时已经现查数据源算好了实时估值。
把那次算好的**逐标的盯市价**(price/timestamp/prev_close/volatility/来源)顺手存下来;Admin 不带
data_source 拉 summary 时,直接喂这份缓存重算估值——读取动作不碰 ricequant,额度只由节点自己的刷新
节奏消耗。估值(equity/pnl/day_pnl/exposure)由 trading_store 从"缓存盯市价 + 当前持仓"重算得出,
所以这里只缓存盯市价这一份"贵在现查"的输入,不重复缓存派生数字。

表按 symbol 存,新的一次盯市 upsert 覆盖;全局 as_of = 各行 as_of 的最大值(最近一次盯市时刻)。
best-effort:任何异常吞掉当缓存不存在,绝不影响主请求。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class MarkCache:
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
                    CREATE TABLE IF NOT EXISTS mark_cache (
                        symbol TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        as_of TEXT NOT NULL
                    )
                    """
                )
        except sqlite3.Error:
            pass

    def put(self, marks: dict[str, dict], as_of: str) -> None:
        """把一次成功盯市的逐标的价写入(覆盖旧值)。marks = {symbol: {price, timestamp, ...}}。"""
        rows = []
        for symbol, mark in (marks or {}).items():
            try:
                rows.append((str(symbol), json.dumps(mark, ensure_ascii=False), as_of))
            except (TypeError, ValueError):
                continue
        if not rows:
            return
        try:
            with self._connection() as conn:
                conn.executemany(
                    "INSERT INTO mark_cache (symbol, payload, as_of) VALUES (?, ?, ?) "
                    "ON CONFLICT(symbol) DO UPDATE SET payload=excluded.payload, as_of=excluded.as_of",
                    rows,
                )
        except sqlite3.Error:
            pass

    def load_all(self) -> tuple[dict[str, dict], str | None]:
        """返回 ({symbol: mark_dict}, 全局 as_of=各行最大)。无缓存 → ({}, None)。"""
        out: dict[str, dict] = {}
        as_of: str | None = None
        try:
            with self._connection() as conn:
                rows = conn.execute("SELECT symbol, payload, as_of FROM mark_cache").fetchall()
            for r in rows:
                try:
                    out[r["symbol"]] = json.loads(r["payload"])
                except (json.JSONDecodeError, ValueError):
                    continue
                if as_of is None or r["as_of"] > as_of:
                    as_of = r["as_of"]
        except sqlite3.Error:
            return {}, None
        return out, as_of
