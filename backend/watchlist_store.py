from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# 自选股是 workspace 级(全组共享一个监控池),不挂账户,不进审计——纯监控用途。
DEFAULT_WATCHLIST = ["000001.SZ", "600519.SH", "000858.SZ", "000002.SZ"]


class WatchlistStore:
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
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watchlist (
                    symbol TEXT PRIMARY KEY,
                    note TEXT,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    added_at TEXT NOT NULL
                )
                """
            )

    def seed_demo(self) -> None:
        if self.list_symbols():
            return
        for index, symbol in enumerate(DEFAULT_WATCHLIST):
            self.add(symbol, sort_order=index)

    def list_symbols(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT symbol, note, sort_order, added_at FROM watchlist ORDER BY sort_order ASC, added_at ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def add(self, symbol: str, *, note: str | None = None, sort_order: int | None = None) -> dict[str, Any]:
        clean = _normalize_symbol(symbol)
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            if sort_order is None:
                row = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM watchlist").fetchone()
                sort_order = int(row["next"])
            conn.execute(
                """
                INSERT INTO watchlist (symbol, note, sort_order, added_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET note = excluded.note
                """,
                (clean, note, sort_order, now),
            )
        return {"symbol": clean, "note": note, "sort_order": sort_order, "added_at": now}

    def remove(self, symbol: str) -> bool:
        clean = _normalize_symbol(symbol)
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM watchlist WHERE symbol = ?", (clean,))
            return cursor.rowcount > 0


def _normalize_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if not value:
        raise ValueError("symbol is required")
    return value
