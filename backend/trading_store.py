from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend import app_settings
from backend import events
from backend import friction
from backend import names as security_names
from backend import repo
from backend.audit_store import AuditEvent, AuditStore
from backend.data_connectors import normalize_frequency


# 策略描述附件:允许的类型 + 大小上限 + Content-Type 映射。
_MAX_FILE_BYTES = 25 * 1024 * 1024  # 25MB
_ALLOWED_FILE_EXT = {"pdf", "doc", "docx", "xls", "xlsx", "md", "markdown", "txt", "csv"}
_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "md": "text/markdown; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
}


def _content_type_for(ext: str) -> str:
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


DEFAULT_ACCOUNT = {
    "id": "acct_a_share_alpha",
    "name": "A-Share Alpha",
    "initial_cash": 10_000_000.0,
    "currency": "CNY",
    "market": "CN_A",
    "commission_rate": 0.00008,
    "min_commission": 5.0,
    "stamp_duty_rate": 0.001,
    "slippage_model": "adaptive",
    "slippage_value": 1.0,
    "auto_reverse_repo_enabled": 1,
    "reverse_repo_annual_rate": 0.018,
}


class TradingStore:
    def __init__(self, db_path: str | Path, audit_store: AuditStore):
        self.db_path = Path(db_path)
        self.audit_store = audit_store
        # risk gate 在 server 装配时注入(RiskStore 需要先有 paper_orders 表)；
        # 为 None 时跳过风控，保持旧行为。
        self.risk_store = None
        # data connector registry 同样由 server 注入；注入后下单可省略价格，
        # broker 自动按最新行情 close 定价(市价单语义)。
        self.connectors = None
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
        # 老库(含 sleeves 资金单元)迁移到单一账户现金模型;新库直接走下方 CREATE。
        # 迁移要重建表并丢弃 sleeves,涉及外键关系,故在独立的 foreign_keys=OFF 连接里跑,
        # 避免 DROP 父表时被旧外键拦住(且整段是一个事务,失败即全回滚)。
        self._migrate_drop_sleeves()
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    initial_cash REAL NOT NULL,
                    cash REAL NOT NULL,
                    currency TEXT NOT NULL,
                    market TEXT NOT NULL,
                    commission_rate REAL NOT NULL,
                    min_commission REAL NOT NULL,
                    stamp_duty_rate REAL NOT NULL,
                    slippage_model TEXT NOT NULL,
                    slippage_value REAL NOT NULL,
                    auto_reverse_repo_enabled INTEGER NOT NULL,
                    reverse_repo_annual_rate REAL NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    avg_cost REAL NOT NULL,
                    last_price REAL NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, symbol),
                    FOREIGN KEY(account_id) REFERENCES accounts(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_orders (
                    id TEXT PRIMARY KEY,
                    source_event_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    time_in_force TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    filled_quantity INTEGER NOT NULL,
                    remaining_quantity INTEGER NOT NULL,
                    signal_price REAL NOT NULL,
                    limit_price REAL,
                    last_fill_price REAL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(account_id) REFERENCES accounts(id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_paper_orders_filters
                ON paper_orders (account_id, strategy_id, symbol, status, created_at)
                """
            )
            # 国债逆回购独立账本:与审计流水分开,专供逆回购面板(每账户每日一条,upsert)。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reverse_repo_records (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    invest_amount REAL NOT NULL,
                    annual_rate REAL NOT NULL,
                    interest REAL NOT NULL,
                    source TEXT NOT NULL,
                    UNIQUE(account_id, trade_date)
                )
                """
            )
            self._ensure_column(conn, "reverse_repo_records", "rate_source", "TEXT NOT NULL DEFAULT 'custom'")
            # 账户加 owner(交易员标识),供 Admin 按人分组/排名;缺省空串,读取时回退到 name。
            self._ensure_column(conn, "accounts", "owner", "TEXT NOT NULL DEFAULT ''")
            # 账户加 description(策略描述文字,手动/AI 输入);Admin 点开账户可见。
            self._ensure_column(conn, "accounts", "description", "TEXT NOT NULL DEFAULT ''")
            # 策略描述附件(pdf/word/excel/md):字节存 BLOB,随数据目录走;Admin 代理下载查看。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS account_files (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    content BLOB NOT NULL,
                    uploaded_at TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id)
                )
                """
            )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _migrate_drop_sleeves(self) -> None:
        """把老库(账户未分配现金 + sleeve 资金单元)迁到单一 account.cash 模型。

        - 账户现金:cash = 原 unallocated_cash + 各 sleeve 的 available_cash 之和。
        - 持仓:去掉 sleeve_id,按 (account_id, symbol) 合并(数量相加、成本按量加权)。
        - 订单:去掉 sleeve_id 列。
        - 删除 sleeves 表。
        审计库(append-only)不动:历史行保留 sleeve_id 列,新行不再写。
        幂等:已是新结构(有 cash 列且无 sleeves 表)直接返回。
        在 foreign_keys=OFF 的独立连接里整体事务执行,失败即全回滚。
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._run_sleeve_migration(conn)
            conn.commit()
        finally:
            conn.close()

    def _run_sleeve_migration(self, conn: sqlite3.Connection) -> None:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "accounts" not in tables:
            return  # 全新安装,无需迁移
        # 清理可能残留的迁移中间表(上次迁移半途中断),保证可重入。
        for stale in ("accounts_legacy", "positions_legacy", "paper_orders_legacy"):
            conn.execute(f"DROP TABLE IF EXISTS {stale}")
        acct_cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        if "cash" in acct_cols and "sleeves" not in tables:
            return  # 已迁移

        has_sleeves = "sleeves" in tables
        # 1) accounts:折叠现金 + 把 unallocated_cash 重命名为 cash(整表重建,版本无关)。
        if "cash" not in acct_cols:
            owner_sel = "owner" if "owner" in acct_cols else "''"
            desc_sel = "description" if "description" in acct_cols else "''"
            sleeve_sum = (
                "COALESCE((SELECT SUM(available_cash) FROM sleeves WHERE sleeves.account_id = a.id), 0)"
                if has_sleeves
                else "0"
            )
            conn.execute("ALTER TABLE accounts RENAME TO accounts_legacy")
            conn.execute(
                """
                CREATE TABLE accounts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    initial_cash REAL NOT NULL,
                    cash REAL NOT NULL,
                    currency TEXT NOT NULL,
                    market TEXT NOT NULL,
                    commission_rate REAL NOT NULL,
                    min_commission REAL NOT NULL,
                    stamp_duty_rate REAL NOT NULL,
                    slippage_model TEXT NOT NULL,
                    slippage_value REAL NOT NULL,
                    auto_reverse_repo_enabled INTEGER NOT NULL,
                    reverse_repo_annual_rate REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    owner TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                f"""
                INSERT INTO accounts (
                    id, name, initial_cash, cash, currency, market, commission_rate,
                    min_commission, stamp_duty_rate, slippage_model, slippage_value,
                    auto_reverse_repo_enabled, reverse_repo_annual_rate, created_at, owner, description
                )
                SELECT id, name, initial_cash,
                    ROUND(unallocated_cash + {sleeve_sum}, 2),
                    currency, market, commission_rate, min_commission, stamp_duty_rate,
                    slippage_model, slippage_value, auto_reverse_repo_enabled,
                    reverse_repo_annual_rate, created_at, {owner_sel}, {desc_sel}
                FROM accounts_legacy a
                """
            )
            conn.execute("DROP TABLE accounts_legacy")

        # 2) positions:去 sleeve_id,按 (account_id, symbol) 合并。
        if "positions" in tables:
            pos_cols = {r["name"] for r in conn.execute("PRAGMA table_info(positions)").fetchall()}
            if "sleeve_id" in pos_cols:
                conn.execute("ALTER TABLE positions RENAME TO positions_legacy")
                conn.execute(
                    """
                    CREATE TABLE positions (
                        account_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        quantity INTEGER NOT NULL,
                        avg_cost REAL NOT NULL,
                        last_price REAL NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(account_id, symbol),
                        FOREIGN KEY(account_id) REFERENCES accounts(id)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO positions (account_id, symbol, quantity, avg_cost, last_price, updated_at)
                    SELECT account_id, symbol, SUM(quantity),
                        CASE WHEN SUM(quantity) > 0
                             THEN ROUND(SUM(quantity * avg_cost) / SUM(quantity), 4) ELSE 0 END,
                        (SELECT last_price FROM positions_legacy p2
                         WHERE p2.account_id = p.account_id AND p2.symbol = p.symbol
                         ORDER BY updated_at DESC LIMIT 1),
                        MAX(updated_at)
                    FROM positions_legacy p
                    GROUP BY account_id, symbol
                    HAVING SUM(quantity) > 0
                    """
                )
                conn.execute("DROP TABLE positions_legacy")

        # 3) paper_orders:去 sleeve_id 列。
        if "paper_orders" in tables:
            ord_cols = {r["name"] for r in conn.execute("PRAGMA table_info(paper_orders)").fetchall()}
            if "sleeve_id" in ord_cols:
                conn.execute("ALTER TABLE paper_orders RENAME TO paper_orders_legacy")
                conn.execute(
                    """
                    CREATE TABLE paper_orders (
                        id TEXT PRIMARY KEY,
                        source_event_id TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        strategy_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        side TEXT NOT NULL,
                        order_type TEXT NOT NULL,
                        time_in_force TEXT NOT NULL,
                        quantity INTEGER NOT NULL,
                        filled_quantity INTEGER NOT NULL,
                        remaining_quantity INTEGER NOT NULL,
                        signal_price REAL NOT NULL,
                        limit_price REAL,
                        last_fill_price REAL,
                        status TEXT NOT NULL,
                        reason TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        metadata TEXT NOT NULL DEFAULT '{}',
                        FOREIGN KEY(account_id) REFERENCES accounts(id)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO paper_orders (
                        id, source_event_id, account_id, strategy_id, run_id, symbol, side,
                        order_type, time_in_force, quantity, filled_quantity, remaining_quantity,
                        signal_price, limit_price, last_fill_price, status, reason,
                        created_at, updated_at, metadata
                    )
                    SELECT id, source_event_id, account_id, strategy_id, run_id, symbol, side,
                        order_type, time_in_force, quantity, filled_quantity, remaining_quantity,
                        signal_price, limit_price, last_fill_price, status, reason,
                        created_at, updated_at, metadata
                    FROM paper_orders_legacy
                    """
                )
                conn.execute("DROP TABLE paper_orders_legacy")

        # 4) 丢弃 sleeves 表。
        conn.execute("DROP TABLE IF EXISTS sleeves")

    def seed_demo(self) -> None:
        if self.get_account(DEFAULT_ACCOUNT["id"]):
            return
        account = self.create_account(DEFAULT_ACCOUNT, seed=True)
        # 演示持仓:600519 200 股 @1725.8;现金 = 初始资金 - 持仓成本,使初始净值=初始资金。
        qty, cost = 200, 1725.8
        spent = round(qty * cost, 2)
        with self._connection() as conn:
            conn.execute("UPDATE accounts SET cash = ROUND(cash - ?, 2) WHERE id = ?", (spent, account["id"]))
            conn.execute(
                """
                INSERT INTO positions (
                    account_id, symbol, quantity, avg_cost, last_price, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (account["id"], "600519.SH", qty, cost, cost, _now()),
            )

    def create_account(self, payload: dict[str, Any], *, seed: bool = False) -> dict[str, Any]:
        account_id = payload.get("id") or f"acct_{uuid.uuid4().hex[:10]}"
        now = _now()
        initial_cash = _float(payload.get("initial_cash"), 10_000_000.0)
        name = payload.get("name") or "Paper Account"
        account = {
            "id": account_id,
            "name": name,
            "owner": (str(payload.get("owner")).strip() if payload.get("owner") else "") or name,
            "initial_cash": initial_cash,
            "cash": initial_cash,
            "currency": payload.get("currency") or "CNY",
            "market": payload.get("market") or "CN_A",
            "commission_rate": _float(payload.get("commission_rate"), 0.00008),
            "min_commission": _float(payload.get("min_commission"), 5.0),
            "stamp_duty_rate": _float(payload.get("stamp_duty_rate"), 0.001),
            "slippage_model": payload.get("slippage_model") or "adaptive",
            "slippage_value": _float(payload.get("slippage_value"), 1.0),
            "auto_reverse_repo_enabled": 1 if payload.get("auto_reverse_repo_enabled", True) else 0,
            "reverse_repo_annual_rate": _float(payload.get("reverse_repo_annual_rate"), 0.018),
            "created_at": payload.get("created_at") or now,
        }
        if account["initial_cash"] <= 0:
            raise ValueError("initial_cash must be positive")
        if account["slippage_model"] not in {"bps", "fixed_tick", "adaptive"}:
            raise ValueError("slippage_model must be bps, fixed_tick or adaptive")

        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO accounts (
                    id, name, owner, initial_cash, cash, currency, market,
                    commission_rate, min_commission, stamp_duty_rate, slippage_model,
                    slippage_value, auto_reverse_repo_enabled, reverse_repo_annual_rate,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account["id"],
                    account["name"],
                    account["owner"],
                    account["initial_cash"],
                    account["cash"],
                    account["currency"],
                    account["market"],
                    account["commission_rate"],
                    account["min_commission"],
                    account["stamp_duty_rate"],
                    account["slippage_model"],
                    account["slippage_value"],
                    account["auto_reverse_repo_enabled"],
                    account["reverse_repo_annual_rate"],
                    account["created_at"],
                ),
            )

        if not seed:
            self.audit_store.record_event(
                AuditEvent(
                    timestamp=now,
                    ledger_type="system",
                    event_type="account_created",
                    account_id=account["id"],
                    amount=account["initial_cash"],
                    reason="paper account created",
                    metadata={
                        "name": account["name"],
                        "commission_rate": account["commission_rate"],
                        "stamp_duty_rate": account["stamp_duty_rate"],
                        "slippage_model": account["slippage_model"],
                        "slippage_value": account["slippage_value"],
                    },
                )
            )
        return self.get_account(account["id"]) or account

    def update_account(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """更新已有账户的可改配置(摩擦/滑点/逆回购/账户名)。

        initial_cash 不可改:它是净值基线、且现金已按它分配,事后改会让账本对不上。
        改动只对**之后的**成交生效;已结算的历史成交保留当时的费用,不回溯。
        """
        account = self.get_account(account_id)
        if not account:
            raise ValueError(f"unknown account_id: {account_id}")

        updatable = {
            "name": lambda v: str(v).strip() or account["name"],
            "owner": lambda v: str(v).strip() or account.get("owner") or account["name"],
            "commission_rate": lambda v: _float(v, account["commission_rate"]),
            "min_commission": lambda v: _float(v, account["min_commission"]),
            "stamp_duty_rate": lambda v: _float(v, account["stamp_duty_rate"]),
            "slippage_model": lambda v: str(v),
            "slippage_value": lambda v: _float(v, account["slippage_value"]),
            "reverse_repo_annual_rate": lambda v: _float(v, account["reverse_repo_annual_rate"]),
        }
        changes: dict[str, Any] = {}
        for field, coerce in updatable.items():
            if field in payload and payload[field] is not None:
                new_value = coerce(payload[field])
                if new_value != account[field]:
                    changes[field] = new_value
        if "auto_reverse_repo_enabled" in payload:
            new_flag = 1 if payload["auto_reverse_repo_enabled"] else 0
            if new_flag != account["auto_reverse_repo_enabled"]:
                changes["auto_reverse_repo_enabled"] = new_flag

        if changes.get("slippage_model", account["slippage_model"]) not in {"bps", "fixed_tick", "adaptive"}:
            raise ValueError("slippage_model must be bps, fixed_tick or adaptive")
        for rate_field in ("commission_rate", "stamp_duty_rate", "reverse_repo_annual_rate", "slippage_value"):
            if rate_field in changes and changes[rate_field] < 0:
                raise ValueError(f"{rate_field} 不能为负")

        if not changes:
            return account

        before = {field: account[field] for field in changes}
        assignments = ", ".join(f"{field} = ?" for field in changes)
        with self._connection() as conn:
            conn.execute(
                f"UPDATE accounts SET {assignments} WHERE id = ?",
                (*changes.values(), account_id),
            )
        self.audit_store.record_event(
            AuditEvent(
                timestamp=_now(),
                ledger_type="system",
                event_type="account_updated",
                account_id=account_id,
                reason="account config updated",
                metadata={"changed": list(changes.keys()), "before": before, "after": changes},
            )
        )
        return self.get_account(account_id) or account

    # ———————————————— 策略描述(文字 + 附件)————————————————
    def set_description(self, account_id: str, description: str) -> dict[str, Any]:
        if not self.get_account(account_id):
            raise ValueError(f"unknown account_id: {account_id}")
        with self._connection() as conn:
            conn.execute("UPDATE accounts SET description = ? WHERE id = ?", (str(description or ""), account_id))
        return self.get_description(account_id)

    def get_description(self, account_id: str) -> dict[str, Any]:
        account = self.get_account(account_id)
        if not account:
            raise ValueError(f"unknown account_id: {account_id}")
        return {
            "account_id": account_id,
            "description": account.get("description") or "",
            "files": self.list_files(account_id),
        }

    def list_files(self, account_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id, filename, content_type, size, uploaded_at FROM account_files "
                "WHERE account_id = ? ORDER BY uploaded_at ASC",
                (account_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_file(self, account_id: str, filename: str, content: bytes) -> dict[str, Any]:
        if not self.get_account(account_id):
            raise ValueError(f"unknown account_id: {account_id}")
        filename = str(filename or "").strip() or "未命名"
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in _ALLOWED_FILE_EXT:
            raise ValueError(f"不支持的文件类型 .{ext};仅 pdf/word/excel/md/txt/csv")
        if len(content) > _MAX_FILE_BYTES:
            raise ValueError(f"文件过大({len(content)} 字节),上限 {_MAX_FILE_BYTES // (1024 * 1024)}MB")
        file_id = f"file_{uuid.uuid4().hex[:12]}"
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO account_files (id, account_id, filename, content_type, size, content, uploaded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (file_id, account_id, filename, _content_type_for(ext), len(content), content, _now()),
            )
        return {"id": file_id, "filename": filename, "content_type": _content_type_for(ext),
                "size": len(content), "uploaded_at": _now()}

    def get_file(self, account_id: str, file_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT filename, content_type, content FROM account_files WHERE id = ? AND account_id = ?",
                (file_id, account_id),
            ).fetchone()
        if not row:
            return None
        return {"filename": row["filename"], "content_type": row["content_type"], "content": bytes(row["content"])}

    def delete_file(self, account_id: str, file_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            cur = conn.execute("DELETE FROM account_files WHERE id = ? AND account_id = ?", (file_id, account_id))
        return {"deleted": cur.rowcount > 0, "id": file_id}

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY created_at ASC").fetchall()
        accounts = [_row(row) for row in rows]
        return accounts

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return _row(row) if row else None

    def delete_account(self, account_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """删除一个账户及其全部子数据(持仓 / 订单 / 风控 / 择时绑定 / 调度任务)。

        安全护栏:账户仍有持仓时默认拒绝,需显式 force=true 才强删,避免误删在用账户。
        审计事件(历史流水)不随之清除——账本是 append-only,删账户不改写历史。
        """
        payload = payload or {}
        account = self.get_account(account_id)
        if not account:
            raise ValueError(f"unknown account_id: {account_id}")
        force = bool(payload.get("force", False))

        position_count = len(self.list_positions(account_id))
        if position_count > 0 and not force:
            raise ValueError(
                f"账户 {account_id} 仍有 {position_count} 个持仓;确认要删请传 force=true。"
            )

        # 同库里其它模块挂在 account_id 上的行,连带清理避免留孤儿(表可能在精简部署里不存在,先探测)。
        cleanup = [
            ("risk_configs", "account_id"),
            ("timing_strategy_runs", "account_id"),
            ("timing_strategy_bindings", "account_id"),
            ("timing_decisions", "account_id"),
            ("scheduler_tasks", "account_id"),
        ]
        with self._connection() as conn:
            existing = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if {"scheduler_ticks", "scheduler_tasks"} <= existing:
                conn.execute(
                    "DELETE FROM scheduler_ticks WHERE task_id IN (SELECT id FROM scheduler_tasks WHERE account_id = ?)",
                    (account_id,),
                )
            for table, column in cleanup:
                if table in existing:
                    conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (account_id,))
            # 核心外键顺序:positions / paper_orders -> accounts
            conn.execute("DELETE FROM positions WHERE account_id = ?", (account_id,))
            conn.execute("DELETE FROM paper_orders WHERE account_id = ?", (account_id,))
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

        self.audit_store.record_event(
            AuditEvent(
                timestamp=_now(),
                ledger_type="system",
                event_type="account_deleted",
                account_id=account_id,
                reason="paper account deleted",
                metadata={
                    "name": account["name"],
                    "removed_positions": position_count,
                    "forced": force,
                },
            )
        )
        return {
            "deleted": True,
            "id": account_id,
            "removed": {"positions": position_count},
        }

    def void_trade(self, account_id: str, trade_event_id: str, reason: str) -> dict[str, Any]:
        """作废一笔错误成交:反向冲回它对现金/持仓的影响,使账本与净值"如同该笔从未发生"。

        与"反向补录"不同——后者会在曲线上留下一笔虚假往返+费用。作废是把这笔从所有派生
        (净值重建/流水/盈亏)里彻底剔除。护栏:必须填原因;只能作废真实成交;不能重复作废;
        且整个动作以 trade_voided 事件记入 append-only 审计流水(可追溯、不可悄悄删)。
        """
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("作废交易必须填写原因")
        trade = self.audit_store.get_event(trade_event_id)
        if not trade or trade.get("event_type") != "trade_filled":
            raise ValueError("未找到该成交事件,无法作废")
        if trade.get("account_id") != account_id:
            raise ValueError("该成交不属于此账户")
        if trade_event_id in self.audit_store.voided_trade_event_ids(account_id):
            raise ValueError("该成交已作废,请勿重复操作")

        symbol = trade["symbol"]
        side = str((trade.get("metadata") or {}).get("side") or "BUY").upper()
        qty = int(trade["quantity"])
        price = float(trade["price"])
        gross = round(qty * price, 2)
        chain = self.audit_store.get_chain(trade_event_id)
        fees = round(
            sum(-float(e.get("amount") or 0.0) for e in chain.get("cash_changes", [])
                if e["event_type"] in {"commission", "stamp_duty", "slippage"}),
            2,
        )
        # 原始这笔对账户现金的影响:BUY 减 gross+fees;SELL 加 gross-fees。作废=反向冲回。
        original_cash_delta = (-(gross) - fees) if side == "BUY" else (gross - fees)

        # 先把作废事件记入审计(此后 voided 集合即含本笔),再据"剩余未作废成交"重算持仓。
        self.audit_store.record_event(
            AuditEvent(
                timestamp=_now(),
                ledger_type="system",
                event_type="trade_voided",
                account_id=account_id,
                strategy_id=trade.get("strategy_id"),
                symbol=symbol,
                quantity=qty,
                price=price,
                amount=gross,
                reason=reason,
                source_event_id=trade.get("source_event_id"),
                metadata={
                    "trade_event_id": trade_event_id,
                    "side": side,
                    "fees": fees,
                    "reversed_cash": round(-original_cash_delta, 2),
                },
            )
        )

        new_qty, new_avg = self._replay_position(account_id, symbol)
        with self._connection() as conn:
            conn.execute(
                "UPDATE accounts SET cash = ROUND(cash - ?, 2) WHERE id = ?",
                (original_cash_delta, account_id),
            )
            if new_qty > 0:
                conn.execute(
                    "UPDATE positions SET quantity = ?, avg_cost = ?, updated_at = ? "
                    "WHERE account_id = ? AND symbol = ?",
                    (new_qty, round(new_avg, 4), _now(), account_id, symbol),
                )
            else:
                conn.execute(
                    "DELETE FROM positions WHERE account_id = ? AND symbol = ?",
                    (account_id, symbol),
                )
        return {
            "voided": True,
            "trade_event_id": trade_event_id,
            "symbol": symbol,
            "side": side,
            "quantity": qty,
            "reversed_cash": round(-original_cash_delta, 2),
            "position_after": new_qty,
        }

    def _replay_position(self, account_id: str, symbol: str) -> tuple[int, float]:
        """按时间顺序重放该 account+symbol 的未作废成交,得到(数量, 平均成本)。"""
        voided = self.audit_store.voided_trade_event_ids(account_id)
        fills = self.audit_store.list_events(
            {
                "event_type": "trade_filled",
                "account_id": account_id,
                "symbol": symbol,
                "limit": "100000",
            }
        )
        fills.sort(key=lambda e: (e["timestamp"], e["id"]))
        qty = 0
        avg = 0.0
        for fill in fills:
            if fill["id"] in voided:
                continue
            fq = int(fill["quantity"])
            fp = float(fill["price"])
            fside = str((fill.get("metadata") or {}).get("side") or "BUY").upper()
            if fside == "BUY":
                total = qty + fq
                avg = (avg * qty + fp * fq) / total if total else 0.0
                qty = total
            else:
                qty -= fq
                if qty <= 0:
                    qty = 0
                    avg = 0.0
        return qty, avg

    def list_positions(self, account_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM positions WHERE account_id = ? ORDER BY symbol ASC", (account_id,)).fetchall()
        return [_row(row) for row in rows]

    def get_portfolio_summary(
        self,
        account_id: str | None = None,
        *,
        mark_prices: dict[str, dict[str, Any]] | None = None,
        mark_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        accounts = [self.get_account(account_id)] if account_id else self.list_accounts()
        accounts = [account for account in accounts if account]
        if account_id and not accounts:
            raise ValueError(f"unknown account_id: {account_id}")

        summaries = [self._portfolio_for_account(account, mark_prices or {}) for account in accounts]
        totals = _portfolio_totals(summaries)
        return {
            "accounts": summaries,
            "totals": totals,
            "mark": mark_metadata or {"mode": "position_last_price"},
        }

    def list_position_symbols(self, account_id: str | None = None) -> list[str]:
        clauses: list[str] = []
        params: list[Any] = []
        if account_id:
            clauses.append("account_id = ?")
            params.append(account_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(f"SELECT DISTINCT symbol FROM positions {where} ORDER BY symbol ASC", params).fetchall()
        return [str(row["symbol"]) for row in rows]

    def list_orders(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        filters = filters or {}
        clauses: list[str] = []
        params: list[Any] = []
        for field in ("account_id", "strategy_id", "symbol", "status"):
            value = filters.get(field)
            if value:
                clauses.append(f"{field} = ?")
                params.append(str(value).upper() if field == "symbol" else value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = int(filters.get("limit") or 200)
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM paper_orders {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [_decode_order(row) for row in rows]

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM paper_orders WHERE id = ?", (order_id,)).fetchone()
        return _decode_order(row) if row else None

    def cancel_order(self, order_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        order = self.get_order(order_id)
        if not order:
            raise ValueError(f"unknown order_id: {order_id}")
        if order["status"] not in {"created", "submitted", "partially_filled"}:
            raise ValueError(f"order {order_id} cannot be cancelled from status {order['status']}")
        timestamp = payload.get("timestamp") or _now()
        reason = payload.get("reason") or "order cancelled by user"
        before_state = {
            "status": order["status"],
            "filled_quantity": order["filled_quantity"],
            "remaining_quantity": order["remaining_quantity"],
        }
        after_state = {
            "status": "cancelled",
            "filled_quantity": order["filled_quantity"],
            "remaining_quantity": order["remaining_quantity"],
        }
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE paper_orders
                SET status = ?, reason = ?, updated_at = ?
                WHERE id = ?
                """,
                ("cancelled", reason, timestamp, order_id),
            )
        event_id = self.audit_store.record_event(
            AuditEvent(
                timestamp=timestamp,
                ledger_type="order",
                event_type="order_cancelled",
                account_id=order["account_id"],
                strategy_id=order["strategy_id"],
                run_id=order["run_id"],
                symbol=order["symbol"],
                quantity=order["remaining_quantity"],
                price=order["signal_price"],
                before_state=before_state,
                after_state=after_state,
                reason=reason,
                source_event_id=order["source_event_id"],
                metadata={"order_id": order_id, "side": order["side"], "order_type": order["order_type"]},
            )
        )
        return {"cancelled": True, "order": self.get_order(order_id), "event_id": event_id}

    def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = _required(payload, "account_id")
        symbol = _required(payload, "symbol").upper()
        side = _required(payload, "side").upper()
        quantity = int(_float(payload.get("quantity"), 0.0))
        signal_price = _float(payload.get("signal_price"), _float(payload.get("price"), 0.0))
        price_source = "client"
        if signal_price <= 0 and self.connectors:
            # 市价单语义：客户端不报价时，按 connector 最新 close 定价。
            signal_price = self._latest_close(
                payload.get("data_source") or app_settings.default_data_source(),
                symbol,
                payload.get("frequency") or "5m",
            )
            price_source = f"{(payload.get('data_source') or app_settings.default_data_source()).lower()}_close"
        fill_price = _float(payload.get("fill_price"), signal_price)
        timestamp = payload.get("timestamp") or _now()
        strategy_id = payload.get("strategy_id") or "manual_strategy"
        run_id = payload.get("run_id") or f"manual_run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        timing_strategy_id = payload.get("timing_strategy_id") or "manual_timing_gate"
        allow_open = bool(payload.get("allow_open", True))
        position_policy = payload.get("position_policy") or "hold"
        order_id = payload.get("order_id") or f"ord_{uuid.uuid4().hex[:12]}"
        order_type = (payload.get("order_type") or "market").lower()
        time_in_force = (payload.get("time_in_force") or "day").upper()
        timing_reason = payload.get("timing_reason") or "timing strategy decision"
        timing_metadata = {
            "allow_open": allow_open,
            "position_policy": position_policy,
            "timing_decision_id": payload.get("timing_decision_id"),
            "timing_decision_event_id": payload.get("timing_decision_event_id"),
            "timing_binding_id": payload.get("timing_binding_id"),
        }

        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if quantity % 100 != 0:
            raise ValueError("A-share order quantity must be in 100-share lots")
        if signal_price <= 0 or fill_price <= 0:
            raise ValueError("price must be positive")
        if order_type not in {"market", "limit"}:
            raise ValueError("order_type must be market or limit")

        account = self.get_account(account_id)
        if not account:
            raise ValueError(f"unknown account_id: {account_id}")

        source_event_id = payload.get("source_event_id") or f"sig_{uuid.uuid4().hex[:12]}"
        self.audit_store.record_event(
            AuditEvent(
                event_id=source_event_id,
                timestamp=timestamp,
                ledger_type="decision",
                event_type="strategy_signal",
                account_id=account_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol=symbol,
                quantity=quantity,
                price=signal_price,
                reason=payload.get("signal_reason") or "manual strategy signal submitted",
                metadata={"frequency": payload.get("frequency", "5m"), "side": side, "price_source": price_source},
            )
        )
        self._create_order_record(
            order_id=order_id,
            source_event_id=source_event_id,
            account_id=account_id,
            strategy_id=strategy_id,
            run_id=run_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            time_in_force=time_in_force,
            quantity=quantity,
            signal_price=signal_price,
            limit_price=payload.get("limit_price"),
            timestamp=timestamp,
            reason=payload.get("signal_reason") or "order created from strategy signal",
            metadata={"frequency": payload.get("frequency", "5m")},
        )

        opening_blocked = side == "BUY" and (not allow_open or position_policy in {"reduce_only", "close_all"})
        if opening_blocked:
            self.audit_store.record_event(
                AuditEvent(
                    timestamp=timestamp,
                    ledger_type="decision",
                    event_type="timing_blocked",
                    account_id=account_id,
                    strategy_id=timing_strategy_id,
                    run_id=run_id,
                    symbol=symbol,
                    quantity=quantity,
                    price=signal_price,
                    reason=timing_reason,
                    source_event_id=source_event_id,
                    metadata={**timing_metadata, "blocked_strategy": strategy_id},
                )
            )
            order_event_id = self._reject_order(
                order_id=order_id,
                account_id=account_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=signal_price,
                timestamp=timestamp,
                source_event_id=source_event_id,
                reason=f"blocked by {timing_strategy_id}",
            )
            return {
                "accepted": False,
                "reason": f"blocked by {timing_strategy_id}",
                "event_id": order_event_id,
                "source_event_id": source_event_id,
            }

        self.audit_store.record_event(
            AuditEvent(
                timestamp=timestamp,
                ledger_type="decision",
                event_type="timing_decision",
                account_id=account_id,
                strategy_id=timing_strategy_id,
                run_id=run_id,
                symbol=symbol,
                reason=timing_reason if payload.get("timing_reason") else "timing gate allowed order",
                source_event_id=source_event_id,
                metadata=timing_metadata,
            )
        )

        # 择时门控之后、broker 接单之前执行 pre-trade risk gate(交易前风控门控)。
        if self.risk_store:
            risk_breach = self.risk_store.evaluate_order(
                account=account,
                positions=self.list_positions(account_id),
                symbol=symbol,
                side=side,
                quantity=quantity,
                signal_price=signal_price,
                fill_price=fill_price,
                run_id=run_id,
                timestamp=timestamp,
            )
            if risk_breach:
                self.audit_store.record_event(
                    AuditEvent(
                        timestamp=timestamp,
                        ledger_type="decision",
                        event_type="risk_blocked",
                        account_id=account_id,
                        strategy_id=strategy_id,
                        run_id=run_id,
                        symbol=symbol,
                        amount=round(quantity * signal_price, 2),
                        quantity=quantity,
                        price=signal_price,
                        reason=risk_breach["reason"],
                        source_event_id=source_event_id,
                        metadata={**risk_breach, "side": side, "blocked_strategy": strategy_id},
                    )
                )
                order_event_id = self._reject_order(
                    order_id=order_id,
                    account_id=account_id,
                    strategy_id=strategy_id,
                    run_id=run_id,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=signal_price,
                    timestamp=timestamp,
                    source_event_id=source_event_id,
                    reason=risk_breach["reason"],
                )
                return {
                    "accepted": False,
                    "reason": risk_breach["reason"],
                    "risk": risk_breach,
                    "event_id": order_event_id,
                    "source_event_id": source_event_id,
                }

        position = self._get_position(account_id, symbol)
        available_cash = float(account["cash"])
        position_before = int(position["quantity"]) if position else 0
        avg_cost_before = float(position["avg_cost"]) if position else 0.0
        fill_quantity = _fill_quantity(payload.get("fill_quantity"), quantity)
        if fill_quantity and fill_quantity % 100 != 0:
            raise ValueError("A-share fill quantity must be in 100-share lots")
        gross_amount = round(fill_quantity * fill_price, 2)
        commission = max(round(gross_amount * account["commission_rate"], 2), account["min_commission"])
        if fill_quantity == 0:
            commission = 0.0
        stamp_duty = round(gross_amount * account["stamp_duty_rate"], 2) if side == "SELL" else 0.0
        slippage_cost = self._slippage_cost(account, symbol, fill_quantity, fill_price, payload)
        total_cost = round(commission + stamp_duty + slippage_cost, 2)
        cash_delta = -gross_amount - total_cost if side == "BUY" else gross_amount - total_cost

        if side == "BUY" and available_cash + cash_delta < -0.001:
            order_event_id = self._reject_order(
                order_id=order_id,
                account_id=account_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=signal_price,
                timestamp=timestamp,
                source_event_id=source_event_id,
                reason="insufficient cash",
            )
            return {"accepted": False, "reason": "insufficient cash", "event_id": order_event_id, "source_event_id": source_event_id}
        # 校验A(时序):显式(可能回溯)时间的卖单,按"该时点持仓"校验,防"未持有先卖"凭空造现金。
        sell_limit = position_before
        if payload.get("timestamp"):
            sell_limit = self.audit_store.net_position_asof(account_id, symbol, timestamp)
        if side == "SELL" and fill_quantity > sell_limit:
            reject_reason = (
                "insufficient position quantity"
                if sell_limit == position_before
                else f"卖出 {fill_quantity} 股,但 {str(timestamp)[:10]} 当时按时序只持有 {sell_limit} 股(不能卖出当时未持有的量)"
            )
            order_event_id = self._reject_order(
                order_id=order_id,
                account_id=account_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=signal_price,
                timestamp=timestamp,
                source_event_id=source_event_id,
                reason=reject_reason,
            )
            return {
                "accepted": False,
                "reason": reject_reason,
                "event_id": order_event_id,
                "source_event_id": source_event_id,
            }

        order_event_id = self.audit_store.record_event(
            AuditEvent(
                event_id=order_id,
                timestamp=timestamp,
                ledger_type="order",
                event_type="order_submitted",
                account_id=account_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol=symbol,
                amount=round(quantity * signal_price, 2),
                quantity=quantity,
                price=signal_price,
                reason="order accepted by paper broker",
                source_event_id=source_event_id,
                metadata={"order_id": order_id, "side": side, "order_type": order_type, "time_in_force": time_in_force, "timing_gate": "allowed"},
            )
        )
        self._update_order_status(
            order_id,
            status="submitted",
            filled_quantity=0,
            last_fill_price=None,
            timestamp=timestamp,
            reason="order accepted by paper broker",
        )

        if fill_quantity == 0:
            return {
                "accepted": True,
                "source_event_id": source_event_id,
                "order_event_id": order_event_id,
                "order_id": order_id,
                "order_status": "submitted",
                "filled_quantity": 0,
                "remaining_quantity": quantity,
                "cash_after": available_cash,
                "position_after": position_before,
                "costs": {
                    "commission": 0.0,
                    "stamp_duty": 0.0,
                    "slippage": 0.0,
                },
            }

        position_after = position_before + fill_quantity if side == "BUY" else position_before - fill_quantity
        if side == "BUY":
            avg_cost_after = round(
                ((position_before * avg_cost_before) + (fill_quantity * fill_price)) / position_after,
                4,
            )
        else:
            avg_cost_after = 0.0 if position_after == 0 else avg_cost_before

        self.audit_store.record_trade_settlement(
            account_id=account_id,
            strategy_id=strategy_id,
            run_id=run_id,
            symbol=symbol,
            side=side,
            quantity=fill_quantity,
            price=fill_price,
            timestamp=timestamp,
            source_event_id=source_event_id,
            cash_before=available_cash,
            position_before=position_before,
            avg_cost_before=avg_cost_before,
            avg_cost_after=avg_cost_after,
            commission=commission,
            stamp_duty=stamp_duty,
            slippage_cost=slippage_cost,
        )
        order_status = "filled" if fill_quantity == quantity else "partially_filled"
        self.audit_store.record_event(
            AuditEvent(
                timestamp=timestamp,
                ledger_type="order",
                event_type="order_filled" if order_status == "filled" else "order_partially_filled",
                account_id=account_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol=symbol,
                amount=round(fill_quantity * fill_price, 2),
                quantity=fill_quantity,
                price=fill_price,
                before_state={"filled_quantity": 0, "remaining_quantity": quantity},
                after_state={"filled_quantity": fill_quantity, "remaining_quantity": quantity - fill_quantity},
                reason="paper broker fill recorded",
                source_event_id=source_event_id,
                metadata={"order_id": order_id, "side": side, "order_status": order_status},
            )
        )
        self._update_order_status(
            order_id,
            status=order_status,
            filled_quantity=fill_quantity,
            last_fill_price=fill_price,
            timestamp=timestamp,
            reason="paper broker fill recorded",
        )

        available_cash_after = round(available_cash + cash_delta, 2)
        with self._connection() as conn:
            conn.execute("UPDATE accounts SET cash = ? WHERE id = ?", (available_cash_after, account_id))
            if position_after == 0:
                conn.execute(
                    "DELETE FROM positions WHERE account_id = ? AND symbol = ?",
                    (account_id, symbol),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO positions (
                        account_id, symbol, quantity, avg_cost, last_price, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, symbol)
                    DO UPDATE SET
                        quantity = excluded.quantity,
                        avg_cost = excluded.avg_cost,
                        last_price = excluded.last_price,
                        updated_at = excluded.updated_at
                    """,
                    (account_id, symbol, position_after, avg_cost_after, fill_price, timestamp),
                )

        if fill_quantity > 0:
            events.publish({
                "type": "trade_filled", "account_id": account_id,
                "symbol": symbol, "side": side, "quantity": fill_quantity, "price": fill_price,
                "timestamp": str(timestamp),
            })
        return {
            "accepted": True,
            "source_event_id": source_event_id,
            "order_event_id": order_event_id,
            "order_id": order_id,
            "order_status": order_status,
            "filled_quantity": fill_quantity,
            "remaining_quantity": quantity - fill_quantity,
            "cash_after": available_cash_after,
            "position_after": position_after,
            "costs": {
                "commission": commission,
                "stamp_duty": stamp_duty,
                "slippage": slippage_cost,
            },
        }

    def backfill_trade(self, payload: dict[str, Any]) -> dict[str, Any]:
        """交易历史补充：仅用于补录此前未被系统记录的【真实历史成交】。

        与正常策略下单(place_order)的关键区别——本方法**绕过择时/风控门控**,
        因为补录的是已经发生的既成事实,而不是一笔新决策;但它仍然严格维护账本一致性
        (现金、持仓数量、持仓成本),并给每条补录打上 backfill 标记写入审计链,便于和正常
        策略流水区分。

        严格门槛:symbol、price、side、quantity、trade_date 缺一不可,否则拒绝使用。
        正常模拟交易不应走这里;此功能只用于补历史,除回测外不要用它造交易。
        """
        account_id = _required(payload, "account_id")
        symbol = _required(payload, "symbol").upper()
        side = _required(payload, "side").upper()
        trade_date = _required(payload, "trade_date")
        # 价格、数量必须显式声明:补录里没有"市价自动定价"这种语义。
        if payload.get("price") in (None, ""):
            raise ValueError("price is required: 交易历史补充必须声明成交价格")
        if payload.get("quantity") in (None, ""):
            raise ValueError("quantity is required: 交易历史补充必须声明成交数量")
        price = float(payload["price"])
        quantity = int(_float(payload.get("quantity"), 0.0))
        apply_fees = bool(payload.get("apply_fees", True))
        note = str(payload.get("note") or "").strip()

        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if price <= 0:
            raise ValueError("price must be positive")
        timestamp = _trade_timestamp(trade_date, payload.get("trade_time"))

        account = self.get_account(account_id)
        if not account:
            raise ValueError(f"unknown account_id: {account_id}")

        position = self._get_position(account_id, symbol)
        position_before = int(position["quantity"]) if position else 0
        avg_cost_before = float(position["avg_cost"]) if position else 0.0
        available_cash = float(account["cash"])

        # 校验A(时序):卖出按"该交易日当时持仓"(成交事件按时序重建)校验,而非当前实时持仓。
        # 否则"6/15 卖出 6/16 才买入的票"会蒙混过关,凭空造出现金/负持仓。
        if side == "SELL":
            held_asof = self.audit_store.net_position_asof(account_id, symbol, timestamp)
            if quantity > held_asof:
                raise ValueError(
                    f"补录卖出 {quantity} 股,但按交易日时序,{trade_date} 当时该账户只持有 {symbol} {held_asof} 股;"
                    f"不能卖出当时还没买入的部分(请先按时间顺序补录买入)。"
                )

        # 校验B(价格哨兵):成交价偏离当日行情过大(超出 0.4x~2.5x)疑似错价,拦下。取不到行情则放行。
        self._guard_price_sanity(symbol, price, trade_date, payload.get("data_source"))

        gross_amount = round(quantity * price, 2)
        commission = stamp_duty = 0.0
        if apply_fees:
            commission = max(round(gross_amount * account["commission_rate"], 2), account["min_commission"])
            stamp_duty = round(gross_amount * account["stamp_duty_rate"], 2) if side == "SELL" else 0.0
        slippage_cost = 0.0  # 补录的是真实成交价,不再叠加滑点模型
        total_cost = round(commission + stamp_duty + slippage_cost, 2)
        cash_delta = -gross_amount - total_cost if side == "BUY" else gross_amount - total_cost

        if side == "BUY" and available_cash + cash_delta < -0.001:
            raise ValueError(
                f"补录买入需现金 {gross_amount + total_cost:.2f},但账户可用现金仅 {available_cash:.2f};"
                f"请核对补录数据。"
            )

        if side == "BUY":
            position_after = position_before + quantity
            avg_cost_after = round(((position_before * avg_cost_before) + (quantity * price)) / position_after, 4)
        else:
            position_after = position_before - quantity
            avg_cost_after = 0.0 if position_after == 0 else avg_cost_before

        strategy_id = "manual_backfill"
        run_id = payload.get("run_id") or f"backfill_{str(trade_date).replace('-', '')}_{uuid.uuid4().hex[:6]}"
        order_id = payload.get("order_id") or f"bf_{uuid.uuid4().hex[:12]}"
        source_event_id = f"bf_sig_{uuid.uuid4().hex[:12]}"
        backfill_meta = {"backfill": True, "note": note, "apply_fees": apply_fees}

        # 1) 补录声明事件:作为这条历史成交审计链的根。
        self.audit_store.record_event(
            AuditEvent(
                event_id=source_event_id,
                timestamp=timestamp,
                ledger_type="decision",
                event_type="trade_backfill_declared",
                account_id=account_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol=symbol,
                quantity=quantity,
                price=price,
                reason=note or "manual historical trade backfill",
                metadata={**backfill_meta, "side": side},
            )
        )
        # 2) 订单记录:直接置为 filled,order_type 标 backfill 以便 blotter 一眼识别。
        self._create_order_record(
            order_id=order_id,
            source_event_id=source_event_id,
            account_id=account_id,
            strategy_id=strategy_id,
            run_id=run_id,
            symbol=symbol,
            side=side,
            order_type="backfill",
            time_in_force="GTC",
            quantity=quantity,
            signal_price=price,
            limit_price=None,
            timestamp=timestamp,
            reason="historical trade backfilled",
            metadata=backfill_meta,
        )
        # 3) 结算:复用与正常成交相同的审计拆分(现金本金/佣金/印花税/持仓)。
        self.audit_store.record_trade_settlement(
            account_id=account_id,
            strategy_id=strategy_id,
            run_id=run_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            timestamp=timestamp,
            source_event_id=source_event_id,
            cash_before=available_cash,
            position_before=position_before,
            avg_cost_before=avg_cost_before,
            avg_cost_after=avg_cost_after,
            commission=commission,
            stamp_duty=stamp_duty,
            slippage_cost=slippage_cost,
        )
        self._update_order_status(
            order_id,
            status="filled",
            filled_quantity=quantity,
            last_fill_price=price,
            timestamp=timestamp,
            reason="historical trade backfilled",
        )

        # 4) 落地持仓与账户现金(与 place_order 成交路径一致)。
        available_cash_after = round(available_cash + cash_delta, 2)
        with self._connection() as conn:
            conn.execute("UPDATE accounts SET cash = ? WHERE id = ?", (available_cash_after, account_id))
            if position_after == 0:
                conn.execute(
                    "DELETE FROM positions WHERE account_id = ? AND symbol = ?",
                    (account_id, symbol),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO positions (
                        account_id, symbol, quantity, avg_cost, last_price, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, symbol)
                    DO UPDATE SET
                        quantity = excluded.quantity,
                        avg_cost = excluded.avg_cost,
                        last_price = excluded.last_price,
                        updated_at = excluded.updated_at
                    """,
                    (account_id, symbol, position_after, avg_cost_after, price, timestamp),
                )

        events.publish({
            "type": "trade_filled", "account_id": account_id,
            "symbol": symbol, "side": side, "quantity": quantity, "price": price,
            "timestamp": str(timestamp), "backfill": True,
        })
        return {
            "accepted": True,
            "backfill": True,
            "order_id": order_id,
            "source_event_id": source_event_id,
            "timestamp": timestamp,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "cash_after": available_cash_after,
            "position_after": position_after,
            "avg_cost_after": avg_cost_after,
            "costs": {"commission": commission, "stamp_duty": stamp_duty, "slippage": slippage_cost},
        }

    def run_reverse_repo(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """手动国债逆回购:记入独立逆回购账本(不进主审计流水),利息计入未分配现金。

        利率来源二选一:
          - rate_mode="market":按实时行情——拉逆回购品种(默认 GC001/204001.SH)最新年化利率;
          - rate_mode="custom"(默认):用 payload.annual_rate 或账户默认利率(保留自定义权限)。
        取不到实时行情时自动回退到自定义,不报错。交易日默认今天、时间当日 14:30。
        """
        account = self.get_account(account_id)
        if not account:
            raise ValueError(f"unknown account_id: {account_id}")
        if not account["auto_reverse_repo_enabled"]:
            raise ValueError("auto reverse repo is disabled for this account")
        # 可投金额=账户闲置现金,与自动逆回购口径一致。
        idle_cash = round(float(account["cash"]), 2)
        amount = _float(payload.get("amount"), idle_cash)
        if amount <= 0:
            raise ValueError("amount must be positive")
        if amount > idle_cash + 0.001:
            raise ValueError(f"逆回购金额 {amount:.2f} 超过账户可用闲置现金 {idle_cash:.2f}")

        repo_symbol = (payload.get("repo_symbol") or repo.DEFAULT_SYMBOL).upper()
        term = repo.term_days(repo_symbol)
        rate_mode = str(payload.get("rate_mode") or "custom").lower()
        custom_rate = _float(payload.get("annual_rate"), account["reverse_repo_annual_rate"])
        annual_rate = custom_rate
        rate_source = "custom"
        if rate_mode == "market" and self.connectors:
            quote = repo.fetch_latest_rate(self.connectors.get(payload.get("data_source")), repo_symbol)
            if quote:
                annual_rate = quote["annual_rate"]
                rate_source = f"market:{repo.name_of(repo_symbol)}"
            else:
                rate_source = "custom(行情取不到,回退)"

        trade_date = str(payload.get("trade_date") or "").strip() or _today_cn()
        timestamp = payload.get("timestamp") or _trade_timestamp(trade_date, "14:30")
        interest = round(amount * annual_rate * term / 365, 2)
        # 幂等:同一天若已有记录(自动或手动),只把利息差额计入现金,避免重复计息。
        prev_interest = self._existing_repo_interest(account_id, trade_date)
        self._upsert_repo_record(
            account_id=account_id,
            trade_date=trade_date,
            timestamp=timestamp,
            invest_amount=amount,
            annual_rate=annual_rate,
            interest=interest,
            source=str(payload.get("source") or "manual"),
            rate_source=rate_source,
        )
        with self._connection() as conn:
            conn.execute(
                "UPDATE accounts SET cash = ROUND(cash + ?, 2) WHERE id = ?",
                (round(interest - prev_interest, 2), account_id),
            )
        events.publish({
            "type": "reverse_repo", "account_id": account_id, "trade_date": trade_date,
            "invest_amount": amount, "annual_rate": annual_rate, "interest": interest,
            "rate_source": rate_source, "source": str(payload.get("source") or "manual"),
        })
        return {"trade_date": trade_date, "timestamp": timestamp, "invest_amount": amount,
                "annual_rate": annual_rate, "interest": interest, "rate_source": rate_source,
                "repo_symbol": repo_symbol, "term_days": term, "replaced": prev_interest > 0}

    def _existing_repo_interest(self, account_id: str, trade_date: str) -> float:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT interest FROM reverse_repo_records WHERE account_id = ? AND trade_date = ?",
                (account_id, trade_date),
            ).fetchone()
        return float(row["interest"]) if row else 0.0

    def _upsert_repo_record(
        self,
        *,
        account_id: str,
        trade_date: str,
        timestamp: str,
        invest_amount: float,
        annual_rate: float,
        interest: float,
        source: str,
        rate_source: str = "custom",
    ) -> None:
        """每账户每日一条逆回购记录(同日重复=覆盖)。"""
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO reverse_repo_records (
                    id, account_id, trade_date, timestamp, invest_amount, annual_rate, interest, source, rate_source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, trade_date) DO UPDATE SET
                    timestamp = excluded.timestamp,
                    invest_amount = excluded.invest_amount,
                    annual_rate = excluded.annual_rate,
                    interest = excluded.interest,
                    source = excluded.source,
                    rate_source = excluded.rate_source
                """,
                (
                    f"repo_{uuid.uuid4().hex[:12]}",
                    account_id,
                    trade_date,
                    timestamp,
                    round(invest_amount, 2),
                    annual_rate,
                    round(interest, 2),
                    source,
                    rate_source,
                ),
            )

    def sync_auto_repo(self, account_id: str, schedule: list[dict[str, Any]]) -> dict[str, Any]:
        """把 NAV 重建算出的逐日逆回购计划补进独立账本(source=auto)。

        幂等:已存在该日记录(手动或自动)就跳过,不覆盖手动条目。供"自动补全逆回购"用。
        """
        if not self.get_account(account_id):
            raise ValueError(f"unknown account_id: {account_id}")
        today = _today_cn()
        inserted = 0
        new_interest = 0.0
        reverted_interest = 0.0
        with self._connection() as conn:
            # 自愈:清掉"当日(及未来)"被提前自动补的逆回购——当日只能手动买,自动不该碰。
            # 手动记录(source!='auto')一律保留。删除时把当初记入未分配现金的利息原路扣回。
            stale = conn.execute(
                "SELECT interest FROM reverse_repo_records "
                "WHERE account_id = ? AND trade_date >= ? AND source = 'auto'",
                (account_id, today),
            ).fetchall()
            if stale:
                reverted_interest = round(sum(float(r["interest"]) for r in stale), 2)
                conn.execute(
                    "DELETE FROM reverse_repo_records "
                    "WHERE account_id = ? AND trade_date >= ? AND source = 'auto'",
                    (account_id, today),
                )
                if reverted_interest:
                    conn.execute(
                        "UPDATE accounts SET cash = ROUND(cash - ?, 2) WHERE id = ?",
                        (reverted_interest, account_id),
                    )
            existing = {
                row["trade_date"]
                for row in conn.execute(
                    "SELECT trade_date FROM reverse_repo_records WHERE account_id = ?", (account_id,)
                ).fetchall()
            }
            for entry in schedule:
                day = str(entry["trade_date"])
                # 双保险:即便上游计划里混进了当日,自动补全也只补当日之前。
                if day >= today or day in existing:
                    continue
                interest = round(float(entry.get("interest", 0.0)), 2)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO reverse_repo_records (
                        id, account_id, trade_date, timestamp, invest_amount, annual_rate, interest, source, rate_source
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'auto', ?)
                    """,
                    (
                        f"repo_{uuid.uuid4().hex[:12]}",
                        account_id,
                        day,
                        entry.get("timestamp") or f"{day}T14:30:00+08:00",
                        round(float(entry.get("principal", 0.0)), 2),
                        float(entry.get("annual_rate", 0.0)),
                        interest,
                        "market" if entry.get("rate_source") == "market" else "auto",
                        ),
                )
                inserted += 1
                new_interest += interest
            # 新补入的利息计入账户未分配现金(幂等:只计新增,重跑不重复计)。
            if new_interest:
                conn.execute(
                    "UPDATE accounts SET cash = ROUND(cash + ?, 2) WHERE id = ?",
                    (round(new_interest, 2), account_id),
                )
        if inserted or new_interest:
            events.publish({
                "type": "reverse_repo", "account_id": account_id, "source": "auto",
                "filled": inserted, "interest": round(new_interest, 2),
            })
        return {
            "filled": inserted,
            "total": len(schedule),
            "credited_interest": round(new_interest, 2),
            "removed_today": len(stale),
            "reverted_interest": reverted_interest,
        }

    def list_reverse_repo(self, account_id: str, limit: int = 750) -> dict[str, Any]:
        """逆回购面板数据:逐日记录 + 汇总(累计投入次数、累计利息)。"""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM reverse_repo_records WHERE account_id = ? ORDER BY trade_date DESC LIMIT ?",
                (account_id, limit),
            ).fetchall()
        records = [dict(row) for row in rows]
        total_interest = round(sum(float(r["interest"]) for r in records), 2)
        return {
            "account_id": account_id,
            "records": records,
            "summary": {
                "days": len(records),
                "total_interest": total_interest,
                "last_date": records[0]["trade_date"] if records else None,
            },
        }

    def _portfolio_for_account(self, account: dict[str, Any], mark_prices: dict[str, dict[str, Any]]) -> dict[str, Any]:
        positions: list[dict[str, Any]] = []
        market_value = 0.0
        cost_basis = 0.0
        unrealized_pnl = 0.0
        holdings_day_pnl = 0.0

        for position in self.list_positions(account["id"]):
            enriched = _enrich_position(position, account["id"], mark_prices)
            positions.append(enriched)
            market_value += enriched["market_value"]
            cost_basis += enriched["cost_basis"]
            unrealized_pnl += enriched["unrealized_pnl"]
            holdings_day_pnl += enriched.get("day_pnl") or 0.0

        cash = _money(account["cash"])
        equity = _money(cash + market_value)
        pnl = _money(equity - account["initial_cash"])
        return {
            "id": account["id"],
            "name": account["name"],
            "owner": account.get("owner") or account["name"],
            "description": account.get("description") or "",
            "currency": account["currency"],
            "market": account["market"],
            "initial_cash": _money(account["initial_cash"]),
            "cash": cash,
            "total_cash": cash,
            "market_value": _money(market_value),
            "cost_basis": _money(cost_basis),
            "unrealized_pnl": _money(unrealized_pnl),
            "holdings_day_pnl": _money(holdings_day_pnl),
            "equity": equity,
            "pnl": pnl,
            "pnl_pct": _ratio(pnl, account["initial_cash"]),
            "exposure": _ratio(market_value, equity),
            "positions": positions,
        }

    def _get_position(self, account_id: str, symbol: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM positions WHERE account_id = ? AND symbol = ?",
                (account_id, symbol),
            ).fetchone()
        return _row(row) if row else None

    def _reject_order(
        self,
        *,
        order_id: str,
        account_id: str,
        strategy_id: str,
        run_id: str,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        timestamp: str,
        source_event_id: str,
        reason: str,
    ) -> str:
        order = self.get_order(order_id)
        before_state = {
            "status": order["status"] if order else "created",
            "filled_quantity": order["filled_quantity"] if order else 0,
            "remaining_quantity": order["remaining_quantity"] if order else quantity,
        }
        after_state = {
            "status": "rejected",
            "filled_quantity": order["filled_quantity"] if order else 0,
            "remaining_quantity": order["remaining_quantity"] if order else quantity,
        }
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE paper_orders
                SET status = ?, reason = ?, updated_at = ?
                WHERE id = ?
                """,
                ("rejected", reason, timestamp, order_id),
            )

        # 推送拒单事件(风控/择时/现金/持仓/时序等各种拦截统一在此一处)。
        events.publish({
            "type": "order_rejected", "account_id": account_id,
            "symbol": symbol, "side": side, "quantity": quantity, "reason": reason,
            "timestamp": str(timestamp),
        })
        return self.audit_store.record_event(
            AuditEvent(
                timestamp=timestamp,
                ledger_type="order",
                event_type="order_rejected",
                account_id=account_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol=symbol,
                amount=round(quantity * price, 2),
                quantity=quantity,
                price=price,
                before_state=before_state,
                after_state=after_state,
                reason=reason,
                source_event_id=source_event_id,
                metadata={"order_id": order_id, "side": side, "order_type": order["order_type"] if order else "market"},
            )
        )

    def _create_order_record(
        self,
        *,
        order_id: str,
        source_event_id: str,
        account_id: str,
        strategy_id: str,
        run_id: str,
        symbol: str,
        side: str,
        order_type: str,
        time_in_force: str,
        quantity: int,
        signal_price: float,
        limit_price: Any,
        timestamp: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        limit_value = None if limit_price in {None, ""} else float(limit_price)
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO paper_orders (
                    id, source_event_id, account_id, strategy_id, run_id,
                    symbol, side, order_type, time_in_force, quantity, filled_quantity,
                    remaining_quantity, signal_price, limit_price, last_fill_price,
                    status, reason, created_at, updated_at, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    source_event_id,
                    account_id,
                    strategy_id,
                    run_id,
                    symbol,
                    side,
                    order_type,
                    time_in_force,
                    quantity,
                    0,
                    quantity,
                    signal_price,
                    limit_value,
                    None,
                    "created",
                    reason,
                    timestamp,
                    timestamp,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                ),
            )

        self.audit_store.record_event(
            AuditEvent(
                event_id=f"{order_id}_created",
                timestamp=timestamp,
                ledger_type="order",
                event_type="order_created",
                account_id=account_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol=symbol,
                amount=round(quantity * signal_price, 2),
                quantity=quantity,
                price=signal_price,
                before_state={},
                after_state={
                    "status": "created",
                    "filled_quantity": 0,
                    "remaining_quantity": quantity,
                },
                reason=reason,
                source_event_id=source_event_id,
                metadata={
                    **(metadata or {}),
                    "order_id": order_id,
                    "side": side,
                    "order_type": order_type,
                    "time_in_force": time_in_force,
                },
            )
        )

    def _update_order_status(
        self,
        order_id: str,
        *,
        status: str,
        filled_quantity: int,
        last_fill_price: float | None,
        timestamp: str,
        reason: str,
    ) -> dict[str, Any]:
        order = self.get_order(order_id)
        if not order:
            raise ValueError(f"unknown order_id: {order_id}")
        remaining_quantity = max(int(order["quantity"]) - int(filled_quantity), 0)
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE paper_orders
                SET filled_quantity = ?, remaining_quantity = ?, last_fill_price = ?,
                    status = ?, reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    int(filled_quantity),
                    remaining_quantity,
                    last_fill_price,
                    status,
                    reason,
                    timestamp,
                    order_id,
                ),
            )
        updated = self.get_order(order_id)
        if not updated:
            raise ValueError(f"unknown order_id: {order_id}")
        return updated

    def _guard_price_sanity(self, symbol: str, price: float, trade_date: str, data_source: Any) -> None:
        """成交价与当日行情偏离过大(超出 0.4x~2.5x)时拦截,疑似错价。

        A 股单日涨跌幅受限,正常成交价不可能偏离当日收盘 2.5 倍;偏离这么多基本是录错价
        (如把 6 元的票按 16 元卖)。取不到当日行情就放行,避免误伤离线/历史深度不足的场景。
        """
        if not self.connectors or price <= 0:
            return
        try:
            connector = self.connectors.get(data_source)
            bars = connector.get_bars([symbol], frequency="1d", limit=3, start=trade_date, end=trade_date)
            closes = [
                float(bar["close"])
                for bar in bars
                if str(bar.get("symbol", "")).upper() == symbol and float(bar.get("close") or 0) > 0
            ]
        except Exception:  # noqa: BLE001 - 取不到行情不拦截
            return
        if not closes:
            return
        market = closes[-1]
        if market > 0 and (price > market * 2.5 or price < market * 0.4):
            raise ValueError(
                f"成交价 {price} 与 {trade_date} 行情 {market:.2f} 偏离过大(应在 {market * 0.4:.2f}~{market * 2.5:.2f} 之间);"
                f"疑似录错价格,请核对后再补录。"
            )

    def _latest_close(self, data_source: str, symbol: str, frequency: str) -> float:
        if not self.connectors:
            raise ValueError("price is required: no data connectors attached for market pricing")
        connector = self.connectors.get(data_source)
        bars = connector.get_bars([symbol], frequency=normalize_frequency(frequency), limit=1)
        candidates = [bar for bar in bars if str(bar.get("symbol", "")).upper() == symbol]
        if not candidates:
            raise ValueError(f"no market price available for {symbol} from {data_source}")
        latest = max(candidates, key=lambda bar: str(bar.get("timestamp") or ""))
        close = float(latest["close"])
        if close <= 0:
            raise ValueError(f"invalid market price for {symbol} from {data_source}")
        return close

    def _slippage_cost(
        self,
        account: dict[str, Any],
        symbol: str,
        quantity: int,
        price: float,
        payload: dict[str, Any],
    ) -> float:
        model = account.get("slippage_model", "adaptive")
        ref_bars = None
        # 自适应模型需要近段日频 bar 估 ADV/σ;固定 ADV 用日频,和回测口径一致。
        if model == "adaptive" and self.connectors:
            try:
                data_source = payload.get("data_source") or app_settings.default_data_source()
                connector = self.connectors.get(data_source)
                bars = connector.get_bars([symbol], frequency="1d", limit=friction.DEFAULT_ADV_WINDOW)
                ref_bars = [bar for bar in bars if str(bar.get("symbol", "")).upper() == symbol]
                ref_bars.sort(key=lambda bar: str(bar.get("timestamp") or ""))
            except Exception:
                ref_bars = None  # 取不到行情就让 friction 退化为温和固定 bps
        return friction.slippage_cost(
            model,
            quantity=quantity,
            fill_price=price,
            slippage_value=account["slippage_value"],
            ref_bars=ref_bars,
        )


def _row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for key in ("auto_reverse_repo_enabled", "active"):
        if key in item:
            item[key] = bool(item[key])
    return item


def _decode_order(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["metadata"] = json.loads(item.get("metadata") or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
    return item


def _enrich_position(
    position: dict[str, Any],
    account_id: str,
    mark_prices: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    quantity = int(position["quantity"])
    avg_cost = float(position["avg_cost"])
    last_price = float(position["last_price"])
    mark = (mark_prices or {}).get(position["symbol"], {})
    mark_price = float(mark.get("price", last_price))
    market_value = _money(quantity * mark_price)
    cost_basis = _money(quantity * avg_cost)
    unrealized_pnl = _money(market_value - cost_basis)
    prev_close = mark.get("prev_close")
    # 当日盈亏:盯市价相对昨收的变动 × 持仓量(昨收缺失则记 0)。
    day_pnl = _money(quantity * (mark_price - float(prev_close))) if prev_close else 0.0
    return {
        "account_id": account_id,
        "symbol": position["symbol"],
        "name": security_names.resolve(position["symbol"]),
        "quantity": quantity,
        "avg_cost": avg_cost,
        "last_price": last_price,
        "mark_price": mark_price,
        "mark_timestamp": mark.get("timestamp"),
        "price_source": mark.get("data_source") or "position_last_price",
        "mark_frequency": mark.get("frequency"),
        "volatility": mark.get("volatility"),
        "market_value": market_value,
        "cost_basis": cost_basis,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pnl_pct": _ratio(unrealized_pnl, cost_basis),
        "prev_close": float(prev_close) if prev_close else None,
        "day_pnl": day_pnl,
        "updated_at": position["updated_at"],
    }


def _portfolio_totals(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    fields = [
        "initial_cash",
        "cash",
        "total_cash",
        "market_value",
        "cost_basis",
        "unrealized_pnl",
        "equity",
        "pnl",
    ]
    totals = {field: _money(sum(float(account[field]) for account in accounts)) for field in fields}
    totals["pnl_pct"] = _ratio(totals["pnl"], totals["initial_cash"])
    totals["exposure"] = _ratio(totals["market_value"], totals["equity"])
    totals["account_count"] = len(accounts)
    totals["position_count"] = sum(len(account["positions"]) for account in accounts)
    return totals


def _money(value: Any) -> float:
    return round(float(value or 0.0), 2)


def _ratio(numerator: Any, denominator: Any) -> float:
    denominator_value = float(denominator or 0.0)
    if abs(denominator_value) < 1e-12:
        return 0.0
    return round(float(numerator or 0.0) / denominator_value, 6)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_cn() -> str:
    """北京时区今天的日期(YYYY-MM-DD)。"""
    return datetime.now(CN_TZ).date().isoformat()


CN_TZ = timezone(timedelta(hours=8))  # A 股北京时间 UTC+8


def _trade_timestamp(trade_date: Any, trade_time: Any) -> str:
    """把用户声明的历史日期(至少 YYYY-MM-DD)+ 可选时间(HH:MM[:SS])转成带时区(北京)的 ISO 时间戳。

    交易历史补充只要求精确到日期;不给具体时间时,默认按当日 **9:30 早盘开盘**记——
    因为开仓通常在早盘,而闲置资金的逆回购在下午 14:30,默认早盘可避免与逆回购时序冲突。
    """
    date_str = str(trade_date).strip()
    try:
        base = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("trade_date 必须是 YYYY-MM-DD 格式(至少精确到日期)") from exc

    if trade_time in (None, ""):
        base = base.replace(hour=9, minute=30, second=0)
    else:
        parsed = None
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                parsed = datetime.strptime(str(trade_time).strip(), fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError("trade_time 必须是 HH:MM 或 HH:MM:SS 格式")
        base = base.replace(hour=parsed.hour, minute=parsed.minute, second=parsed.second)
    return base.replace(tzinfo=CN_TZ).isoformat()


def _required(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or value == "":
        raise ValueError(f"{key} is required")
    return str(value)


def _float(value: Any, default: float) -> float:
    if value is None or value == "":
        return float(default)
    return float(value)


def _fill_quantity(value: Any, order_quantity: int) -> int:
    if value is None or value == "":
        return int(order_quantity)
    fill_quantity = int(_float(value, 0.0))
    if fill_quantity < 0:
        raise ValueError("fill_quantity cannot be negative")
    if fill_quantity > order_quantity:
        raise ValueError("fill_quantity cannot exceed order quantity")
    return fill_quantity
