from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any


AUDIT_FIELDS = [
    "id",
    "timestamp",
    "ledger_type",
    "account_id",
    "sleeve_id",
    "strategy_id",
    "run_id",
    "event_type",
    "symbol",
    "amount",
    "quantity",
    "price",
    "before_state",
    "after_state",
    "reason",
    "source_event_id",
    "metadata",
]


LEDGER_TYPES = {
    "events": None,
    "orders": "order",
    "trades": "trade",
    "cash": "cash",
    "positions": "position",
    "decisions": "decision",
    "system": "system",
    "portfolio": "portfolio_snapshot",
}


@dataclass(frozen=True)
class AuditEvent:
    ledger_type: str
    event_type: str
    account_id: str
    sleeve_id: str | None = None
    strategy_id: str | None = None
    run_id: str | None = None
    symbol: str | None = None
    amount: float | None = None
    quantity: float | None = None
    price: float | None = None
    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None
    reason: str | None = None
    source_event_id: str | None = None
    metadata: dict[str, Any] | None = None
    timestamp: str | None = None
    event_id: str | None = None


class AuditStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    ledger_type TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    sleeve_id TEXT,
                    strategy_id TEXT,
                    run_id TEXT,
                    event_type TEXT NOT NULL,
                    symbol TEXT,
                    amount REAL,
                    quantity REAL,
                    price REAL,
                    before_state TEXT NOT NULL DEFAULT '{}',
                    after_state TEXT NOT NULL DEFAULT '{}',
                    reason TEXT,
                    source_event_id TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_events_filters
                ON audit_events (
                    ledger_type,
                    account_id,
                    strategy_id,
                    symbol,
                    event_type,
                    timestamp
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_events_source
                ON audit_events (source_event_id)
                """
            )

    def clear(self) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM audit_events")

    def count(self) -> int:
        with self._connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM audit_events").fetchone()
            return int(row["count"])

    def record_event(self, event: AuditEvent) -> str:
        event_id = event.event_id or f"evt_{uuid.uuid4().hex[:16]}"
        timestamp = event.timestamp or datetime.now(timezone.utc).isoformat()
        before_state = json.dumps(event.before_state or {}, ensure_ascii=False, sort_keys=True)
        after_state = json.dumps(event.after_state or {}, ensure_ascii=False, sort_keys=True)
        metadata = json.dumps(event.metadata or {}, ensure_ascii=False, sort_keys=True)

        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (
                    id, timestamp, ledger_type, account_id, sleeve_id, strategy_id,
                    run_id, event_type, symbol, amount, quantity, price, before_state,
                    after_state, reason, source_event_id, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    timestamp,
                    event.ledger_type,
                    event.account_id,
                    event.sleeve_id,
                    event.strategy_id,
                    event.run_id,
                    event.event_type,
                    event.symbol,
                    event.amount,
                    event.quantity,
                    event.price,
                    before_state,
                    after_state,
                    event.reason,
                    event.source_event_id,
                    metadata,
                ),
            )
        return event_id

    def record_trade_settlement(
        self,
        *,
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
        cash_before: float,
        position_before: int,
        avg_cost_before: float,
        commission: float,
        stamp_duty: float,
        slippage_cost: float,
        avg_cost_after: float | None = None,
    ) -> dict[str, str]:
        # 交易结算必须拆分记录费用，复盘时才能解释 net PnL 的来源。
        gross_amount = round(quantity * price, 2)
        signed_amount = -gross_amount if side == "BUY" else gross_amount
        total_cost = round(commission + stamp_duty + slippage_cost, 2)
        cash_after = round(cash_before + signed_amount - total_cost, 2)
        position_after = position_before + quantity if side == "BUY" else position_before - quantity
        if avg_cost_after is None:
            avg_cost_after = price if position_after and side == "BUY" else avg_cost_before
        event_ids: dict[str, str] = {}

        event_ids["trade"] = self.record_event(
            AuditEvent(
                event_id=f"trade_{uuid.uuid4().hex[:12]}",
                timestamp=timestamp,
                ledger_type="trade",
                event_type="trade_filled",
                account_id=account_id,
                sleeve_id=sleeve_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol=symbol,
                amount=gross_amount,
                quantity=quantity,
                price=price,
                before_state={"position": position_before, "cash": cash_before},
                after_state={"position": position_after, "cash": cash_after},
                reason=f"{side} filled at next event price",
                source_event_id=source_event_id,
                metadata={"side": side, "gross_amount": gross_amount},
            )
        )

        cash_entries = [
            ("trade_principal", signed_amount, "principal cash movement"),
            ("commission", -commission, "commission charged"),
            ("stamp_duty", -stamp_duty, "stamp duty charged"),
            ("slippage", -slippage_cost, "slippage cost charged"),
        ]
        running_cash = cash_before
        for event_type, amount, reason in cash_entries:
            before = running_cash
            running_cash = round(running_cash + amount, 2)
            event_ids[event_type] = self.record_event(
                AuditEvent(
                    timestamp=timestamp,
                    ledger_type="cash",
                    event_type=event_type,
                    account_id=account_id,
                    sleeve_id=sleeve_id,
                    strategy_id=strategy_id,
                    run_id=run_id,
                    symbol=symbol,
                    amount=round(amount, 2),
                    before_state={"cash": before},
                    after_state={"cash": running_cash},
                    reason=reason,
                    source_event_id=source_event_id,
                    metadata={"trade_event_id": event_ids["trade"], "side": side},
                )
            )

        event_ids["position"] = self.record_event(
            AuditEvent(
                timestamp=timestamp,
                ledger_type="position",
                event_type="position_update",
                account_id=account_id,
                sleeve_id=sleeve_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol=symbol,
                quantity=position_after,
                price=price,
                before_state={"quantity": position_before, "avg_cost": avg_cost_before},
                after_state={"quantity": position_after, "avg_cost": avg_cost_after},
                reason="position updated after fill",
                source_event_id=source_event_id,
                metadata={"trade_event_id": event_ids["trade"], "side": side},
            )
        )

        market_value = round(position_after * price, 2)
        event_ids["portfolio"] = self.record_event(
            AuditEvent(
                timestamp=timestamp,
                ledger_type="portfolio_snapshot",
                event_type="portfolio_snapshot",
                account_id=account_id,
                sleeve_id=sleeve_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol=symbol,
                amount=round(cash_after + market_value, 2),
                before_state={"cash": cash_before, "market_value": position_before * price},
                after_state={"cash": cash_after, "market_value": market_value},
                reason="portfolio marked after trade settlement",
                source_event_id=source_event_id,
                metadata={"trade_event_id": event_ids["trade"]},
            )
        )
        return event_ids

    def record_reverse_repo(
        self,
        *,
        account_id: str,
        timestamp: str,
        invest_cash: float,
        annual_rate: float,
        cash_before: float,
        source_event_id: str,
    ) -> dict[str, str]:
        interest = round(invest_cash * annual_rate / 365, 2)
        ids: dict[str, str] = {}
        ids["invest"] = self.record_event(
            AuditEvent(
                timestamp=timestamp,
                ledger_type="cash",
                event_type="reverse_repo_invest",
                account_id=account_id,
                amount=-invest_cash,
                before_state={"idle_cash": cash_before},
                after_state={"idle_cash": round(cash_before - invest_cash, 2)},
                reason="auto reverse repo invested idle cash",
                source_event_id=source_event_id,
                metadata={"annual_rate": annual_rate},
            )
        )
        ids["principal_return"] = self.record_event(
            AuditEvent(
                timestamp=timestamp,
                ledger_type="cash",
                event_type="reverse_repo_principal_return",
                account_id=account_id,
                amount=invest_cash,
                before_state={"idle_cash": round(cash_before - invest_cash, 2)},
                after_state={"idle_cash": cash_before},
                reason="reverse repo principal returned",
                source_event_id=source_event_id,
                metadata={"annual_rate": annual_rate},
            )
        )
        ids["interest"] = self.record_event(
            AuditEvent(
                timestamp=timestamp,
                ledger_type="cash",
                event_type="reverse_repo_interest",
                account_id=account_id,
                amount=interest,
                before_state={"cash": cash_before},
                after_state={"cash": round(cash_before + interest, 2)},
                reason="reverse repo interest settled",
                source_event_id=source_event_id,
                metadata={"annual_rate": annual_rate, "invest_cash": invest_cash},
            )
        )
        return ids

    def list_events(self, filters: dict[str, str | None] | None = None) -> list[dict[str, Any]]:
        filters = filters or {}
        clauses: list[str] = []
        params: list[Any] = []

        for field in ("ledger_type", "account_id", "sleeve_id", "strategy_id", "run_id", "symbol", "event_type"):
            value = filters.get(field)
            if value:
                clauses.append(f"{field} = ?")
                params.append(value)

        start = filters.get("start")
        end = filters.get("end")
        if start:
            clauses.append("timestamp >= ?")
            params.append(start)
        if end:
            clauses.append("timestamp <= ?")
            params.append(end)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = int(filters.get("limit") or 500)
        query = f"SELECT rowid, * FROM audit_events {where} ORDER BY timestamp DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._decode_row(row) for row in rows]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM audit_events WHERE id = ?", (event_id,)).fetchone()
        return self._decode_row(row) if row else None

    def get_chain(self, event_id: str) -> dict[str, Any]:
        event = self.get_event(event_id)
        if not event:
            return {
                "signal": None,
                "timing_decision": None,
                "risk_decision": None,
                "order": None,
                "order_events": [],
                "trade": None,
                "cash_changes": [],
                "position_changes": [],
                "portfolio_snapshot": None,
                "all_events": [],
            }

        root_id = event["source_event_id"] or event["id"]
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT rowid, * FROM audit_events
                WHERE id = ? OR source_event_id = ?
                ORDER BY timestamp ASC, rowid ASC
                """,
                (root_id, root_id),
            ).fetchall()
        events = [self._decode_row(row) for row in rows]

        def first(ledger_type: str, *event_types: str) -> dict[str, Any] | None:
            if event_types:
                for event_type in event_types:
                    for item in events:
                        if item["ledger_type"] == ledger_type and item["event_type"] == event_type:
                            return item
                return None
            for item in events:
                if item["ledger_type"] != ledger_type:
                    continue
                return item
            return None

        return {
            "signal": first("decision", "strategy_signal"),
            "timing_decision": first("decision", "timing_decision", "timing_blocked"),
            "risk_decision": first("decision", "risk_blocked"),
            "order": first(
                "order",
                "order_submitted",
                "order_rejected",
                "order_cancelled",
                "order_partially_filled",
                "order_filled",
                "order_created",
            ),
            "order_events": [item for item in events if item["ledger_type"] == "order"],
            "trade": first("trade"),
            "cash_changes": [item for item in events if item["ledger_type"] == "cash"],
            "position_changes": [item for item in events if item["ledger_type"] == "position"],
            "portfolio_snapshot": first("portfolio_snapshot"),
            "all_events": events,
        }

    def export_events(self, filters: dict[str, str | None], export_format: str) -> tuple[str, str]:
        events = self.list_events(filters)
        if export_format == "json":
            return "application/json; charset=utf-8", json.dumps(events, ensure_ascii=False, indent=2)
        if export_format != "csv":
            raise ValueError("format must be csv or json")

        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=AUDIT_FIELDS)
        writer.writeheader()
        for event in events:
            row = {field: event.get(field) for field in AUDIT_FIELDS}
            row["before_state"] = json.dumps(row["before_state"], ensure_ascii=False, sort_keys=True)
            row["after_state"] = json.dumps(row["after_state"], ensure_ascii=False, sort_keys=True)
            row["metadata"] = json.dumps(row["metadata"], ensure_ascii=False, sort_keys=True)
            writer.writerow(row)
        return "text/csv; charset=utf-8", output.getvalue()

    def seed_demo(self) -> None:
        if self.count() > 0:
            return

        account_id = "acct_a_share_alpha"
        sleeve_id = "sleeve_value_5m"
        strategy_id = "strategy_value_rotation"
        run_id = "run_20260610_0930"
        source_signal = "sig_600519_buy_0930"
        blocked_signal = "sig_000858_blocked_0945"
        reverse_repo_source = "cash_mgmt_20260610_close"

        self.record_event(
            AuditEvent(
                event_id=source_signal,
                timestamp="2026-06-10T09:30:00+08:00",
                ledger_type="decision",
                event_type="strategy_signal",
                account_id=account_id,
                sleeve_id=sleeve_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol="600519.SH",
                quantity=200,
                price=1725.0,
                reason="value_rotation score crossed buy threshold",
                metadata={"score": 0.91, "frequency": "5m"},
            )
        )
        self.record_event(
            AuditEvent(
                timestamp="2026-06-10T09:30:01+08:00",
                ledger_type="decision",
                event_type="timing_decision",
                account_id=account_id,
                sleeve_id=sleeve_id,
                strategy_id="timing_market_regime",
                run_id=run_id,
                symbol="600519.SH",
                reason="market regime risk-on, allow opening",
                source_event_id=source_signal,
                metadata={"allow_open": True, "position_policy": "hold"},
            )
        )
        order_id = self.record_event(
            AuditEvent(
                event_id="ord_600519_093001",
                timestamp="2026-06-10T09:30:01+08:00",
                ledger_type="order",
                event_type="order_submitted",
                account_id=account_id,
                sleeve_id=sleeve_id,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol="600519.SH",
                amount=345000.0,
                quantity=200,
                price=1725.0,
                reason="buy order accepted after timing gate",
                source_event_id=source_signal,
                metadata={"side": "BUY", "order_type": "market", "timing_gate": "risk_on"},
            )
        )
        self.record_trade_settlement(
            account_id=account_id,
            sleeve_id=sleeve_id,
            strategy_id=strategy_id,
            run_id=run_id,
            symbol="600519.SH",
            side="BUY",
            quantity=200,
            price=1725.8,
            timestamp="2026-06-10T09:35:00+08:00",
            source_event_id=source_signal,
            cash_before=2_000_000.0,
            position_before=0,
            avg_cost_before=0.0,
            commission=86.29,
            stamp_duty=0.0,
            slippage_cost=160.0,
        )
        self.record_event(
            AuditEvent(
                timestamp="2026-06-10T09:35:01+08:00",
                ledger_type="system",
                event_type="connector_health",
                account_id=account_id,
                reason="TongDaXin 5m bars updated",
                metadata={"connector": "TongDaXin", "latency_ms": 420, "status": "ok"},
            )
        )

        self.record_event(
            AuditEvent(
                event_id=blocked_signal,
                timestamp="2026-06-10T09:45:00+08:00",
                ledger_type="decision",
                event_type="strategy_signal",
                account_id=account_id,
                sleeve_id="sleeve_growth_5m",
                strategy_id="strategy_growth_breakout",
                run_id="run_20260610_0945",
                symbol="000858.SZ",
                quantity=1000,
                price=126.3,
                reason="growth_breakout generated buy signal",
                metadata={"score": 0.84, "frequency": "5m"},
            )
        )
        self.record_event(
            AuditEvent(
                timestamp="2026-06-10T09:45:01+08:00",
                ledger_type="decision",
                event_type="timing_blocked",
                account_id=account_id,
                sleeve_id="sleeve_growth_5m",
                strategy_id="timing_market_regime",
                run_id="run_20260610_0945",
                symbol="000858.SZ",
                quantity=1000,
                price=126.3,
                reason="timing strategy risk-off blocked new opening",
                source_event_id=blocked_signal,
                metadata={"allow_open": False, "position_policy": "reduce_only", "blocked_strategy": "strategy_growth_breakout"},
            )
        )
        self.record_event(
            AuditEvent(
                timestamp="2026-06-10T09:45:01+08:00",
                ledger_type="order",
                event_type="order_rejected",
                account_id=account_id,
                sleeve_id="sleeve_growth_5m",
                strategy_id="strategy_growth_breakout",
                run_id="run_20260610_0945",
                symbol="000858.SZ",
                amount=126300.0,
                quantity=1000,
                price=126.3,
                reason="blocked by timing_market_regime",
                source_event_id=blocked_signal,
                metadata={"side": "BUY", "order_type": "market", "blocked_by": "timing_market_regime"},
            )
        )

        self.record_event(
            AuditEvent(
                event_id=reverse_repo_source,
                timestamp="2026-06-10T15:00:00+08:00",
                ledger_type="system",
                event_type="cash_management_scan",
                account_id=account_id,
                amount=1_200_000.0,
                reason="auto reverse repo enabled for idle cash",
                metadata={"annual_rate": 0.018, "instrument": "GC001_SIM"},
            )
        )
        self.record_reverse_repo(
            account_id=account_id,
            timestamp="2026-06-10T15:00:01+08:00",
            invest_cash=1_200_000.0,
            annual_rate=0.018,
            cash_before=1_654_593.71,
            source_event_id=reverse_repo_source,
        )
        self.record_event(
            AuditEvent(
                timestamp="2026-06-10T15:01:00+08:00",
                ledger_type="portfolio_snapshot",
                event_type="portfolio_snapshot",
                account_id=account_id,
                amount=10_046_812.44,
                before_state={"equity": 10_032_661.81},
                after_state={"equity": 10_046_812.44, "cash": 1_658_653.71},
                reason="end-of-day account snapshot after reverse repo",
                source_event_id=reverse_repo_source,
                metadata={"gross_pnl": 48212.44, "net_pnl": 46812.44},
            )
        )

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item.pop("rowid", None)
        item["before_state"] = json.loads(item["before_state"] or "{}")
        item["after_state"] = json.loads(item["after_state"] or "{}")
        item["metadata"] = json.loads(item["metadata"] or "{}")
        return item
