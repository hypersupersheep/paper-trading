"""历史日线(不复权收盘价)的不可变缓存。

要点:三个真实数据源(RiceQuant adjust_type="none"、Wind S_DQ_CLOSE、TongDaXin)返回的都是
**不复权 raw close**——某只股票某个「已收盘日期」的收盘价一旦定了就永不追溯变化(前复权价才会在
除权除息时被重算,那种不能这样缓存)。所以过去的日线可以放心长期缓存,NAV 冷重算时只需向数据源
补拉「缓存前沿到今天」这一小段,而不是每次都拉 2000 天全历史 × 上百只标的(那才是打开卡的真正大头)。

只缓存 `trade_date < today` 的已收盘日;今天那根还在动,绝不入缓存(留给实时盯市)。
全程 best-effort:任何异常都当缓存不存在、回落到全量拉取,绝不影响正确性(缓存只加速,不是真相源)。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

_CHUNK = 400  # SQLite 单条 SQL 参数上限约 999,分批查询留足余量


class DailyBarCache:
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
                    CREATE TABLE IF NOT EXISTS daily_bar_cache (
                        data_source TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        trade_date TEXT NOT NULL,
                        close REAL NOT NULL,
                        PRIMARY KEY (data_source, symbol, trade_date)
                    )
                    """
                )
        except sqlite3.Error:
            pass

    def load(self, data_source: str, symbols: list[str], start_date: str, before_date: str) -> dict[str, dict[str, float]]:
        """取 [start_date, before_date) 区间内已缓存的收盘价。返回 {symbol: {date: close}}。
        before_date 通常传今天(排他)——今天那根不缓存,故永远从数据源现拉。"""
        out: dict[str, dict[str, float]] = {}
        if not symbols:
            return out
        try:
            with self._connection() as conn:
                for i in range(0, len(symbols), _CHUNK):
                    chunk = symbols[i : i + _CHUNK]
                    placeholders = ",".join("?" * len(chunk))
                    rows = conn.execute(
                        f"SELECT symbol, trade_date, close FROM daily_bar_cache "
                        f"WHERE data_source = ? AND symbol IN ({placeholders}) "
                        f"AND trade_date >= ? AND trade_date < ?",
                        (data_source, *chunk, start_date, before_date),
                    ).fetchall()
                    for r in rows:
                        out.setdefault(r["symbol"], {})[r["trade_date"]] = float(r["close"])
        except sqlite3.Error:
            return {}
        return out

    def put_many(self, data_source: str, closes: dict[str, dict[str, float]]) -> None:
        """写入已收盘日线(调用方须保证只传 trade_date < today 的行)。best-effort。"""
        params = [
            (data_source, sym, d, float(c))
            for sym, days in closes.items()
            for d, c in days.items()
        ]
        if not params:
            return
        try:
            with self._connection() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO daily_bar_cache (data_source, symbol, trade_date, close) "
                    "VALUES (?, ?, ?, ?)",
                    params,
                )
        except sqlite3.Error:
            pass


def fetch_with_cache(cache, connector, ds, symbols, start_date, today) -> dict[str, dict[str, float]]:
    """取 symbols 在 [start_date, today] 的逐日收盘价,用不可变历史日线缓存加速。

    逻辑:过去已收盘的日线从缓存直接取,只向数据源补拉「缓存前沿→今天」这一小段。
      - 任一标的完全没缓存(新标的/手工回填导致起点前移)→ fetch_start 回到 start_date 全量拉一次;
      - 否则 fetch_start = 各标的缓存前沿的最小值(稳态下各标的前沿一致 ≈ 上次重算日,只补几天);
      - 数据源拉取失败 → 返缓存里的历史,今日缺失由 reconstruct 回退成交价兜底,仍比整段丢失强。
    返回 {symbol: {date: close}}。只把 date < today 的已收盘日写回缓存(今天那根在动,不缓存)。"""
    try:
        cached = cache.load(ds, symbols, start_date, today)
    except Exception:  # noqa: BLE001
        cached = {}
    fetch_start = today
    for sym in symbols:
        days = cached.get(sym)
        if not days:
            fetch_start = start_date
            break
        mx = max(days)
        if mx < fetch_start:
            fetch_start = mx
    result: dict[str, dict[str, float]] = {sym: dict(cached.get(sym, {})) for sym in symbols}
    try:
        bars = connector.get_bars(symbols, frequency="1d", limit=2000, start=fetch_start, end=today)
    except Exception:  # noqa: BLE001 - 拉取失败就用缓存历史,别整段丢
        return result
    fresh: dict[str, dict[str, float]] = {}
    for bar in bars:
        sym = str(bar["symbol"]).upper()
        d = str(bar["timestamp"])[:10]
        c = float(bar["close"])
        result.setdefault(sym, {})[d] = c
        if d < today:
            fresh.setdefault(sym, {})[d] = c
    try:
        cache.put_many(ds, fresh)
    except Exception:  # noqa: BLE001
        pass
    return result
