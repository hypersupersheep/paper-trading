from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend import app_settings
from backend.audit_store import AuditEvent, AuditStore
from backend.data_connectors import normalize_frequency
from backend.strategy_store import StrategyStore
from backend.timing_store import TimingStore
from backend.trading_store import TradingStore


# 固定 UTC+8(与 trading_store 一致,避免依赖系统时区库/tzdata,见 risk_store 说明)。
CN_TZ = timezone(timedelta(hours=8))


_SCHED_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS scheduler_tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    account_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    timing_strategy_id TEXT,
    data_source TEXT NOT NULL,
    symbols TEXT NOT NULL,
    frequency TEXT NOT NULL,
    interval_seconds REAL NOT NULL,
    bar_limit INTEGER NOT NULL,
    calendar TEXT NOT NULL DEFAULT 'CN_A',
    calendar_enabled INTEGER NOT NULL DEFAULT 1,
    dedupe_bars INTEGER NOT NULL DEFAULT 1,
    last_bar_key TEXT,
    last_bar_at TEXT,
    status TEXT NOT NULL,
    ticks_started INTEGER NOT NULL,
    ticks_completed INTEGER NOT NULL,
    ticks_skipped INTEGER NOT NULL DEFAULT 0,
    last_tick_at TEXT,
    next_tick_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


_SCHED_TICKS_DDL = """
CREATE TABLE IF NOT EXISTS scheduler_ticks (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    timing_run_id TEXT,
    strategy_run_id TEXT,
    decisions_recorded INTEGER NOT NULL,
    orders_submitted INTEGER NOT NULL,
    bar_key TEXT,
    bar_timestamp TEXT,
    skip_reason TEXT,
    error TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(task_id) REFERENCES scheduler_tasks(id)
)
"""


