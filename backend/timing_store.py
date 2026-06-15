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
from backend.trading_store import TradingStore
from backend.workers import worker_command as _worker_command


POSITION_POLICIES = {"hold", "reduce_only", "close_all", "target_exposure"}


class TimingStore:
    def __init__(
        self,
        db_path: str | Path,
        audit_store: AuditStore,
        trading_store: TradingStore,
        timing_dir: str | Path | None = None,
    ):
        self.db_path = Path(db_path)
        self.audit_store = audit_store
        self.trading_store = trading_store
        self.timing_dir = Path(timing_dir) if timing_dir else self.db_path.parent / "timing_strategies"
        self.root_dir = Path(__file__).resolve().parents[1]
        self.timing_dir.mkdir(parents=True, exist_ok=True)
        self.connectors = DataConnectorRegistry()
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
                CREATE TABLE IF NOT EXISTS timing_strategies (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS timing_strategy_runs (
                    id TEXT PRIMARY KEY,
                    timing_strategy_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    sleeve_id TEXT,
                    strategy_id TEXT,
                    frequency TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    bars_processed INTEGER NOT NULL,
                    decisions_recorded INTEGER NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    FOREIGN KEY(timing_strategy_id) REFERENCES timing_strategies(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS timing_strategy_bindings (
                    id TEXT PRIMARY KEY,
                    timing_strategy_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    sleeve_id TEXT,
                    active INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(timing_strategy_id) REFERENCES timing_strategies(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS timing_decisions (
                    id TEXT PRIMARY KEY,
                    audit_event_id TEXT NOT NULL,
                    timing_strategy_id TEXT NOT NULL,
                    strategy_id TEXT,
                    account_id TEXT NOT NULL,
                    sleeve_id TEXT,
                    run_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    symbol TEXT,
                    allow_open INTEGER NOT NULL,
                    position_policy TEXT NOT NULL,
                    target_exposure REAL,
                    valid_until TEXT,
                    reason TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(timing_strategy_id) REFERENCES timing_strategies(id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_timing_bindings_lookup
                ON timing_strategy_bindings (strategy_id, account_id, sleeve_id, active)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_timing_decisions_latest
                ON timing_decisions (timing_strategy_id, account_id, sleeve_id, strategy_id, timestamp)
                """
            )

    def seed_demo(self) -> None:
        if self.list_timing_strategies():
            return
        self.create_timing_strategy(
            {
                "id": "timing_demo_regime",
                "name": "Demo Market Regime Gate",
                "code": SAMPLE_TIMING_CODE,
            },
            seed=True,
        )

    def create_timing_strategy(self, payload: dict[str, Any], *, seed: bool = False) -> dict[str, Any]:
        name = payload.get("name") or "Timing Strategy"
        # 同选股策略：常见写法自动接驱动，信号返回值翻译成 TimingDecision。
        adapted = adapt_strategy_code(payload.get("code"), kind="择时策略", flavor="timing")
        timing_strategy_id = payload.get("id") or f"timing_{uuid.uuid4().hex[:10]}"
        file_path = self.timing_dir / f"{timing_strategy_id}.py"
        file_path.write_text(adapted["code"], encoding="utf-8")
        now = _now()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO timing_strategies (id, name, file_path, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (timing_strategy_id, name, str(file_path), now),
            )
        if not seed:
            self.audit_store.record_event(
                AuditEvent(
                    timestamp=now,
                    ledger_type="system",
                    event_type="timing_strategy_imported",
                    account_id="workspace",
                    strategy_id=timing_strategy_id,
                    reason="python timing strategy imported",
                    metadata={
                        "name": name,
                        "file_path": str(file_path),
                        "source_filename": payload.get("source_filename"),
                        "adapter_mode": adapted["mode"],
                        "adapter_entry": adapted["entry"],
                    },
                )
            )
        timing_strategy = self.get_timing_strategy(timing_strategy_id) or {
            "id": timing_strategy_id,
            "name": name,
            "file_path": str(file_path),
            "created_at": now,
        }
        timing_strategy["adapter"] = {"mode": adapted["mode"], "entry": adapted["entry"]}
        return timing_strategy

    def delete_timing_strategy(self, timing_strategy_id: str) -> dict[str, Any]:
        timing_strategy = self.get_timing_strategy(timing_strategy_id)
        if not timing_strategy:
            raise ValueError(f"unknown timing_strategy_id: {timing_strategy_id}")
        with self._connection() as conn:
            # 先清子表(runs/decisions/bindings 有外键)。
            conn.execute("DELETE FROM timing_strategy_runs WHERE timing_strategy_id = ?", (timing_strategy_id,))
            conn.execute("DELETE FROM timing_decisions WHERE timing_strategy_id = ?", (timing_strategy_id,))
            conn.execute("DELETE FROM timing_strategy_bindings WHERE timing_strategy_id = ?", (timing_strategy_id,))
            conn.execute("DELETE FROM timing_strategies WHERE id = ?", (timing_strategy_id,))
        try:
            Path(timing_strategy["file_path"]).unlink(missing_ok=True)
        except OSError:
            pass
        return {"deleted": True, "id": timing_strategy_id}

    def list_timing_strategies(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM timing_strategies ORDER BY created_at ASC").fetchall()
        return [_row(row) for row in rows]

    def get_timing_strategy(self, timing_strategy_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM timing_strategies WHERE id = ?", (timing_strategy_id,)).fetchone()
        return _row(row) if row else None

    def list_runs(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM timing_strategy_runs ORDER BY created_at DESC LIMIT 100").fetchall()
        return [_row(row) for row in rows]

    def list_bindings(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        filters = filters or {}
        clauses: list[str] = []
        params: list[Any] = []
        for field in ("timing_strategy_id", "strategy_id", "account_id", "sleeve_id"):
            value = filters.get(field)
            if value:
                clauses.append(f"{field} = ?")
                params.append(value)
        if filters.get("active") is not None:
            clauses.append("active = ?")
            params.append(1 if filters.get("active") else 0)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(f"SELECT * FROM timing_strategy_bindings {where} ORDER BY updated_at DESC", params).fetchall()
        return [_decode_binding(row) for row in rows]

    def list_decisions(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        filters = filters or {}
        clauses: list[str] = []
        params: list[Any] = []
        for field in ("timing_strategy_id", "strategy_id", "account_id", "sleeve_id", "run_id", "symbol"):
            value = filters.get(field)
            if value:
                clauses.append(f"{field} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = int(filters.get("limit") or 100)
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT rowid, * FROM timing_decisions {where} ORDER BY timestamp DESC, rowid DESC LIMIT ?",
                params,
            ).fetchall()
        return [_decode_decision(row) for row in rows]

    def bind_strategy(self, timing_strategy_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.get_timing_strategy(timing_strategy_id):
            raise ValueError(f"unknown timing_strategy_id: {timing_strategy_id}")
        strategy_id = _required(payload, "strategy_id")
        account_id = _required(payload, "account_id")
        sleeve_id = payload.get("sleeve_id") or None
        if not self.trading_store.get_account(account_id):
            raise ValueError(f"unknown account_id: {account_id}")
        if sleeve_id:
            sleeve = self.trading_store.get_sleeve(sleeve_id)
            if not sleeve or sleeve["account_id"] != account_id:
                raise ValueError(f"sleeve_id {sleeve_id} does not belong to account {account_id}")

        now = _now()
        active = 1 if payload.get("active", True) else 0
        with self._connection() as conn:
            existing = conn.execute(
                """
                SELECT id FROM timing_strategy_bindings
                WHERE timing_strategy_id = ? AND strategy_id = ? AND account_id = ?
                  AND COALESCE(sleeve_id, '') = COALESCE(?, '')
                """,
                (timing_strategy_id, strategy_id, account_id, sleeve_id),
            ).fetchone()
            if existing:
                binding_id = existing["id"]
                conn.execute(
                    """
                    UPDATE timing_strategy_bindings
                    SET active = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (active, now, binding_id),
                )
            else:
                binding_id = f"timing_bind_{uuid.uuid4().hex[:10]}"
                conn.execute(
                    """
                    INSERT INTO timing_strategy_bindings (
                        id, timing_strategy_id, strategy_id, account_id,
                        sleeve_id, active, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (binding_id, timing_strategy_id, strategy_id, account_id, sleeve_id, active, now, now),
                )

        self.audit_store.record_event(
            AuditEvent(
                timestamp=now,
                ledger_type="system",
                event_type="timing_strategy_bound",
                account_id=account_id,
                sleeve_id=sleeve_id,
                strategy_id=timing_strategy_id,
                reason="timing strategy bound to stock-picking strategy",
                metadata={"binding_id": binding_id, "controlled_strategy_id": strategy_id, "active": bool(active)},
            )
        )
        bindings = self.list_bindings({"timing_strategy_id": timing_strategy_id, "strategy_id": strategy_id, "account_id": account_id})
        for binding in bindings:
            if binding["id"] == binding_id:
                return binding
        return {"id": binding_id, "timing_strategy_id": timing_strategy_id, "strategy_id": strategy_id, "account_id": account_id}

    def run_timing_strategy(self, timing_strategy_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        timing_strategy = self.get_timing_strategy(timing_strategy_id)
        if not timing_strategy:
            raise ValueError(f"unknown timing_strategy_id: {timing_strategy_id}")

        account_id = _required(payload, "account_id")
        account = self.trading_store.get_account(account_id)
        if not account:
            raise ValueError(f"unknown account_id: {account_id}")
        sleeve_id = payload.get("sleeve_id") or None
        sleeve = None
        if sleeve_id:
            sleeve = self.trading_store.get_sleeve(str(sleeve_id))
            if not sleeve or sleeve["account_id"] != account_id:
                raise ValueError(f"sleeve_id {sleeve_id} does not belong to account {account_id}")

        symbols = payload.get("symbols") or ["000001.SZ"]
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
        run_id = payload.get("run_id") or f"timing_run_{uuid.uuid4().hex[:10]}"
        controlled_strategy_id = payload.get("strategy_id") or payload.get("controlled_strategy_id") or None
        now = _now()
        self._insert_run(
            run_id=run_id,
            timing_strategy_id=timing_strategy_id,
            account_id=account_id,
            sleeve_id=sleeve_id,
            strategy_id=controlled_strategy_id,
            frequency=frequency,
            mode=f"{data_source}_timing_replay",
            status="running",
            bars_processed=0,
            decisions_recorded=0,
            error=None,
            created_at=now,
            finished_at=None,
        )
        self.audit_store.record_event(
            AuditEvent(
                timestamp=now,
                ledger_type="system",
                event_type="timing_strategy_run_started",
                account_id=account_id,
                sleeve_id=sleeve_id,
                strategy_id=timing_strategy_id,
                run_id=run_id,
                reason="timing strategy subprocess started",
                metadata={"symbols": symbols, "frequency": frequency, "bars": len(bars), "connector": connector_health},
            )
        )

        worker_result = self._run_worker(timing_strategy, account, sleeve, bars, frequency, run_id)
        if not worker_result["ok"]:
            self._finish_run(run_id, "failed", len(bars), 0, worker_result.get("error"))
            self.audit_store.record_event(
                AuditEvent(
                    timestamp=_now(),
                    ledger_type="system",
                    event_type="timing_strategy_run_failed",
                    account_id=account_id,
                    sleeve_id=sleeve_id,
                    strategy_id=timing_strategy_id,
                    run_id=run_id,
                    reason=worker_result.get("error"),
                    metadata={"traceback": worker_result.get("traceback")},
                )
            )
            return {"run_id": run_id, "status": "failed", "error": worker_result.get("error"), "decisions": []}

        decision_ids: list[str] = []
        decisions: list[dict[str, Any]] = []
        for decision in worker_result["decisions"]:
            if decision.get("event_type") == "timing_log":
                self.audit_store.record_event(
                    AuditEvent(
                        timestamp=decision.get("timestamp") or _now(),
                        ledger_type="system",
                        event_type="timing_log",
                        account_id=account_id,
                        sleeve_id=sleeve_id,
                        strategy_id=timing_strategy_id,
                        run_id=run_id,
                        reason=decision.get("message"),
                        metadata={"level": decision.get("level", "INFO")},
                    )
                )
                continue
            recorded = self._record_decision(
                timing_strategy_id=timing_strategy_id,
                account_id=account_id,
                sleeve_id=sleeve_id,
                strategy_id=controlled_strategy_id,
                run_id=run_id,
                decision=decision,
            )
            decision_ids.append(recorded["audit_event_id"])
            decisions.append(recorded)

        self._finish_run(run_id, "completed", len(bars), len(decision_ids), None)
        self.audit_store.record_event(
            AuditEvent(
                timestamp=_now(),
                ledger_type="system",
                event_type="timing_strategy_run_completed",
                account_id=account_id,
                sleeve_id=sleeve_id,
                strategy_id=timing_strategy_id,
                run_id=run_id,
                reason="completed",
                metadata={"decisions": len(decision_ids)},
            )
        )
        return {
            "run_id": run_id,
            "status": "completed",
            "bars_processed": len(bars),
            "decisions_recorded": len(decision_ids),
            "decision_event_ids": decision_ids,
            "latest_decision": decisions[-1] if decisions else None,
        }

    def resolve_gate(self, account_id: str, sleeve_id: str, strategy_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            binding = conn.execute(
                """
                SELECT * FROM timing_strategy_bindings
                WHERE active = 1 AND strategy_id = ? AND account_id = ?
                  AND (sleeve_id IS NULL OR sleeve_id = ?)
                ORDER BY CASE WHEN sleeve_id = ? THEN 0 ELSE 1 END, updated_at DESC
                LIMIT 1
                """,
                (strategy_id, account_id, sleeve_id, sleeve_id),
            ).fetchone()
            if not binding:
                return None
            decision = conn.execute(
                """
                SELECT rowid, * FROM timing_decisions
                WHERE timing_strategy_id = ? AND account_id = ?
                  AND (sleeve_id IS NULL OR sleeve_id = ?)
                  AND (strategy_id IS NULL OR strategy_id = ?)
                ORDER BY
                  CASE WHEN sleeve_id = ? THEN 0 ELSE 1 END,
                  CASE WHEN strategy_id = ? THEN 0 ELSE 1 END,
                  timestamp DESC,
                  rowid DESC
                LIMIT 1
                """,
                (binding["timing_strategy_id"], account_id, sleeve_id, strategy_id, sleeve_id, strategy_id),
            ).fetchone()

        binding_item = _decode_binding(binding)
        if not decision:
            return {
                "timing_strategy_id": binding_item["timing_strategy_id"],
                "binding_id": binding_item["id"],
                "allow_open": False,
                "position_policy": "reduce_only",
                "target_exposure": None,
                "reason": "active timing binding has no decision yet",
                "metadata": {"missing_decision": True},
            }

        decision_item = _decode_decision(decision)
        if _is_expired(decision_item.get("valid_until")):
            return {
                **decision_item,
                "binding_id": binding_item["id"],
                "allow_open": False,
                "position_policy": "reduce_only",
                "reason": "latest timing decision expired",
                "metadata": {**decision_item.get("metadata", {}), "expired": True},
            }
        return {**decision_item, "binding_id": binding_item["id"]}

    def _record_decision(
        self,
        *,
        timing_strategy_id: str,
        account_id: str,
        sleeve_id: str | None,
        strategy_id: str | None,
        run_id: str,
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        position_policy = str(decision.get("position_policy") or "hold")
        if position_policy not in POSITION_POLICIES:
            raise ValueError(f"position_policy must be one of {sorted(POSITION_POLICIES)}")
        allow_open = bool(decision.get("allow_open", True))
        if position_policy in {"reduce_only", "close_all"} and "allow_open" not in decision:
            allow_open = False
        timestamp = decision.get("timestamp") or _now()
        metadata = {
            **(decision.get("metadata") or {}),
            "allow_open": allow_open,
            "position_policy": position_policy,
            "target_exposure": decision.get("target_exposure"),
            "valid_until": decision.get("valid_until"),
        }
        audit_event_id = self.audit_store.record_event(
            AuditEvent(
                event_id=f"timing_dec_{uuid.uuid4().hex[:12]}",
                timestamp=timestamp,
                ledger_type="decision",
                event_type="timing_decision",
                account_id=account_id,
                sleeve_id=sleeve_id,
                strategy_id=timing_strategy_id,
                run_id=run_id,
                symbol=decision.get("symbol"),
                reason=decision.get("reason") or "timing strategy decision",
                metadata=metadata,
            )
        )
        decision_id = f"timing_decision_{uuid.uuid4().hex[:10]}"
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO timing_decisions (
                    id, audit_event_id, timing_strategy_id, strategy_id,
                    account_id, sleeve_id, run_id, timestamp, symbol, allow_open,
                    position_policy, target_exposure, valid_until, reason, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    audit_event_id,
                    timing_strategy_id,
                    strategy_id,
                    account_id,
                    sleeve_id,
                    run_id,
                    timestamp,
                    decision.get("symbol"),
                    1 if allow_open else 0,
                    position_policy,
                    decision.get("target_exposure"),
                    decision.get("valid_until"),
                    decision.get("reason") or "timing strategy decision",
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
        return self.list_decisions({"run_id": run_id, "limit": 1})[0]

    def _run_worker(
        self,
        timing_strategy: dict[str, Any],
        account: dict[str, Any],
        sleeve: dict[str, Any] | None,
        bars: list[dict[str, Any]],
        frequency: str,
        run_id: str,
    ) -> dict[str, Any]:
        payload = {
            "strategy_path": timing_strategy["file_path"],
            "timing_strategy_id": timing_strategy["id"],
            "run_id": run_id,
            "account_id": account["id"],
            "sleeve_id": sleeve["id"] if sleeve else None,
            "frequency": frequency,
            "account": account,
            "sleeve": {**sleeve, "positions": self.trading_store.list_positions(sleeve["id"])} if sleeve else {},
            "bars": bars,
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as tmp:
            json.dump(payload, tmp, ensure_ascii=False)
            tmp_path = Path(tmp.name)
        try:
            completed = subprocess.run(
                _worker_command("timing", str(tmp_path)),
                cwd=str(self.root_dir),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if not completed.stdout:
                return {"ok": False, "error": completed.stderr or "timing worker produced no output"}
            return json.loads(completed.stdout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timing worker timed out"}
        finally:
            tmp_path.unlink(missing_ok=True)

    def _insert_run(
        self,
        *,
        run_id: str,
        timing_strategy_id: str,
        account_id: str,
        sleeve_id: str | None,
        strategy_id: str | None,
        frequency: str,
        mode: str,
        status: str,
        bars_processed: int,
        decisions_recorded: int,
        error: str | None,
        created_at: str,
        finished_at: str | None,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO timing_strategy_runs (
                    id, timing_strategy_id, account_id, sleeve_id, strategy_id,
                    frequency, mode, status, bars_processed, decisions_recorded,
                    error, created_at, finished_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    timing_strategy_id,
                    account_id,
                    sleeve_id,
                    strategy_id,
                    frequency,
                    mode,
                    status,
                    bars_processed,
                    decisions_recorded,
                    error,
                    created_at,
                    finished_at,
                ),
            )

    def _finish_run(self, run_id: str, status: str, bars_processed: int, decisions_recorded: int, error: str | None) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE timing_strategy_runs
                SET status = ?, bars_processed = ?, decisions_recorded = ?,
                    error = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, bars_processed, decisions_recorded, error, _now(), run_id),
            )


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _decode_binding(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["active"] = bool(item["active"])
    return item


def _decode_decision(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["allow_open"] = bool(item["allow_open"])
    item["metadata"] = json.loads(item.get("metadata") or "{}")
    return item


def _is_expired(valid_until: str | None) -> bool:
    if not valid_until:
        return False
    try:
        value = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
    except ValueError:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value < datetime.now(timezone.utc)


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


SAMPLE_TIMING_CODE = '''\
def on_init(ctx):
    ctx.log("INFO", "demo timing strategy initialized")


def on_bar(ctx, bar):
    # 示例：用最近 3 根 K 线判断市场环境。
    # 收盘价高于 3 根前的收盘价，则允许选股策略开仓；否则只允许减仓。
    history = ctx.history(bar["symbol"], ["close"], 3)
    if len(history) < 3:
        return
    allow_open = bar["close"] >= history[0]["close"]
    ctx.set_decision(
        allow_open=allow_open,
        position_policy="hold" if allow_open else "reduce_only",
        reason="3-bar market regime is risk-on" if allow_open else "3-bar market regime is risk-off",
        metadata={"lookback": 3, "last_close": bar["close"], "first_close": history[0]["close"]},
    )
'''
