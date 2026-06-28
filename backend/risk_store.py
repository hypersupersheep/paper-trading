from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.audit_store import AuditEvent, AuditStore


CN_TZ = ZoneInfo("Asia/Shanghai")

# 限额字段统一在这里声明：API、merge、前端字段保持一致，新增规则只需扩展此表。
RISK_LIMIT_FIELDS = [
    "max_order_notional",
    "max_exposure",
    "max_symbol_position",
    "min_cash_buffer",
    "max_orders_per_tick",
    "max_orders_per_day",
]

INTEGER_LIMIT_FIELDS = {"max_symbol_position", "max_orders_per_tick", "max_orders_per_day"}


class RiskStore:
    """Pre-trade risk gate(交易前风控门控) 的配置与检查层。

    配置为 account 级(单账户单一现金池)。检查在 paper broker 接单前执行，
    命中即拒单，不做部分放行。
    """

    def __init__(self, db_path: str | Path, audit_store: AuditStore, trading_store: Any | None = None):
        self.db_path = Path(db_path)
        self.audit_store = audit_store
        # trading_store 仅用于校验 scope id 是否存在；测试可不传。
        self.trading_store = trading_store
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
                CREATE TABLE IF NOT EXISTS risk_configs (
                    id TEXT PRIMARY KEY,
                    scope_type TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    max_order_notional REAL,
                    max_exposure REAL,
                    max_symbol_position INTEGER,
                    min_cash_buffer REAL,
                    max_orders_per_tick INTEGER,
                    max_orders_per_day INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(scope_type, scope_id)
                )
                """
            )
            # 老库迁移:max_sleeve_exposure -> max_exposure;并清掉 sleeve 级配置(已无 sleeve)。
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(risk_configs)").fetchall()}
            if "max_sleeve_exposure" in cols and "max_exposure" not in cols:
                conn.execute("ALTER TABLE risk_configs RENAME COLUMN max_sleeve_exposure TO max_exposure")
            conn.execute("DELETE FROM risk_configs WHERE scope_type = 'sleeve'")

    def upsert_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = _required(payload, "account_id")
        if self.trading_store:
            if not self.trading_store.get_account(account_id):
                raise ValueError(f"unknown account_id: {account_id}")

        scope_type = "account"
        scope_id = account_id
        limits: dict[str, Any] = {}
        for field in RISK_LIMIT_FIELDS:
            limits[field] = _limit_value(field, payload.get(field))
        if all(value is None for value in limits.values()):
            raise ValueError("at least one risk limit must be set")

        enabled = 1 if payload.get("enabled", True) else 0
        now = _now()
        config_id = f"risk_{uuid.uuid4().hex[:10]}"
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO risk_configs (
                    id, scope_type, scope_id, account_id, enabled,
                    max_order_notional, max_exposure, max_symbol_position,
                    min_cash_buffer, max_orders_per_tick, max_orders_per_day,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_type, scope_id)
                DO UPDATE SET
                    enabled = excluded.enabled,
                    max_order_notional = excluded.max_order_notional,
                    max_exposure = excluded.max_exposure,
                    max_symbol_position = excluded.max_symbol_position,
                    min_cash_buffer = excluded.min_cash_buffer,
                    max_orders_per_tick = excluded.max_orders_per_tick,
                    max_orders_per_day = excluded.max_orders_per_day,
                    updated_at = excluded.updated_at
                """,
                (
                    config_id,
                    scope_type,
                    scope_id,
                    account_id,
                    enabled,
                    limits["max_order_notional"],
                    limits["max_exposure"],
                    limits["max_symbol_position"],
                    limits["min_cash_buffer"],
                    limits["max_orders_per_tick"],
                    limits["max_orders_per_day"],
                    now,
                    now,
                ),
            )
        config = self.get_config(scope_type, scope_id) or {}
        self.audit_store.record_event(
            AuditEvent(
                timestamp=now,
                ledger_type="system",
                event_type="risk_config_updated",
                account_id=account_id,
                reason=f"risk config updated for {scope_type} {scope_id}",
                metadata={"config_id": config.get("id"), "enabled": bool(enabled), **limits},
            )
        )
        return config

    def list_configs(self, account_id: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if account_id:
            clauses.append("account_id = ?")
            params.append(account_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM risk_configs {where} ORDER BY created_at ASC",
                params,
            ).fetchall()
        return [_decode_config(row) for row in rows]

    def get_config(self, scope_type: str, scope_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM risk_configs WHERE scope_type = ? AND scope_id = ?",
                (scope_type, scope_id),
            ).fetchone()
        return _decode_config(row) if row else None

    def resolve_limits(self, account_id: str) -> dict[str, Any] | None:
        """取该账户启用的风控限额。"""
        account_config = self.get_config("account", account_id)
        if not account_config or not account_config["enabled"]:
            return None
        limits: dict[str, Any] = {field: None for field in RISK_LIMIT_FIELDS}
        sources: dict[str, str] = {}
        for field in RISK_LIMIT_FIELDS:
            if account_config.get(field) is not None:
                limits[field] = account_config[field]
                sources[field] = f"account:{account_id}"
        if all(value is None for value in limits.values()):
            return None
        limits["sources"] = sources
        return limits

    def evaluate_order(
        self,
        *,
        account: dict[str, Any],
        positions: list[dict[str, Any]],
        symbol: str,
        side: str,
        quantity: int,
        signal_price: float,
        fill_price: float,
        run_id: str,
        timestamp: str,
    ) -> dict[str, Any] | None:
        """命中任一规则即返回 breach 字典；全部通过返回 None。

        BUY 检查全部规则；SELL 是减仓方向，只检查单笔金额和订单频次。
        """
        limits = self.resolve_limits(account["id"])
        if not limits:
            return None

        def breach(rule: str, observed: float, limit: float, detail: str) -> dict[str, Any]:
            return {
                "rule": rule,
                "observed": round(float(observed), 4),
                "limit": limit,
                "scope": limits["sources"].get(rule),
                "reason": f"risk_blocked: {rule} {detail}",
            }

        notional = round(quantity * signal_price, 2)
        limit = limits["max_order_notional"]
        if limit is not None and notional > limit:
            return breach(
                "max_order_notional",
                notional,
                limit,
                f"order notional {notional:.2f} exceeds limit {limit:.2f}",
            )

        position_before = 0
        existing_market_value = 0.0
        for position in positions:
            existing_market_value += float(position["quantity"]) * float(position["last_price"])
            if position["symbol"] == symbol:
                position_before = int(position["quantity"])

        if side == "BUY":
            limit = limits["max_symbol_position"]
            position_after = position_before + quantity
            if limit is not None and position_after > limit:
                return breach(
                    "max_symbol_position",
                    position_after,
                    limit,
                    f"position {position_after} shares of {symbol} exceeds limit {int(limit)}",
                )

            limit = limits["max_exposure"]
            if limit is not None:
                available_cash = float(account["cash"])
                equity = available_cash + existing_market_value
                projected_market_value = existing_market_value + quantity * fill_price
                # 买入是现金换持仓，equity 近似不变，所以用买前 equity 做分母。
                exposure_after = projected_market_value / equity if equity > 0 else float("inf")
                if exposure_after > limit:
                    return breach(
                        "max_exposure",
                        exposure_after,
                        limit,
                        f"exposure {exposure_after:.4f} exceeds limit {limit:.4f}",
                    )

            limit = limits["min_cash_buffer"]
            if limit is not None:
                cash_after = float(account["cash"]) - self._estimated_buy_cost(account, quantity, fill_price)
                if cash_after < limit:
                    return breach(
                        "min_cash_buffer",
                        cash_after,
                        limit,
                        f"account cash after order {cash_after:.2f} below buffer {limit:.2f}",
                    )

        limit = limits["max_orders_per_tick"]
        if limit is not None:
            # 当前订单已写入 paper_orders(status=created)，计数包含自身。
            count = self._order_count(account["id"], run_id=run_id)
            if count > limit:
                return breach(
                    "max_orders_per_tick",
                    count,
                    limit,
                    f"order count {count} in run {run_id} exceeds limit {int(limit)}",
                )

        limit = limits["max_orders_per_day"]
        if limit is not None:
            trade_date = _cn_date(timestamp)
            count = self._order_count(account["id"], trade_date=trade_date)
            if count > limit:
                return breach(
                    "max_orders_per_day",
                    count,
                    limit,
                    f"order count {count} on {trade_date} exceeds limit {int(limit)}",
                )
        return None

    def _order_count(self, account_id: str, *, run_id: str | None = None, trade_date: str | None = None) -> int:
        clauses = ["account_id = ?", "status != 'rejected'"]
        params: list[Any] = [account_id]
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT created_at FROM paper_orders WHERE {' AND '.join(clauses)}",
                params,
            ).fetchall()
        if trade_date is None:
            return len(rows)
        # 按 A 股交易日(Asia/Shanghai 日期) 统计，时间戳时区可能混杂，逐条转换。
        return sum(1 for row in rows if _cn_date(str(row["created_at"])) == trade_date)

    @staticmethod
    def _estimated_buy_cost(account: dict[str, Any], quantity: int, price: float) -> float:
        gross = round(quantity * price, 2)
        commission = max(round(gross * account["commission_rate"], 2), account["min_commission"])
        if account["slippage_model"] == "bps":
            slippage = round(gross * account["slippage_value"] / 10_000, 2)
        else:
            slippage = round(quantity * account["slippage_value"], 2)
        return round(gross + commission + slippage, 2)


def _decode_config(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["enabled"] = bool(item["enabled"])
    return item


def _limit_value(field: str, value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    number = float(value)
    if number <= 0:
        raise ValueError(f"{field} must be positive")
    if field in INTEGER_LIMIT_FIELDS:
        return int(number)
    return number


def _cn_date(timestamp: str) -> str:
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(CN_TZ).date().isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or value == "":
        raise ValueError(f"{key} is required")
    return str(value)
