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
                    id, timestamp, ledger_type, account_id, strategy_id,
                    run_id, event_type, symbol, amount, quantity, price, before_state,
                    after_state, reason, source_event_id, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    timestamp,
                    event.ledger_type,
                    event.account_id,
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

    def net_position_asof(self, account_id: str, symbol: str, as_of_ts: str) -> int:
        """按成交事件时序重建该 (账户, 标的) 截至 as_of_ts(含)的净持仓股数。

        用于补录卖出的时序校验:不能卖出"该交易日当时还没买入"的量。
        """
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT quantity, json_extract(metadata, '$.side') AS side
                FROM audit_events
                WHERE ledger_type = 'trade' AND event_type = 'trade_filled'
                  AND account_id = ? AND symbol = ? AND timestamp <= ?
                """,
                (account_id, symbol, as_of_ts),
            ).fetchall()
        qty = 0
        for row in rows:
            q = int(row["quantity"] or 0)
            qty += q if str(row["side"]).upper() == "BUY" else -q
        return qty

    def list_events(self, filters: dict[str, str | None] | None = None) -> list[dict[str, Any]]:
        filters = filters or {}
        clauses: list[str] = []
        params: list[Any] = []

        for field in ("ledger_type", "account_id", "strategy_id", "run_id", "symbol", "event_type"):
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

    def voided_trade_event_ids(self, account_id: str | None = None) -> set[str]:
        """已作废成交的 trade_event_id 集合(从 append-only 的 trade_voided 事件派生)。

        作废不物理删除任何审计行,而是追加一条 trade_voided 记录;净值重建/流水/盈亏一律
        据此把被作废的成交排除在外,使曲线"如同该笔从未发生"。
        """
        filters: dict[str, str | None] = {"event_type": "trade_voided", "limit": "100000"}
        if account_id:
            filters["account_id"] = account_id
        voided: set[str] = set()
        for event in self.list_events(filters):
            tid = (event.get("metadata") or {}).get("trade_event_id")
            if tid:
                voided.add(str(tid))
        return voided

    def trade_summaries(self, filters: dict[str, str | None] | None = None) -> list[dict[str, Any]]:
        """把"一笔交易/一只股票"引起的整条审计流水折叠成一行,并补上股票名称。

        每个 trade_filled 折叠它同一条链(source_event_id)上的本金/佣金/印花税/滑点/持仓子事件,
        汇总成:方向、数量、成交价、成交额、费用合计、净现金影响、成交后持仓、卖出已实现盈亏。
        没有成交的链(被风控/择时拦截、订单拒绝/撤销)各保留一行头条,信息不丢。
        """
        from backend import names

        filters = dict(filters or {})
        action_limit = int(filters.get("limit") or 300)
        # 放宽底层拉取量,确保同一笔的子事件能凑齐再折叠。
        raw = self.list_events({**filters, "event_type": None, "limit": max(action_limit * 12, 4000)})
        voided = self.voided_trade_event_ids(filters.get("account_id"))

        by_root: dict[str, list[dict[str, Any]]] = {}
        for event in raw:
            by_root.setdefault(event.get("source_event_id") or event["id"], []).append(event)

        fee_types = {"commission", "stamp_duty", "slippage"}
        rows: list[dict[str, Any]] = []
        for root, group in by_root.items():
            group.sort(key=lambda e: (e["timestamp"], e["id"]))
            trade = next(
                (e for e in group if e["ledger_type"] == "trade" and e["event_type"] == "trade_filled"),
                None,
            )
            if trade is not None:
                side = str((trade.get("metadata") or {}).get("side") or "").upper()
                qty = int(trade.get("quantity") or 0)
                price = float(trade.get("price") or 0.0)
                gross = float(trade.get("amount") or 0.0)
                fees = round(sum(-float(e.get("amount") or 0.0) for e in group if e["event_type"] in fee_types), 2)
                position = next((e for e in group if e["ledger_type"] == "position"), None)
                avg_before = float(((position or {}).get("before_state") or {}).get("avg_cost") or 0.0)
                pos_after = int((trade.get("after_state") or {}).get("position") or 0)
                net_cash = round((-gross if side == "BUY" else gross) - fees, 2)
                realized = None
                if side == "SELL" and avg_before > 0:
                    realized = round(qty * (price - avg_before) - fees, 2)
                backfill = bool(
                    (trade.get("metadata") or {}).get("backfill")
                    or any(e["event_type"] == "trade_backfill_declared" for e in group)
                )
                is_voided = trade["id"] in voided
                rows.append(
                    {
                        "kind": "trade",
                        "id": trade["id"],
                        "root_id": root,
                        "timestamp": trade["timestamp"],
                        "account_id": trade.get("account_id"),
                        "strategy_id": trade.get("strategy_id"),
                        "symbol": trade.get("symbol"),
                        "name": names.resolve(trade.get("symbol") or ""),
                        "side": side,
                        "quantity": qty,
                        "price": price,
                        "gross_amount": gross,
                        "fees": fees,
                        "net_cash": net_cash,
                        "position_after": pos_after,
                        "avg_cost_before": round(avg_before, 4) if avg_before else None,
                        "realized_pnl": None if is_voided else realized,
                        "backfill": backfill,
                        "voided": is_voided,
                        "reason": trade.get("reason"),
                    }
                )
                continue

            headline = next(
                (
                    e
                    for e in group
                    if e["event_type"] in ("order_rejected", "risk_blocked", "timing_blocked", "order_cancelled")
                ),
                None,
            )
            if headline is not None:
                symbol = headline.get("symbol")
                rows.append(
                    {
                        "kind": headline["event_type"],
                        "id": headline["id"],
                        "root_id": root,
                        "timestamp": headline["timestamp"],
                        "account_id": headline.get("account_id"),
                        "strategy_id": headline.get("strategy_id"),
                        "symbol": symbol,
                        "name": names.resolve(symbol) if symbol else "",
                        "side": str((headline.get("metadata") or {}).get("side") or "").upper(),
                        "quantity": int(headline.get("quantity") or 0) or None,
                        "price": headline.get("price"),
                        "gross_amount": None,
                        "fees": None,
                        "net_cash": None,
                        "realized_pnl": None,
                        "backfill": False,
                        "reason": headline.get("reason"),
                    }
                )

        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        return rows[:action_limit]

    def realized_pnl_by_symbol(
        self,
        filters: dict[str, str | None] | None = None,
        *,
        exclude_symbols: set[str] | None = None,
    ) -> dict[str, Any]:
        """按个股汇总历史买卖的已实现盈亏(平均成本法,卖出时结转)。供日志页"历史个股盈亏"看台。

        exclude_symbols:当前仍持仓的标的——它们的盈亏归"现有持仓",不计入历史盈亏。
        已作废(voided)的成交不参与汇总。
        """
        rows = self.trade_summaries({**(filters or {}), "limit": 100000})
        skip = {str(s).upper() for s in (exclude_symbols or set())}
        agg: dict[str, dict[str, Any]] = {}
        for r in rows:
            if r["kind"] != "trade" or not r.get("symbol") or r.get("voided"):
                continue
            sym = r["symbol"]
            if str(sym).upper() in skip:
                continue
            bucket = agg.setdefault(
                sym,
                {
                    "symbol": sym,
                    "name": r["name"],
                    "realized_pnl": 0.0,
                    "buy_quantity": 0,
                    "sell_quantity": 0,
                    "fees": 0.0,
                    "trades": 0,
                    "last_timestamp": None,
                },
            )
            bucket["trades"] += 1
            bucket["fees"] = round(bucket["fees"] + float(r.get("fees") or 0.0), 2)
            if r["side"] == "BUY":
                bucket["buy_quantity"] += int(r.get("quantity") or 0)
            else:
                bucket["sell_quantity"] += int(r.get("quantity") or 0)
            if r.get("realized_pnl") is not None:
                bucket["realized_pnl"] = round(bucket["realized_pnl"] + float(r["realized_pnl"]), 2)
            if not bucket["last_timestamp"] or r["timestamp"] > bucket["last_timestamp"]:
                bucket["last_timestamp"] = r["timestamp"]

        out = list(agg.values())
        out.sort(key=lambda b: b["realized_pnl"], reverse=True)
        total = round(sum(b["realized_pnl"] for b in out), 2)
        return {"symbols": out, "total_realized_pnl": total}

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