class SchedulerStore:
    def __init__(
        self,
        db_path: str | Path,
        audit_store: AuditStore,
        trading_store: TradingStore,
        strategy_store: StrategyStore,
        timing_store: TimingStore,
    ):
        self.db_path = Path(db_path)
        self.audit_store = audit_store
        self.trading_store = trading_store
        self.strategy_store = strategy_store
        self.timing_store = timing_store
        self._threads: dict[str, threading.Thread] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        # 由 server 注入;tick 完成后追加 NAV 快照,实盘 loop 自然积累净值曲线。None 时跳过。
        self.performance = None
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
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
            conn.execute(_SCHED_TASKS_DDL)
            conn.execute(_SCHED_TICKS_DDL)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scheduler_ticks_task
                ON scheduler_ticks (task_id, started_at)
                """
            )
            self._ensure_column(conn, "scheduler_tasks", "calendar", "TEXT NOT NULL DEFAULT 'CN_A'")
            self._ensure_column(conn, "scheduler_tasks", "calendar_enabled", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "scheduler_tasks", "dedupe_bars", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "scheduler_tasks", "last_bar_key", "TEXT")
            self._ensure_column(conn, "scheduler_tasks", "last_bar_at", "TEXT")
            self._ensure_column(conn, "scheduler_tasks", "ticks_skipped", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "scheduler_ticks", "bar_key", "TEXT")
            self._ensure_column(conn, "scheduler_ticks", "bar_timestamp", "TEXT")
            self._ensure_column(conn, "scheduler_ticks", "skip_reason", "TEXT")
        # 老库迁移:去掉 scheduler_tasks.sleeve_id 列(列补齐并提交后再重建)。
        # 用独立的 foreign_keys=OFF + legacy_alter_table=ON 连接:重命名时不改写
        # scheduler_ticks 的外键引用名,删旧表也不被外键拦住;失败整体回滚。
        self._migrate_drop_sleeve()

    def _migrate_drop_sleeve(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        try:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(scheduler_tasks)").fetchall()]
            if cols and "sleeve_id" in cols:
                conn.execute("DROP TABLE IF EXISTS scheduler_tasks_legacy")
                keep = ", ".join(c for c in cols if c != "sleeve_id")
                conn.execute("ALTER TABLE scheduler_tasks RENAME TO scheduler_tasks_legacy")
                conn.execute(_SCHED_TASKS_DDL)
                conn.execute(f"INSERT INTO scheduler_tasks ({keep}) SELECT {keep} FROM scheduler_tasks_legacy")
                conn.execute("DROP TABLE scheduler_tasks_legacy")
            # 自愈:若 scheduler_ticks 的外键被早期半途迁移改写指向了 *_legacy(已不存在),
            # 整表重建以恢复指向 scheduler_tasks 的正确外键。
            refs = {r["table"] for r in conn.execute("PRAGMA foreign_key_list(scheduler_ticks)").fetchall()}
            if any(str(t).endswith("_legacy") for t in refs):
                tcols = [r["name"] for r in conn.execute("PRAGMA table_info(scheduler_ticks)").fetchall()]
                keep = ", ".join(tcols)
                conn.execute("DROP TABLE IF EXISTS scheduler_ticks_legacy")
                conn.execute("ALTER TABLE scheduler_ticks RENAME TO scheduler_ticks_legacy")
                conn.execute(_SCHED_TICKS_DDL)
                conn.execute(f"INSERT INTO scheduler_ticks ({keep}) SELECT {keep} FROM scheduler_ticks_legacy")
                conn.execute("DROP TABLE scheduler_ticks_legacy")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def seed_demo(self) -> None:
        if self.list_tasks():
            return
        try:
            self.create_task(
                {
                    "id": "sched_demo_5m",
                    "name": "Demo 5m Live Loop",
                    "account_id": "acct_a_share_alpha",
                    "strategy_id": "strategy_demo_momentum",
                    "timing_strategy_id": "timing_demo_regime",
                    "data_source": "fixture",
                    "symbols": "000001.SZ",
                    "frequency": "5m",
                    "interval_seconds": 300,
                    "bar_limit": 8,
                },
                seed=True,
            )
        except ValueError:
            return

    def create_task(self, payload: dict[str, Any], *, seed: bool = False) -> dict[str, Any]:
        account_id = _required(payload, "account_id")
        strategy_id = _required(payload, "strategy_id")
        timing_strategy_id = payload.get("timing_strategy_id") or None
        account = self.trading_store.get_account(account_id)
        if not account:
            raise ValueError(f"unknown account_id: {account_id}")
        if not self.strategy_store.get_strategy(strategy_id):
            raise ValueError(f"unknown strategy_id: {strategy_id}")
        if timing_strategy_id and not self.timing_store.get_timing_strategy(str(timing_strategy_id)):
            raise ValueError(f"unknown timing_strategy_id: {timing_strategy_id}")

        symbols = _symbols(payload.get("symbols") or ["000001.SZ"])
        frequency = normalize_frequency(payload.get("frequency") or "5m")
        interval_seconds = _float(payload.get("interval_seconds"), _default_interval(frequency))
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        bar_limit = int(_float(payload.get("bar_limit"), 8))
        if bar_limit <= 0:
            raise ValueError("bar_limit must be positive")
        calendar = (payload.get("calendar") or "CN_A").upper()
        if calendar != "CN_A":
            raise ValueError("calendar currently supports CN_A only")

        task_id = payload.get("id") or f"sched_{uuid.uuid4().hex[:10]}"
        now = _now()
        task = {
            "id": task_id,
            "name": payload.get("name") or f"{strategy_id} live loop",
            "account_id": account_id,
            "strategy_id": strategy_id,
            "timing_strategy_id": timing_strategy_id,
            "data_source": (payload.get("data_source") or app_settings.default_data_source()).lower(),
            "symbols": symbols,
            "frequency": frequency,
            "interval_seconds": interval_seconds,
            "bar_limit": bar_limit,
            "calendar": calendar,
            "calendar_enabled": 1 if payload.get("calendar_enabled", True) else 0,
            "dedupe_bars": 1 if payload.get("dedupe_bars", True) else 0,
            "last_bar_key": None,
            "last_bar_at": None,
            "status": "stopped",
            "ticks_started": 0,
            "ticks_completed": 0,
            "ticks_skipped": 0,
            "last_tick_at": None,
            "next_tick_at": None,
            "last_error": None,
            "created_at": now,
            "updated_at": now,
        }
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO scheduler_tasks (
                    id, name, account_id, strategy_id, timing_strategy_id,
                    data_source, symbols, frequency, interval_seconds, bar_limit,
                    calendar, calendar_enabled, dedupe_bars, last_bar_key,
                    last_bar_at, status, ticks_started, ticks_completed,
                    ticks_skipped, last_tick_at, next_tick_at, last_error,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task["id"],
                    task["name"],
                    task["account_id"],
                    task["strategy_id"],
                    task["timing_strategy_id"],
                    task["data_source"],
                    json.dumps(task["symbols"], ensure_ascii=False),
                    task["frequency"],
                    task["interval_seconds"],
                    task["bar_limit"],
                    task["calendar"],
                    task["calendar_enabled"],
                    task["dedupe_bars"],
                    task["last_bar_key"],
                    task["last_bar_at"],
                    task["status"],
                    task["ticks_started"],
                    task["ticks_completed"],
                    task["ticks_skipped"],
                    task["last_tick_at"],
                    task["next_tick_at"],
                    task["last_error"],
                    task["created_at"],
                    task["updated_at"],
                ),
            )

        if timing_strategy_id:
            self.timing_store.bind_strategy(
                str(timing_strategy_id),
                {
                    "strategy_id": strategy_id,
                    "account_id": account_id,
                    "active": True,
                },
            )

        if not seed:
            self.audit_store.record_event(
                AuditEvent(
                    timestamp=now,
                    ledger_type="system",
                    event_type="scheduler_task_created",
                    account_id=account_id,
                    strategy_id=strategy_id,
                    reason="live scheduler task created",
                    metadata={
                        "task_id": task_id,
                        "timing_strategy_id": timing_strategy_id,
                        "frequency": frequency,
                        "symbols": symbols,
                        "calendar": calendar,
                        "calendar_enabled": bool(task["calendar_enabled"]),
                        "dedupe_bars": bool(task["dedupe_bars"]),
                    },
                )
            )
        return self.get_task(task_id) or task

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM scheduler_tasks ORDER BY created_at ASC").fetchall()
        return [self._decode_task(row) for row in rows]

    def list_ticks(self, task_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT rowid, * FROM scheduler_ticks {where} ORDER BY started_at DESC, rowid DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._decode_tick(row) for row in rows]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM scheduler_tasks WHERE id = ?", (task_id,)).fetchone()
        return self._decode_task(row) if row else None

    def start_task(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"unknown scheduler task_id: {task_id}")
        with self._lock:
            if task_id in self._threads and self._threads[task_id].is_alive():
                return self._set_status(task_id, "running", next_tick_at=task.get("next_tick_at"))
            stop_event = threading.Event()
            thread = threading.Thread(target=self._run_loop, args=(task_id, stop_event), daemon=True)
            self._stop_events[task_id] = stop_event
            self._threads[task_id] = thread
            next_tick_at = _now()
            self._set_status(task_id, "running", next_tick_at=next_tick_at)
            self.audit_store.record_event(
                AuditEvent(
                    timestamp=next_tick_at,
                    ledger_type="system",
                    event_type="scheduler_task_started",
                    account_id=task["account_id"],
                    strategy_id=task["strategy_id"],
                    reason="live scheduler task started",
                    metadata={"task_id": task_id, "interval_seconds": task["interval_seconds"]},
                )
            )
            thread.start()
        return self.get_task(task_id) or task

    def stop_task(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"unknown scheduler task_id: {task_id}")
        thread = None
        with self._lock:
            stop_event = self._stop_events.get(task_id)
            if stop_event:
                stop_event.set()
            thread = self._threads.get(task_id)
            self._set_status(task_id, "stopped", next_tick_at=None)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        self.audit_store.record_event(
            AuditEvent(
                timestamp=_now(),
                ledger_type="system",
                event_type="scheduler_task_stopped",
                account_id=task["account_id"],
                strategy_id=task["strategy_id"],
                reason="live scheduler task stopped",
                metadata={"task_id": task_id},
            )
        )
        return self.get_task(task_id) or task

    def tick_once(self, task_id: str, *, force: bool = False, now: str | datetime | None = None) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"unknown scheduler task_id: {task_id}")
        tick_id = f"sched_tick_{uuid.uuid4().hex[:10]}"
        started_at = _coerce_timestamp(now) if now else _now()
        self._insert_tick(tick_id, task_id, "running", started_at)
        self._increment_task(task_id, ticks_started=1, last_tick_at=started_at, last_error=None)
        self.audit_store.record_event(
            AuditEvent(
                timestamp=started_at,
                ledger_type="system",
                event_type="scheduler_tick_started",
                account_id=task["account_id"],
                strategy_id=task["strategy_id"],
                run_id=tick_id,
                reason="live scheduler tick started",
                metadata={
                    "task_id": task_id,
                    "frequency": task["frequency"],
                    "data_source": task["data_source"],
                    "symbols": task["symbols"],
                    "force": force,
                },
            )
        )

        timing_run_id = None
        strategy_run_id = None
        decisions_recorded = 0
        orders_submitted = 0
        bar_snapshot: dict[str, Any] | None = None
        try:
            calendar_status = _calendar_status(task, started_at)
            if not force and not calendar_status["is_open"]:
                return self._skip_tick(
                    task,
                    tick_id,
                    started_at,
                    reason=calendar_status["reason"],
                    metadata={"calendar": calendar_status},
                )

            bar_snapshot = self._bar_snapshot(task)
            if task.get("dedupe_bars") and task.get("last_bar_key") == bar_snapshot["bar_key"]:
                return self._skip_tick(
                    task,
                    tick_id,
                    started_at,
                    reason="duplicate_bar",
                    bar_key=bar_snapshot["bar_key"],
                    bar_timestamp=bar_snapshot["bar_timestamp"],
                    metadata={"latest_bars": bar_snapshot["latest_bars"]},
                )

            if task.get("timing_strategy_id"):
                timing_result = self.timing_store.run_timing_strategy(
                    task["timing_strategy_id"],
                    {
                        "account_id": task["account_id"],
                        "strategy_id": task["strategy_id"],
                        "data_source": task["data_source"],
                        "symbols": task["symbols"],
                        "frequency": task["frequency"],
                        "bar_limit": task["bar_limit"],
                        "run_id": f"{tick_id}_timing",
                        "bars": bar_snapshot["bars"],
                    },
                )
                timing_run_id = timing_result.get("run_id")
                decisions_recorded = int(timing_result.get("decisions_recorded") or 0)
                if timing_result.get("status") == "failed":
                    raise RuntimeError(timing_result.get("error") or "timing strategy failed")

            strategy_result = self.strategy_store.run_strategy(
                task["strategy_id"],
                {
                    "account_id": task["account_id"],
                    "data_source": task["data_source"],
                    "symbols": task["symbols"],
                    "frequency": task["frequency"],
                    "bar_limit": task["bar_limit"],
                    "run_id": f"{tick_id}_strategy",
                    "bars": bar_snapshot["bars"],
                },
            )
            strategy_run_id = strategy_result.get("run_id")
            orders_submitted = int(strategy_result.get("orders_submitted") or 0)
            if strategy_result.get("status") == "failed":
                raise RuntimeError(strategy_result.get("error") or "stock strategy failed")

            finished_at = _now()
            latest_task = self.get_task(task_id)
            next_tick_at = _iso_after(finished_at, task["interval_seconds"]) if latest_task and latest_task["status"] == "running" else None
            self._finish_tick(
                tick_id,
                "completed",
                finished_at,
                timing_run_id=timing_run_id,
                strategy_run_id=strategy_run_id,
                decisions_recorded=decisions_recorded,
                orders_submitted=orders_submitted,
                bar_key=bar_snapshot["bar_key"],
                bar_timestamp=bar_snapshot["bar_timestamp"],
                error=None,
            )
            self._increment_task(
                task_id,
                ticks_completed=1,
                next_tick_at=next_tick_at,
                last_error=None,
                last_bar_key=bar_snapshot["bar_key"],
                last_bar_at=bar_snapshot["bar_timestamp"],
            )
            self.audit_store.record_event(
                AuditEvent(
                    timestamp=finished_at,
                    ledger_type="system",
                    event_type="scheduler_tick_completed",
                    account_id=task["account_id"],
                    strategy_id=task["strategy_id"],
                    run_id=tick_id,
                    reason="live scheduler tick completed",
                    metadata={
                        "task_id": task_id,
                        "timing_run_id": timing_run_id,
                        "strategy_run_id": strategy_run_id,
                        "decisions_recorded": decisions_recorded,
                        "orders_submitted": orders_submitted,
                        "bar_key": bar_snapshot["bar_key"],
                        "bar_timestamp": bar_snapshot["bar_timestamp"],
                    },
                )
            )
            self._record_nav_snapshot(task["account_id"], finished_at)
        except Exception as exc:  # noqa: BLE001 - scheduler must keep task inspectable after failures.
            finished_at = _now()
            error = str(exc)
            self._finish_tick(
                tick_id,
                "failed",
                finished_at,
                timing_run_id=timing_run_id,
                strategy_run_id=strategy_run_id,
                decisions_recorded=decisions_recorded,
                orders_submitted=orders_submitted,
                bar_key=bar_snapshot.get("bar_key") if bar_snapshot else None,
                bar_timestamp=bar_snapshot.get("bar_timestamp") if bar_snapshot else None,
                error=error,
            )
            self._increment_task(task_id, last_error=error)
            self.audit_store.record_event(
                AuditEvent(
                    timestamp=finished_at,
                    ledger_type="system",
                    event_type="scheduler_tick_failed",
                    account_id=task["account_id"],
                    strategy_id=task["strategy_id"],
                    run_id=tick_id,
                    reason=error,
                    metadata={"task_id": task_id, "timing_run_id": timing_run_id, "strategy_run_id": strategy_run_id},
                )
            )

        ticks = self.list_ticks(task_id, limit=1)
        return ticks[0] if ticks else {"id": tick_id, "task_id": task_id}

    def _record_nav_snapshot(self, account_id: str, timestamp: str) -> None:
        if not self.performance:
            return
        try:
            summary = self.trading_store.get_portfolio_summary(account_id)["accounts"]
            if not summary:
                return
            account = summary[0]
            self.performance.record_snapshot(
                account_id,
                equity=account["equity"],
                cash=account["total_cash"],
                market_value=account["market_value"],
                pnl=account["pnl"],
                pnl_pct=account["pnl_pct"],
                timestamp=timestamp,
                source="scheduler",
            )
        except Exception:  # noqa: BLE001 - 快照失败不应让 tick 失败。
            return

    def _skip_tick(
        self,
        task: dict[str, Any],
        tick_id: str,
        started_at: str,
        *,
        reason: str,
        bar_key: str | None = None,
        bar_timestamp: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        finished_at = _now()
        latest_task = self.get_task(task["id"])
        next_tick_at = (
            _iso_after(finished_at, task["interval_seconds"])
            if latest_task and latest_task["status"] == "running"
            else None
        )
        self._finish_tick(
            tick_id,
            "skipped",
            finished_at,
            timing_run_id=None,
            strategy_run_id=None,
            decisions_recorded=0,
            orders_submitted=0,
            bar_key=bar_key,
            bar_timestamp=bar_timestamp,
            skip_reason=reason,
            error=None,
        )
        self._increment_task(task["id"], ticks_skipped=1, next_tick_at=next_tick_at, last_error=None)
        self.audit_store.record_event(
            AuditEvent(
                timestamp=finished_at,
                ledger_type="system",
                event_type="scheduler_tick_skipped",
                account_id=task["account_id"],
                strategy_id=task["strategy_id"],
                run_id=tick_id,
                reason=reason,
                metadata={
                    "task_id": task["id"],
                    "bar_key": bar_key,
                    "bar_timestamp": bar_timestamp,
                    **(metadata or {}),
                },
            )
        )
        ticks = self.list_ticks(task["id"], limit=1)
        return ticks[0] if ticks else {"id": tick_id, "task_id": task["id"], "status": "skipped", "skip_reason": reason}

    def _bar_snapshot(self, task: dict[str, Any]) -> dict[str, Any]:
        connector = self.strategy_store.connectors.get(task["data_source"])
        bars = connector.get_bars(task["symbols"], frequency=task["frequency"], limit=int(task["bar_limit"]))
        if not bars:
            raise ValueError("data connector returned no bars")
        latest_by_symbol: dict[str, dict[str, Any]] = {}
        for bar in bars:
            symbol = str(bar.get("symbol") or "").upper()
            timestamp = str(bar.get("timestamp") or "")
            if not symbol or not timestamp:
                continue
            current = latest_by_symbol.get(symbol)
            if current is None or str(current.get("timestamp") or "") < timestamp:
                latest_by_symbol[symbol] = bar
        if not latest_by_symbol:
            raise ValueError("data connector returned bars without symbol/timestamp")
        latest_pairs = [(symbol, str(bar["timestamp"])) for symbol, bar in sorted(latest_by_symbol.items())]
        bar_key = "|".join(f"{symbol}@{timestamp}" for symbol, timestamp in latest_pairs)
        bar_timestamp = max(timestamp for _, timestamp in latest_pairs)
        return {
            "bars": bars,
            "latest_bars": [{"symbol": symbol, "timestamp": timestamp} for symbol, timestamp in latest_pairs],
            "bar_key": f"{task['data_source']}|{task['frequency']}|{bar_key}",
            "bar_timestamp": bar_timestamp,
        }

    def _run_loop(self, task_id: str, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            task = self.get_task(task_id)
            if not task or task["status"] != "running":
                break
            self.tick_once(task_id)
            task = self.get_task(task_id)
            interval = float(task["interval_seconds"]) if task else 1.0
            stop_event.wait(interval)
        with self._lock:
            self._threads.pop(task_id, None)
            self._stop_events.pop(task_id, None)

    def _set_status(self, task_id: str, status: str, next_tick_at: str | None) -> dict[str, Any]:
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE scheduler_tasks
                SET status = ?, next_tick_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, next_tick_at, _now(), task_id),
            )
        return self.get_task(task_id) or {}

    def _insert_tick(self, tick_id: str, task_id: str, status: str, started_at: str) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO scheduler_ticks (
                    id, task_id, status, started_at, finished_at,
                    timing_run_id, strategy_run_id, decisions_recorded,
                    orders_submitted, error, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (tick_id, task_id, status, started_at, None, None, None, 0, 0, None, "{}"),
            )

    def _finish_tick(
        self,
        tick_id: str,
        status: str,
        finished_at: str,
        *,
        timing_run_id: str | None,
        strategy_run_id: str | None,
        decisions_recorded: int,
        orders_submitted: int,
        bar_key: str | None = None,
        bar_timestamp: str | None = None,
        skip_reason: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE scheduler_ticks
                SET status = ?, finished_at = ?, timing_run_id = ?,
                    strategy_run_id = ?, decisions_recorded = ?,
                    orders_submitted = ?, bar_key = ?, bar_timestamp = ?,
                    skip_reason = ?, error = ?, metadata = ?
                WHERE id = ?
                """,
                (
                    status,
                    finished_at,
                    timing_run_id,
                    strategy_run_id,
                    decisions_recorded,
                    orders_submitted,
                    bar_key,
                    bar_timestamp,
                    skip_reason,
                    error,
                    json.dumps(
                        {
                            "timing_run_id": timing_run_id,
                            "strategy_run_id": strategy_run_id,
                            "bar_key": bar_key,
                            "bar_timestamp": bar_timestamp,
                            "skip_reason": skip_reason,
                        },
                        ensure_ascii=False,
                    ),
                    tick_id,
                ),
            )

    def _increment_task(
        self,
        task_id: str,
        *,
        ticks_started: int = 0,
        ticks_completed: int = 0,
        ticks_skipped: int = 0,
        last_tick_at: str | None = None,
        next_tick_at: str | None = None,
        last_error: str | None = None,
        last_bar_key: str | None = None,
        last_bar_at: str | None = None,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE scheduler_tasks
                SET ticks_started = ticks_started + ?,
                    ticks_completed = ticks_completed + ?,
                    ticks_skipped = ticks_skipped + ?,
                    last_tick_at = COALESCE(?, last_tick_at),
                    next_tick_at = ?,
                    last_error = ?,
                    last_bar_key = COALESCE(?, last_bar_key),
                    last_bar_at = COALESCE(?, last_bar_at),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    ticks_started,
                    ticks_completed,
                    ticks_skipped,
                    last_tick_at,
                    next_tick_at,
                    last_error,
                    last_bar_key,
                    last_bar_at,
                    _now(),
                    task_id,
                ),
            )

    @staticmethod
    def _decode_task(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["symbols"] = json.loads(item["symbols"] or "[]")
        item["calendar_enabled"] = bool(item.get("calendar_enabled", 1))
        item["dedupe_bars"] = bool(item.get("dedupe_bars", 1))
        return item

    @staticmethod
    def _decode_tick(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = json.loads(item.get("metadata") or "{}")
        return item


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_timestamp(value: str | datetime) -> str:
    if isinstance(value, datetime):
        item = value
    else:
        item = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if item.tzinfo is None:
        item = item.replace(tzinfo=timezone.utc)
    return item.isoformat()


def _calendar_status(task: dict[str, Any], timestamp: str) -> dict[str, Any]:
    if not task.get("calendar_enabled", True):
        return {"is_open": True, "reason": "calendar_disabled", "calendar": task.get("calendar", "CN_A")}
    if task.get("calendar", "CN_A") != "CN_A":
        return {"is_open": False, "reason": "unsupported_calendar", "calendar": task.get("calendar")}
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(CN_TZ)
    if local.weekday() >= 5:
        return {"is_open": False, "reason": "weekend_closed", "calendar": "CN_A", "local_time": local.isoformat()}
    minutes = local.hour * 60 + local.minute
    morning_open = 9 * 60 + 30 <= minutes <= 11 * 60 + 30
    afternoon_open = 13 * 60 <= minutes <= 15 * 60
    if not (morning_open or afternoon_open):
        return {"is_open": False, "reason": "outside_trading_session", "calendar": "CN_A", "local_time": local.isoformat()}
    return {"is_open": True, "reason": "trading_session_open", "calendar": "CN_A", "local_time": local.isoformat()}


def _iso_after(timestamp: str, seconds: float) -> str:
    base = datetime.fromisoformat(timestamp)
    return (base + timedelta(seconds=float(seconds))).isoformat()


def _required(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or value == "":
        raise ValueError(f"{key} is required")
    return str(value)


def _symbols(value: Any) -> list[str]:
    if isinstance(value, str):
        symbols = [item.strip().upper() for item in value.split(",") if item.strip()]
    else:
        symbols = [str(item).strip().upper() for item in value if str(item).strip()]
    if not symbols:
        raise ValueError("symbols must not be empty")
    return symbols


def _float(value: Any, default: float) -> float:
    if value is None or value == "":
        return float(default)
    return float(value)


def _default_interval(frequency: str) -> float:
    if frequency == "1m":
        return 60.0
    if frequency == "5m":
        return 300.0
    if frequency == "1d":
        return 86_400.0
    return 5.0
