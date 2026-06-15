from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.audit_store import AuditEvent, AuditStore
from backend.data_connectors import DataConnectorRegistry, normalize_frequency
from backend.strategy_adapter import adapt_strategy_code
from backend.timing_store import TimingStore
from backend.trading_store import TradingStore
from backend.workers import worker_command as _worker_command


class StrategyStore:
    def __init__(
        self,
        db_path: str | Path,
        audit_store: AuditStore,
        trading_store: TradingStore,
        strategy_dir: str | Path | None = None,
        timing_store: TimingStore | None = None,
    ):
        self.db_path = Path(db_path)
        self.audit_store = audit_store
        self.trading_store = trading_store
        self.strategy_dir = Path(strategy_dir) if strategy_dir else self.db_path.parent / "strategies"
        self.root_dir = Path(__file__).resolve().parents[1]
        self.strategy_dir.mkdir(parents=True, exist_ok=True)
        self.connectors = DataConnectorRegistry()
        self.timing_store = timing_store
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
                CREATE TABLE IF NOT EXISTS strategies (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_runs (
                    id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    sleeve_id TEXT NOT NULL,
                    frequency TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    bars_processed INTEGER NOT NULL,
                    orders_submitted INTEGER NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    FOREIGN KEY(strategy_id) REFERENCES strategies(id)
                )
                """
            )

    def seed_demo(self) -> None:
        if self.list_strategies():
            return
        self.create_strategy(
            {
                "id": "strategy_demo_momentum",
                "name": "Demo Momentum 5m",
                "code": SAMPLE_STRATEGY_CODE,
            },
            seed=True,
        )

    def create_strategy(self, payload: dict[str, Any], *, seed: bool = False) -> dict[str, Any]:
        name = payload.get("name") or "Python Strategy"
        # 自动适配：on_bar 之外的常见写法(handle_bar/class/信号函数) 由平台接驱动。
        adapted = adapt_strategy_code(payload.get("code"), kind="策略", flavor="stock")
        strategy_id = payload.get("id") or f"strategy_{uuid.uuid4().hex[:10]}"
        file_path = self.strategy_dir / f"{strategy_id}.py"
        file_path.write_text(adapted["code"], encoding="utf-8")
        now = _now()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO strategies (id, name, file_path, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (strategy_id, name, str(file_path), now),
            )
        if not seed:
            self.audit_store.record_event(
                AuditEvent(
                    timestamp=now,
                    ledger_type="system",
                    event_type="strategy_imported",
                    account_id="workspace",
                    strategy_id=strategy_id,
                    reason="python strategy imported",
                    metadata={
                        "name": name,
                        "file_path": str(file_path),
                        "source_filename": payload.get("source_filename"),
                        "adapter_mode": adapted["mode"],
                        "adapter_entry": adapted["entry"],
                    },
                )
            )
        strategy = self.get_strategy(strategy_id) or {"id": strategy_id, "name": name, "file_path": str(file_path), "created_at": now}
        strategy["adapter"] = {"mode": adapted["mode"], "entry": adapted["entry"]}
        return strategy

    def delete_strategy(self, strategy_id: str) -> dict[str, Any]:
        strategy = self.get_strategy(strategy_id)
        if not strategy:
            raise ValueError(f"unknown strategy_id: {strategy_id}")
        with self._connection() as conn:
            # 先清子表(strategy_runs 有外键),否则外键约束会拦住删除。
            conn.execute("DELETE FROM strategy_runs WHERE strategy_id = ?", (strategy_id,))
            conn.execute("DELETE FROM strategies WHERE id = ?", (strategy_id,))
        try:
            Path(strategy["file_path"]).unlink(missing_ok=True)
        except OSError:
            pass
        return {"deleted": True, "id": strategy_id}

    def list_strategies(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM strategies ORDER BY created_at ASC").fetchall()
        return [_row(row) for row in rows]

    def get_strategy(self, strategy_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
        return _row(row) if row else None

    def list_runs(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM strategy_runs ORDER BY created_at DESC LIMIT 100").fetchall()
        return [_row(row) for row in rows]

    def run_strategy(self, strategy_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        strategy = self.get_strategy(strategy_id)
        if not strategy:
            raise ValueError(f"unknown strategy_id: {strategy_id}")
        account_id = _required(payload, "account_id")
        sleeve_id = _required(payload, "sleeve_id")
        account = self.trading_store.get_account(account_id)
        sleeve = self.trading_store.get_sleeve(sleeve_id)
        if not account:
            raise ValueError(f"unknown account_id: {account_id}")
        if not sleeve or sleeve["account_id"] != account_id:
            raise ValueError(f"sleeve_id {sleeve_id} does not belong to account {account_id}")
        if not sleeve.get("active", True):
            raise ValueError(f"sleeve {sleeve_id} 已停用(策略暂停)，启用后再运行")

        symbols = payload.get("symbols") or ["600519.SH"]
        if isinstance(symbols, str):
            symbols = [item.strip().upper() for item in symbols.split(",") if item.strip()]
        symbols = [str(symbol).upper() for symbol in symbols]
        frequency = normalize_frequency(payload.get("frequency") or "5m")
        data_source = (payload.get("data_source") or "fixture").lower()
        provided_bars = payload.get("bars")
        if provided_bars is not None:
            bars = _provided_bars(provided_bars)
            connector_health = {"name": data_source, "status": "provided", "supported_frequencies": [frequency]}
        else:
            connector = self.connectors.get(data_source)
            bars = connector.get_bars(symbols, frequency=frequency, limit=int(payload.get("bar_limit") or 8))
            connector_health = connector.healthcheck()
        run_id = payload.get("run_id") or f"run_{uuid.uuid4().hex[:10]}"
        now = _now()
        self._insert_run(
            run_id=run_id,
            strategy_id=strategy_id,
            account_id=account_id,
            sleeve_id=sleeve_id,
            frequency=frequency,
            mode=f"{data_source}_replay",
            status="running",
            bars_processed=0,
            orders_submitted=0,
            error=None,
            created_at=now,
            finished_at=None,
        )
        self.audit_store.record_event(
            AuditEvent(
                timestamp=now,
                ledger_type="system",
                event_type="strategy_run_started",
                account_id=account_id,
                sleeve_id=sleeve_id,
                strategy_id=strategy_id,
                run_id=run_id,
                reason="strategy subprocess started",
                metadata={"symbols": symbols, "frequency": frequency, "bars": len(bars), "connector": connector_health},
            )
        )

        worker_result = self._run_worker(strategy, account, sleeve, bars, frequency, run_id)
        if not worker_result["ok"]:
            self._finish_run(run_id, "failed", len(bars), 0, worker_result.get("error"))
            self.audit_store.record_event(
                AuditEvent(
                    timestamp=_now(),
                    ledger_type="system",
                    event_type="strategy_run_failed",
                    account_id=account_id,
                    sleeve_id=sleeve_id,
                    strategy_id=strategy_id,
                    run_id=run_id,
                    reason=worker_result.get("error"),
                    metadata={"traceback": worker_result.get("traceback")},
                )
            )
            return {"run_id": run_id, "status": "failed", "error": worker_result.get("error"), "orders": []}

        source_event_ids: list[str] = []
        rejected: list[dict[str, Any]] = []
        for order in worker_result["orders"]:
            if order.get("event_type") == "strategy_log":
                self.audit_store.record_event(
                    AuditEvent(
                        timestamp=order.get("timestamp") or _now(),
                        ledger_type="system",
                        event_type="strategy_log",
                        account_id=account_id,
                        sleeve_id=sleeve_id,
                        strategy_id=strategy_id,
                        run_id=run_id,
                        reason=order.get("message"),
                        metadata={"level": order.get("level", "INFO")},
                    )
                )
                continue
            timing_gate = self._resolve_timing_gate(account_id, sleeve_id, strategy_id, payload)
            try:
                broker_result = self.trading_store.place_order(
                    {
                        "account_id": account_id,
                        "sleeve_id": sleeve_id,
                        "strategy_id": strategy_id,
                        "run_id": run_id,
                        "symbol": order["symbol"],
                        "side": order.get("side", "BUY"),
                        "quantity": order["quantity"],
                        "signal_price": order["signal_price"],
                        "fill_price": order["fill_price"],
                        "timestamp": order.get("timestamp"),
                        "allow_open": timing_gate["allow_open"],
                        "position_policy": timing_gate["position_policy"],
                        "timing_strategy_id": timing_gate["timing_strategy_id"],
                        "timing_reason": timing_gate.get("reason"),
                        "timing_decision_id": timing_gate.get("id"),
                        "timing_decision_event_id": timing_gate.get("audit_event_id"),
                        "timing_binding_id": timing_gate.get("binding_id"),
                        "signal_reason": order.get("reason", "strategy generated order"),
                        "frequency": order.get("frequency", frequency),
                    }
                )
                source_id = broker_result.get("source_event_id") or broker_result.get("event_id")
                if source_id:
                    source_event_ids.append(source_id)
                if not broker_result.get("accepted"):
                    rejected.append(broker_result)
            except Exception as exc:  # noqa: BLE001 - keep run alive for later orders.
                rejected.append({"accepted": False, "reason": str(exc), "order": order})

        status = "completed" if not rejected else "completed_with_rejections"
        self._finish_run(run_id, status, len(bars), len(source_event_ids), None)
        self.audit_store.record_event(
            AuditEvent(
                timestamp=_now(),
                ledger_type="system",
                event_type="strategy_run_completed",
                account_id=account_id,
                sleeve_id=sleeve_id,
                strategy_id=strategy_id,
                run_id=run_id,
                reason=status,
                metadata={"orders": len(source_event_ids), "rejections": rejected},
            )
        )
        return {
            "run_id": run_id,
            "status": status,
            "bars_processed": len(bars),
            "orders_submitted": len(source_event_ids),
            "source_event_ids": source_event_ids,
            "rejections": rejected,
        }

    def _resolve_timing_gate(
        self,
        account_id: str,
        sleeve_id: str,
        strategy_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.timing_store:
            gate = self.timing_store.resolve_gate(account_id, sleeve_id, strategy_id)
            if gate:
                return {
                    **gate,
                    "timing_strategy_id": gate["timing_strategy_id"],
                    "allow_open": bool(gate.get("allow_open", True)),
                    "position_policy": gate.get("position_policy") or "hold",
                }
        return {
            "timing_strategy_id": payload.get("timing_strategy_id", "strategy_runner_gate"),
            "allow_open": bool(payload.get("allow_open", True)),
            "position_policy": payload.get("position_policy") or "hold",
            "reason": payload.get("timing_reason"),
        }

    def _run_worker(
        self,
        strategy: dict[str, Any],
        account: dict[str, Any],
        sleeve: dict[str, Any],
        bars: list[dict[str, Any]],
        frequency: str,
        run_id: str,
    ) -> dict[str, Any]:
        payload = {
            "strategy_path": strategy["file_path"],
            "strategy_id": strategy["id"],
            "run_id": run_id,
            "account_id": account["id"],
            "sleeve_id": sleeve["id"],
            "frequency": frequency,
            "account": account,
            "sleeve": {**sleeve, "positions": self.trading_store.list_positions(sleeve["id"])},
            "bars": bars,
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as tmp:
            json.dump(payload, tmp, ensure_ascii=False)
            tmp_path = Path(tmp.name)
        try:
            completed = subprocess.run(
                _worker_command("strategy", str(tmp_path)),
                cwd=str(self.root_dir),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if not completed.stdout:
                return {"ok": False, "error": completed.stderr or "strategy worker produced no output"}
            return json.loads(completed.stdout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "strategy worker timed out"}
        finally:
            tmp_path.unlink(missing_ok=True)

    def _insert_run(
        self,
        *,
        run_id: str,
        strategy_id: str,
        account_id: str,
        sleeve_id: str,
        frequency: str,
        mode: str,
        status: str,
        bars_processed: int,
        orders_submitted: int,
        error: str | None,
        created_at: str,
        finished_at: str | None,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO strategy_runs (
                    id, strategy_id, account_id, sleeve_id, frequency, mode,
                    status, bars_processed, orders_submitted, error, created_at,
                    finished_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    strategy_id,
                    account_id,
                    sleeve_id,
                    frequency,
                    mode,
                    status,
                    bars_processed,
                    orders_submitted,
                    error,
                    created_at,
                    finished_at,
                ),
            )

    def _finish_run(self, run_id: str, status: str, bars_processed: int, orders_submitted: int, error: str | None) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE strategy_runs
                SET status = ?, bars_processed = ?, orders_submitted = ?,
                    error = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, bars_processed, orders_submitted, error, _now(), run_id),
            )


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or value == "":
        raise ValueError(f"{key} is required")
    return str(value)


def _provided_bars(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("bars must be a list of bar dictionaries")
    bars = [dict(item) for item in value if isinstance(item, dict)]
    if not bars:
        raise ValueError("bars must not be empty")
    return bars


SAMPLE_STRATEGY_CODE = '''\
def on_init(ctx):
    ctx.log("INFO", "demo momentum strategy initialized")


def on_bar(ctx, bar):
    # 示例：当 5m K 线收盘价高于开盘价时，下一个 bar 开盘买入 100 股。
    # 真实策略可以替换成因子打分、择时信号或组合调仓逻辑。
    if bar["symbol"] == "000001.SZ" and bar["close"] > bar["open"]:
        ctx.order_market(bar["symbol"], 100, side="BUY", reason="close > open momentum")
'''
