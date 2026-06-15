from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.audit_store import AuditEvent, AuditStore
from backend.data_connectors import normalize_frequency


DEFAULT_ACCOUNT = {
    "id": "acct_a_share_alpha",
    "name": "A-Share Alpha",
    "initial_cash": 10_000_000.0,
    "currency": "CNY",
    "market": "CN_A",
    "commission_rate": 0.00025,
    "min_commission": 5.0,
    "stamp_duty_rate": 0.0005,
    "slippage_model": "bps",
    "slippage_value": 2.0,
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
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    initial_cash REAL NOT NULL,
                    unallocated_cash REAL NOT NULL,
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
                CREATE TABLE IF NOT EXISTS sleeves (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    allocated_cash REAL NOT NULL,
                    available_cash REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    account_id TEXT NOT NULL,
                    sleeve_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    avg_cost REAL NOT NULL,
                    last_price REAL NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, sleeve_id, symbol),
                    FOREIGN KEY(account_id) REFERENCES accounts(id),
                    FOREIGN KEY(sleeve_id) REFERENCES sleeves(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_orders (
                    id TEXT PRIMARY KEY,
                    source_event_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    sleeve_id TEXT NOT NULL,
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
                    FOREIGN KEY(account_id) REFERENCES accounts(id),
                    FOREIGN KEY(sleeve_id) REFERENCES sleeves(id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_paper_orders_filters
                ON paper_orders (account_id, sleeve_id, strategy_id, symbol, status, created_at)
                """
            )
            self._ensure_column(conn, "sleeves", "active", "INTEGER NOT NULL DEFAULT 1")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def seed_demo(self) -> None:
        if self.get_account(DEFAULT_ACCOUNT["id"]):
            return
        account = self.create_account(DEFAULT_ACCOUNT, seed=True)
        self.create_sleeve(
            account["id"],
            {
                "id": "sleeve_value_5m",
                "name": "Value Rotation 5m",
                "strategy_id": "strategy_value_rotation",
                "allocated_cash": 2_000_000.0,
            },
            seed=True,
        )
        self.create_sleeve(
            account["id"],
            {
                "id": "sleeve_growth_5m",
                "name": "Growth Breakout 5m",
                "strategy_id": "strategy_growth_breakout",
                "allocated_cash": 1_500_000.0,
            },
            seed=True,
        )
        with self._connection() as conn:
            conn.execute("UPDATE sleeves SET available_cash = ? WHERE id = ?", (1_654_593.71, "sleeve_value_5m"))
            conn.execute(
                """
                INSERT INTO positions (
                    account_id, sleeve_id, symbol, quantity, avg_cost, last_price, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account["id"],
                    "sleeve_value_5m",
                    "600519.SH",
                    200,
                    1725.8,
                    1725.8,
                    _now(),
                ),
            )

    def create_account(self, payload: dict[str, Any], *, seed: bool = False) -> dict[str, Any]:
        account_id = payload.get("id") or f"acct_{uuid.uuid4().hex[:10]}"
        now = _now()
        initial_cash = _float(payload.get("initial_cash"), 10_000_000.0)
        account = {
            "id": account_id,
            "name": payload.get("name") or "Paper Account",
            "initial_cash": initial_cash,
            "unallocated_cash": initial_cash,
            "currency": payload.get("currency") or "CNY",
            "market": payload.get("market") or "CN_A",
            "commission_rate": _float(payload.get("commission_rate"), 0.00025),
            "min_commission": _float(payload.get("min_commission"), 5.0),
            "stamp_duty_rate": _float(payload.get("stamp_duty_rate"), 0.0005),
            "slippage_model": payload.get("slippage_model") or "bps",
            "slippage_value": _float(payload.get("slippage_value"), 2.0),
            "auto_reverse_repo_enabled": 1 if payload.get("auto_reverse_repo_enabled", True) else 0,
            "reverse_repo_annual_rate": _float(payload.get("reverse_repo_annual_rate"), 0.018),
            "created_at": payload.get("created_at") or now,
        }
        if account["initial_cash"] <= 0:
            raise ValueError("initial_cash must be positive")
        if account["slippage_model"] not in {"bps", "fixed_tick"}:
            raise ValueError("slippage_model must be bps or fixed_tick")

        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO accounts (
                    id, name, initial_cash, unallocated_cash, currency, market,
                    commission_rate, min_commission, stamp_duty_rate, slippage_model,
                    slippage_value, auto_reverse_repo_enabled, reverse_repo_annual_rate,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account["id"],
                    account["name"],
                    account["initial_cash"],
                    account["unallocated_cash"],
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

    def create_sleeve(self, account_id: str, payload: dict[str, Any], *, seed: bool = False) -> dict[str, Any]:
        account = self.get_account(account_id)
        if not account:
            raise ValueError(f"unknown account_id: {account_id}")

        allocated_cash = _float(payload.get("allocated_cash"), 0.0)
        if allocated_cash <= 0:
            raise ValueError("allocated_cash must be positive")
        if allocated_cash > account["unallocated_cash"]:
            raise ValueError("allocated_cash exceeds account unallocated cash")

        sleeve_id = payload.get("id") or f"sleeve_{uuid.uuid4().hex[:10]}"
        now = _now()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO sleeves (
                    id, account_id, name, strategy_id, allocated_cash,
                    available_cash, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sleeve_id,
                    account_id,
                    payload.get("name") or payload.get("strategy_id") or "Strategy Sleeve",
                    payload.get("strategy_id") or f"strategy_{uuid.uuid4().hex[:8]}",
                    allocated_cash,
                    allocated_cash,
                    now,
                ),
            )
            conn.execute(
                "UPDATE accounts SET unallocated_cash = ROUND(unallocated_cash - ?, 2) WHERE id = ?",
                (allocated_cash, account_id),
            )

        if not seed:
            self.audit_store.record_event(
                AuditEvent(
                    timestamp=now,
                    ledger_type="cash",
                    event_type="sleeve_allocation",
                    account_id=account_id,
                    sleeve_id=sleeve_id,
                    strategy_id=payload.get("strategy_id"),
                    amount=allocated_cash,
                    before_state={"unallocated_cash": account["unallocated_cash"]},
                    after_state={"unallocated_cash": round(account["unallocated_cash"] - allocated_cash, 2)},
                    reason="capital allocated to strategy sleeve",
                    metadata={"sleeve_name": payload.get("name")},
                )
            )
        return self.get_sleeve(sleeve_id) or {}

    def set_sleeve_active(self, sleeve_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """启用/停用策略资金单元。停用后该 sleeve 的 BUY、策略运行、调度 tick 都会被拦。"""
        sleeve = self.get_sleeve(sleeve_id)
        if not sleeve:
            raise ValueError(f"unknown sleeve_id: {sleeve_id}")
        active = bool(payload.get("active", True))
        now = _now()
        with self._connection() as conn:
            conn.execute("UPDATE sleeves SET active = ? WHERE id = ?", (1 if active else 0, sleeve_id))
        self.audit_store.record_event(
            AuditEvent(
                timestamp=now,
                ledger_type="system",
                event_type="sleeve_status_changed",
                account_id=sleeve["account_id"],
                sleeve_id=sleeve_id,
                strategy_id=sleeve["strategy_id"],
                before_state={"active": bool(sleeve.get("active", True))},
                after_state={"active": active},
                reason="strategy sleeve enabled" if active else "strategy sleeve paused",
                metadata={"sleeve_name": sleeve["name"]},
            )
        )
        return self.get_sleeve(sleeve_id) or sleeve

    def adjust_sleeve_allocation(self, sleeve_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """调整 sleeve 资金占比：增量从账户未分配现金划入，减量退回(只能退还未占用的现金)。

        支持 percent(占账户初始资金的百分比, 0-100) 或 allocated_cash(目标金额) 二选一。
        """
        sleeve = self.get_sleeve(sleeve_id)
        if not sleeve:
            raise ValueError(f"unknown sleeve_id: {sleeve_id}")
        account = self.get_account(sleeve["account_id"])
        if not account:
            raise ValueError(f"unknown account_id: {sleeve['account_id']}")

        if payload.get("percent") not in (None, ""):
            percent = float(payload["percent"])
            if percent < 0 or percent > 100:
                raise ValueError("percent must be between 0 and 100")
            target = round(account["initial_cash"] * percent / 100, 2)
        else:
            target = round(_float(payload.get("allocated_cash"), sleeve["allocated_cash"]), 2)
            if target < 0:
                raise ValueError("allocated_cash cannot be negative")

        delta = round(target - float(sleeve["allocated_cash"]), 2)
        if abs(delta) < 0.01:
            return sleeve
        if delta > 0 and delta > account["unallocated_cash"] + 0.001:
            raise ValueError(
                f"账户未分配现金不足: 需要 {delta:.2f}, 仅剩 {account['unallocated_cash']:.2f}"
            )
        if delta < 0 and -delta > float(sleeve["available_cash"]) + 0.001:
            raise ValueError(
                f"sleeve 可退现金不足: 想退回 {-delta:.2f}, 可用现金只有 {sleeve['available_cash']:.2f}(其余已占用在持仓里)"
            )

        now = _now()
        with self._connection() as conn:
            conn.execute(
                "UPDATE sleeves SET allocated_cash = ROUND(allocated_cash + ?, 2), available_cash = ROUND(available_cash + ?, 2) WHERE id = ?",
                (delta, delta, sleeve_id),
            )
            conn.execute(
                "UPDATE accounts SET unallocated_cash = ROUND(unallocated_cash - ?, 2) WHERE id = ?",
                (delta, sleeve["account_id"]),
            )
        self.audit_store.record_event(
            AuditEvent(
                timestamp=now,
                ledger_type="cash",
                event_type="sleeve_allocation_adjusted",
                account_id=sleeve["account_id"],
                sleeve_id=sleeve_id,
                strategy_id=sleeve["strategy_id"],
                amount=delta,
                before_state={
                    "allocated_cash": sleeve["allocated_cash"],
                    "unallocated_cash": account["unallocated_cash"],
                },
                after_state={
                    "allocated_cash": round(sleeve["allocated_cash"] + delta, 2),
                    "unallocated_cash": round(account["unallocated_cash"] - delta, 2),
                },
                reason="sleeve allocation increased" if delta > 0 else "sleeve allocation reduced",
                metadata={"target_allocated_cash": target},
            )
        )
        return self.get_sleeve(sleeve_id) or sleeve

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY created_at ASC").fetchall()
        accounts = [_row(row) for row in rows]
        for account in accounts:
            account["sleeves"] = self.list_sleeves(account["id"])
        return accounts

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return _row(row) if row else None

    def list_sleeves(self, account_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM sleeves WHERE account_id = ? ORDER BY created_at ASC", (account_id,)).fetchall()
        sleeves = [_row(row) for row in rows]
        for sleeve in sleeves:
            sleeve["positions"] = self.list_positions(sleeve["id"])
        return sleeves

    def get_sleeve(self, sleeve_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM sleeves WHERE id = ?", (sleeve_id,)).fetchone()
        return _row(row) if row else None

    def list_positions(self, sleeve_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM positions WHERE sleeve_id = ? ORDER BY symbol ASC", (sleeve_id,)).fetchall()
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
        for field in ("account_id", "sleeve_id", "strategy_id", "symbol", "status"):
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
                sleeve_id=order["sleeve_id"],
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
        sleeve_id = _required(payload, "sleeve_id")
        symbol = _required(payload, "symbol").upper()
        side = _required(payload, "side").upper()
        quantity = int(_float(payload.get("quantity"), 0.0))
        signal_price = _float(payload.get("signal_price"), _float(payload.get("price"), 0.0))
        price_source = "client"
        if signal_price <= 0 and self.connectors:
            # 市价单语义：客户端不报价时，按 connector 最新 close 定价。
            signal_price = self._latest_close(
                payload.get("data_source") or "fixture",
                symbol,
                payload.get("frequency") or "5m",
            )
            price_source = f"{(payload.get('data_source') or 'fixture').lower()}_close"
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
        sleeve = self.get_sleeve(sleeve_id)
        if not account:
            raise ValueError(f"unknown account_id: {account_id}")
        if not sleeve or sleeve["account_id"] != account_id:
            raise ValueError(f"sleeve_id {sleeve_id} does not belong to account {account_id}")

        source_event_id = payload.get("source_event_id") or f"sig_{uuid.uuid4().hex[:12]}"
        self.audit_store.record_event(
            AuditEvent(
                event_id=source_event_id,
                timestamp=timestamp,
                ledger_type="decision",
                event_type="strategy_signal",
                account_id=account_id,
                sleeve_id=sleeve_id,
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
            sleeve_id=sleeve_id,
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

        # 停用的 sleeve 禁止开新仓(SELL 仍放行, 方便清仓退出)。
        if side == "BUY" and not sleeve.get("active", True):
            reason = f"sleeve {sleeve_id} is paused (strategy disabled)"
            self.audit_store.record_event(
                AuditEvent(
                    timestamp=timestamp,
                    ledger_type="decision",
                    event_type="sleeve_paused_blocked",
                    account_id=account_id,
                    sleeve_id=sleeve_id,
                    strategy_id=strategy_id,
                    run_id=run_id,
                    symbol=symbol,
                    quantity=quantity,
                    price=signal_price,
                    reason=reason,
                    source_event_id=source_event_id,
                    metadata={"side": side, "blocked_strategy": strategy_id},
                )
            )
            order_event_id = self._reject_order(
                order_id=order_id,
                account_id=account_id,
                sleeve_id=sleeve_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=signal_price,
                timestamp=timestamp,
                source_event_id=source_event_id,
                reason=reason,
            )
            return {"accepted": False, "reason": reason, "event_id": order_event_id, "source_event_id": source_event_id}

        opening_blocked = side == "BUY" and (not allow_open or position_policy in {"reduce_only", "close_all"})
        if opening_blocked:
            self.audit_store.record_event(
                AuditEvent(
                    timestamp=timestamp,
                    ledger_type="decision",
                    event_type="timing_blocked",
                    account_id=account_id,
                    sleeve_id=sleeve_id,
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
                sleeve_id=sleeve_id,
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
                sleeve_id=sleeve_id,
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
                sleeve=sleeve,
                positions=self.list_positions(sleeve_id),
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
                        sleeve_id=sleeve_id,
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
                    sleeve_id=sleeve_id,
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

        position = self._get_position(account_id, sleeve_id, symbol)
        available_cash = float(sleeve["available_cash"])
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
        slippage_cost = self._slippage_cost(account, fill_quantity, fill_price)
        total_cost = round(commission + stamp_duty + slippage_cost, 2)
        cash_delta = -gross_amount - total_cost if side == "BUY" else gross_amount - total_cost

        if side == "BUY" and available_cash + cash_delta < -0.001:
            order_event_id = self._reject_order(
                order_id=order_id,
                account_id=account_id,
                sleeve_id=sleeve_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=signal_price,
                timestamp=timestamp,
                source_event_id=source_event_id,
                reason="insufficient sleeve cash",
            )
            return {"accepted": False, "reason": "insufficient sleeve cash", "event_id": order_event_id, "source_event_id": source_event_id}
        if side == "SELL" and fill_quantity > position_before:
            order_event_id = self._reject_order(
                order_id=order_id,
                account_id=account_id,
                sleeve_id=sleeve_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=signal_price,
                timestamp=timestamp,
                source_event_id=source_event_id,
                reason="insufficient position quantity",
            )
            return {
                "accepted": False,
                "reason": "insufficient position quantity",
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
                sleeve_id=sleeve_id,
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
            sleeve_id=sleeve_id,
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
                sleeve_id=sleeve_id,
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
            conn.execute("UPDATE sleeves SET available_cash = ? WHERE id = ?", (available_cash_after, sleeve_id))
            if position_after == 0:
                conn.execute(
                    "DELETE FROM positions WHERE account_id = ? AND sleeve_id = ? AND symbol = ?",
                    (account_id, sleeve_id, symbol),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO positions (
                        account_id, sleeve_id, symbol, quantity, avg_cost, last_price, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, sleeve_id, symbol)
                    DO UPDATE SET
                        quantity = excluded.quantity,
                        avg_cost = excluded.avg_cost,
                        last_price = excluded.last_price,
                        updated_at = excluded.updated_at
                    """,
                    (account_id, sleeve_id, symbol, position_after, avg_cost_after, fill_price, timestamp),
                )

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

    def run_reverse_repo(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        account = self.get_account(account_id)
        if not account:
            raise ValueError(f"unknown account_id: {account_id}")
        if not account["auto_reverse_repo_enabled"]:
            raise ValueError("auto reverse repo is disabled for this account")
        amount = _float(payload.get("amount"), account["unallocated_cash"])
        annual_rate = _float(payload.get("annual_rate"), account["reverse_repo_annual_rate"])
        if amount <= 0:
            raise ValueError("amount must be positive")
        if amount > account["unallocated_cash"]:
            raise ValueError("reverse repo amount exceeds unallocated cash")
        timestamp = payload.get("timestamp") or _now()
        source_event_id = f"repo_{uuid.uuid4().hex[:12]}"
        self.audit_store.record_event(
            AuditEvent(
                event_id=source_event_id,
                timestamp=timestamp,
                ledger_type="system",
                event_type="cash_management_scan",
                account_id=account_id,
                amount=amount,
                before_state={"unallocated_cash": account["unallocated_cash"]},
                reason="auto reverse repo requested for idle cash",
                metadata={"annual_rate": annual_rate, "instrument": payload.get("instrument", "GC001_SIM")},
            )
        )
        ids = self.audit_store.record_reverse_repo(
            account_id=account_id,
            timestamp=timestamp,
            invest_cash=amount,
            annual_rate=annual_rate,
            cash_before=account["unallocated_cash"],
            source_event_id=source_event_id,
        )
        interest = round(amount * annual_rate / 365, 2)
        with self._connection() as conn:
            conn.execute(
                "UPDATE accounts SET unallocated_cash = ROUND(unallocated_cash + ?, 2) WHERE id = ?",
                (interest, account_id),
            )
        return {"source_event_id": source_event_id, "event_ids": ids, "interest": interest}

    def _portfolio_for_account(self, account: dict[str, Any], mark_prices: dict[str, dict[str, Any]]) -> dict[str, Any]:
        sleeves = self.list_sleeves(account["id"])
        sleeve_summaries: list[dict[str, Any]] = []
        positions: list[dict[str, Any]] = []
        sleeve_cash = 0.0
        allocated_cash = 0.0
        market_value = 0.0
        cost_basis = 0.0
        unrealized_pnl = 0.0

        for sleeve in sleeves:
            sleeve_positions = []
            sleeve_market_value = 0.0
            sleeve_cost_basis = 0.0
            sleeve_unrealized_pnl = 0.0
            for position in sleeve.get("positions", []):
                enriched = _enrich_position(position, sleeve, account["id"], mark_prices)
                sleeve_positions.append(enriched)
                positions.append(enriched)
                sleeve_market_value += enriched["market_value"]
                sleeve_cost_basis += enriched["cost_basis"]
                sleeve_unrealized_pnl += enriched["unrealized_pnl"]

            sleeve_available_cash = _money(sleeve["available_cash"])
            sleeve_allocated_cash = _money(sleeve["allocated_cash"])
            sleeve_equity = _money(sleeve_available_cash + sleeve_market_value)
            sleeve_pnl = _money(sleeve_equity - sleeve_allocated_cash)
            sleeve_cash += sleeve_available_cash
            allocated_cash += sleeve_allocated_cash
            market_value += sleeve_market_value
            cost_basis += sleeve_cost_basis
            unrealized_pnl += sleeve_unrealized_pnl
            sleeve_summaries.append(
                {
                    "id": sleeve["id"],
                    "account_id": sleeve["account_id"],
                    "name": sleeve["name"],
                    "strategy_id": sleeve["strategy_id"],
                    "active": bool(sleeve.get("active", True)),
                    "allocated_pct": _ratio(sleeve_allocated_cash, account["initial_cash"]),
                    "allocated_cash": sleeve_allocated_cash,
                    "available_cash": sleeve_available_cash,
                    "market_value": _money(sleeve_market_value),
                    "cost_basis": _money(sleeve_cost_basis),
                    "unrealized_pnl": _money(sleeve_unrealized_pnl),
                    "equity": sleeve_equity,
                    "pnl": sleeve_pnl,
                    "pnl_pct": _ratio(sleeve_pnl, sleeve_allocated_cash),
                    "exposure": _ratio(sleeve_market_value, sleeve_equity),
                    "positions": sleeve_positions,
                }
            )

        unallocated_cash = _money(account["unallocated_cash"])
        equity = _money(unallocated_cash + sleeve_cash + market_value)
        pnl = _money(equity - account["initial_cash"])
        return {
            "id": account["id"],
            "name": account["name"],
            "currency": account["currency"],
            "market": account["market"],
            "initial_cash": _money(account["initial_cash"]),
            "unallocated_cash": unallocated_cash,
            "allocated_cash": _money(allocated_cash),
            "sleeve_cash": _money(sleeve_cash),
            "total_cash": _money(unallocated_cash + sleeve_cash),
            "market_value": _money(market_value),
            "cost_basis": _money(cost_basis),
            "unrealized_pnl": _money(unrealized_pnl),
            "equity": equity,
            "pnl": pnl,
            "pnl_pct": _ratio(pnl, account["initial_cash"]),
            "exposure": _ratio(market_value, equity),
            "sleeves": sleeve_summaries,
            "positions": positions,
        }

    def _get_position(self, account_id: str, sleeve_id: str, symbol: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM positions
                WHERE account_id = ? AND sleeve_id = ? AND symbol = ?
                """,
                (account_id, sleeve_id, symbol),
            ).fetchone()
        return _row(row) if row else None

    def _reject_order(
        self,
        *,
        order_id: str,
        account_id: str,
        sleeve_id: str,
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

        return self.audit_store.record_event(
            AuditEvent(
                timestamp=timestamp,
                ledger_type="order",
                event_type="order_rejected",
                account_id=account_id,
                sleeve_id=sleeve_id,
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
        sleeve_id: str,
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
                    id, source_event_id, account_id, sleeve_id, strategy_id, run_id,
                    symbol, side, order_type, time_in_force, quantity, filled_quantity,
                    remaining_quantity, signal_price, limit_price, last_fill_price,
                    status, reason, created_at, updated_at, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    source_event_id,
                    account_id,
                    sleeve_id,
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
                sleeve_id=sleeve_id,
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

    @staticmethod
    def _slippage_cost(account: dict[str, Any], quantity: int, price: float) -> float:
        if account["slippage_model"] == "bps":
            return round(quantity * price * account["slippage_value"] / 10_000, 2)
        return round(quantity * account["slippage_value"], 2)


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
    sleeve: dict[str, Any],
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
    return {
        "account_id": account_id,
        "sleeve_id": sleeve["id"],
        "sleeve_name": sleeve["name"],
        "strategy_id": sleeve["strategy_id"],
        "symbol": position["symbol"],
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
        "updated_at": position["updated_at"],
    }


def _portfolio_totals(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    fields = [
        "initial_cash",
        "unallocated_cash",
        "allocated_cash",
        "sleeve_cash",
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
