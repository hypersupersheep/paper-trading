from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend import app_settings
from backend import friction as friction_model
from backend.data_connectors import normalize_frequency
from backend.performance_store import benchmark_overlay, metrics_from_curve


class BacktestStore:
    """隔离回测引擎 + 结果持久化。

    引擎复用策略/择时 worker 生成信号(无前视:信号价=当前 close,成交价=下一 bar open),
    自己做逐 bar 组合记账(手续费/印花税/滑点 + 择时门控)与盯市,产出净值曲线/指标/成交,
    全程不触碰真实账户。结果存库,可列表、下载。
    """

    def __init__(self, db_path: str | Path, strategy_store: Any, timing_store: Any, connectors: Any):
        self.db_path = Path(db_path)
        self.strategy_store = strategy_store
        self.timing_store = timing_store
        self.connectors = connectors
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
                CREATE TABLE IF NOT EXISTS backtests (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    params TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    result TEXT NOT NULL
                )
                """
            )

    # ------------------------------- 运行 ------------------------------- #
    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        strategy_id = _required(payload, "strategy_id")
        strategy = self.strategy_store.get_strategy(strategy_id)
        if not strategy:
            raise ValueError(f"unknown strategy_id: {strategy_id}")

        symbols = _symbols(payload.get("symbols") or ["000001.SZ"])
        frequency = normalize_frequency(payload.get("frequency") or "1d")
        data_source = (payload.get("data_source") or app_settings.default_data_source()).lower()
        start = (payload.get("start") or "").strip() or None
        end = (payload.get("end") or "").strip() or None
        initial_cash = _float(payload.get("initial_cash"), 1_000_000.0)
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        friction = {
            "commission_rate": _float(payload.get("commission_rate"), 0.00008),
            "min_commission": _float(payload.get("min_commission"), 5.0),
            "stamp_duty_rate": _float(payload.get("stamp_duty_rate"), 0.001),
            "slippage_model": payload.get("slippage_model") or "adaptive",
            "slippage_value": _float(payload.get("slippage_value"), 1.0),
        }

        connector = self.connectors.get(data_source)
        all_bars = connector.get_bars(
            symbols, frequency=frequency, limit=int(payload.get("bar_limit") or 1000), start=start, end=end
        )
        bars = [bar for bar in all_bars if _in_range(str(bar.get("timestamp")), start, end)]
        bars.sort(key=lambda item: (str(item["timestamp"]), str(item["symbol"])))
        timestamps = sorted({str(bar["timestamp"]) for bar in bars})
        if len(timestamps) < 3:
            available = sorted({str(bar.get("timestamp"))[:10] for bar in all_bars})
            hint = (
                f"数据源 {data_source} 实际可取到 {available[0]} ~ {available[-1]}"
                if available
                else f"数据源 {data_source} 未返回数据"
            )
            raise ValueError(
                f"回测区间 {start or '?'} ~ {end or '?'} 内数据点不足(需≥3根)。{hint}。"
                "历史区间回测建议用米筐(ricequant)数据源(原生支持任意历史段);通达信日线可向前翻页但深度有限。"
            )

        # 1) 选股策略信号
        account = {"id": "bt_account", "name": "Backtest", "commission_rate": friction["commission_rate"],
                   "min_commission": friction["min_commission"], "stamp_duty_rate": friction["stamp_duty_rate"],
                   "slippage_model": friction["slippage_model"], "slippage_value": friction["slippage_value"],
                   "initial_cash": initial_cash, "cash": initial_cash}
        worker = self.strategy_store._run_worker(strategy, account, bars, frequency, "bt_strategy")
        if not worker.get("ok"):
            raise ValueError(f"策略回测失败: {worker.get('error')}")
        orders = [item for item in worker["orders"] if item.get("event_type") != "strategy_log"]

        # 2) 择时门控(可选)
        gate = []
        decision_count = 0
        timing_strategy_id = payload.get("timing_strategy_id") or None
        if timing_strategy_id:
            timing = self.timing_store.get_timing_strategy(str(timing_strategy_id))
            if not timing:
                raise ValueError(f"unknown timing_strategy_id: {timing_strategy_id}")
            timing_worker = self.timing_store._run_worker(timing, account, bars, frequency, "bt_timing")
            # 择时失败必须报错,绝不能静默吞掉(否则会"假装没有择时"照常跑,误导回测结论)。
            if not timing_worker.get("ok"):
                raise ValueError(f"择时策略回测失败: {timing_worker.get('error')}")
            gate = sorted(
                (
                    {
                        "timestamp": str(item.get("timestamp")),
                        "allow_open": bool(item.get("allow_open", True)),
                        "position_policy": item.get("position_policy") or "hold",
                    }
                    for item in timing_worker["decisions"]
                    if item.get("event_type") != "timing_log"
                ),
                key=lambda item: item["timestamp"],
            )
            decision_count = len(gate)
            if decision_count == 0:
                raise ValueError("择时策略未产出任何决策(检查 on_bar 是否调用了 ctx.set_decision)")

        # 3) 逐 bar 模拟
        sim = self._simulate(bars, timestamps, orders, gate, initial_cash, friction)

        # 4) 指标 + 基准
        perf = metrics_from_curve(sim["curve"], initial_cash)
        benchmark_symbol = (payload.get("benchmark") or "000300.SH").upper()
        benchmark = None
        if payload.get("benchmark") != "" and len(perf["curve"]) >= 3:
            try:
                bench_source = (payload.get("benchmark_source") or data_source).lower()
                bench_bars = self.connectors.get(bench_source).get_bars(
                    [benchmark_symbol], frequency="1d", limit=len(perf["curve"]) + 30, start=start, end=end
                )
                benchmark = benchmark_overlay(perf["curve"], bench_bars, benchmark_symbol)
            except Exception as exc:  # noqa: BLE001 - 基准失败不影响回测主体。
                benchmark = {"symbol": benchmark_symbol, "error": str(exc)}

        trades = sim["trades"]
        realized = [trade["realized"] for trade in trades if trade["side"] == "SELL"]
        win_trades = sum(1 for value in realized if value > 0)
        summary = {
            **perf["metrics"],
            "symbols": symbols,
            "frequency": frequency,
            "data_source": data_source,
            "start": timestamps[0][:10],
            "end": timestamps[-1][:10],
            "initial_cash": round(initial_cash, 2),
            "final_equity": perf["curve"][-1]["equity"] if perf["curve"] else initial_cash,
            "total_trades": len(trades),
            "closed_trades": len(realized),
            "trade_win_rate": round(win_trades / len(realized), 4) if realized else 0.0,
            "total_realized_pnl": round(sum(realized), 2),
            "rejected_orders": sim["rejected"],
            "timing_strategy_id": timing_strategy_id,
            "timing_decisions": decision_count,
            "excess_return": (benchmark or {}).get("metrics", {}).get("excess_return"),
            "benchmark_symbol": benchmark_symbol,
        }

        result = {
            "curve": perf["curve"],
            "metrics": perf["metrics"],
            "benchmark": benchmark,
            "trades": trades,
            "summary": summary,
            "params": {
                "strategy_id": strategy_id,
                "strategy_name": strategy.get("name"),
                "timing_strategy_id": timing_strategy_id,
                "symbols": symbols,
                "frequency": frequency,
                "data_source": data_source,
                "initial_cash": initial_cash,
                **friction,
                "benchmark": benchmark_symbol,
            },
        }
        record = self._persist(payload.get("name") or f"{strategy.get('name')} 回测", result)
        result["id"] = record["id"]
        result["created_at"] = record["created_at"]
        return result

    def _simulate(
        self,
        bars: list[dict[str, Any]],
        timestamps: list[str],
        orders: list[dict[str, Any]],
        gate: list[dict[str, Any]],
        initial_cash: float,
        friction: dict[str, float],
    ) -> dict[str, Any]:
        bars_by_ts: dict[str, list[dict[str, Any]]] = {}
        for bar in bars:
            bars_by_ts.setdefault(str(bar["timestamp"]), []).append(bar)
        orders_by_ts: dict[str, list[dict[str, Any]]] = {}
        for order in orders:
            orders_by_ts.setdefault(str(order.get("timestamp")), []).append(order)

        def gate_allows(ts: str) -> bool:
            decision = None
            for item in gate:
                if item["timestamp"] <= ts:
                    decision = item
                else:
                    break
            if decision is None:
                return True  # 未绑定/暂无决策:不拦截(回测里择时可选)
            return decision["allow_open"] and decision["position_policy"] not in {"reduce_only", "close_all"}

        cash = initial_cash
        positions: dict[str, dict[str, float]] = {}
        last_close: dict[str, float] = {}
        curve: dict[str, dict[str, Any]] = {}  # 按交易日折叠,保留每日最后净值
        trades: list[dict[str, Any]] = []
        rejected = 0
        slippage_model = friction.get("slippage_model", "adaptive")
        # 每标的已见过的 bar 历史:自适应滑点据此估 ADV/σ(只用截至当前 ts 的,无前视)。
        symbol_history: dict[str, list[dict[str, Any]]] = {}

        def _slip(sym: str, qty: float, px: float) -> float:
            ref = symbol_history.get(sym, [])[-friction_model.DEFAULT_ADV_WINDOW:]
            return friction_model.slippage_cost(
                slippage_model, quantity=qty, fill_price=px,
                slippage_value=friction["slippage_value"], ref_bars=ref,
            )

        for ts in timestamps:
            for bar in bars_by_ts.get(ts, []):
                sym = str(bar["symbol"]).upper()
                last_close[sym] = float(bar["close"])
                symbol_history.setdefault(sym, []).append(bar)

            for order in orders_by_ts.get(ts, []):
                symbol = str(order["symbol"]).upper()
                side = str(order.get("side", "BUY")).upper()
                quantity = int(order.get("quantity") or 0)
                price = float(order.get("fill_price") or order.get("signal_price") or 0)
                if quantity <= 0 or price <= 0:
                    rejected += 1
                    continue
                gross = quantity * price
                commission = max(round(gross * friction["commission_rate"], 2), friction["min_commission"])
                slippage = _slip(symbol, quantity, price)

                if side == "BUY":
                    if not gate_allows(ts):
                        rejected += 1
                        continue
                    cost = gross + commission + slippage
                    if cost > cash + 0.001:
                        rejected += 1
                        continue
                    cash -= cost
                    position = positions.setdefault(symbol, {"qty": 0.0, "cost": 0.0})
                    new_qty = position["qty"] + quantity
                    position["cost"] = (position["qty"] * position["cost"] + quantity * price) / new_qty
                    position["qty"] = new_qty
                    trades.append(_trade(ts, symbol, "BUY", quantity, price, commission, 0.0, slippage, 0.0))
                else:
                    position = positions.get(symbol)
                    held = int(position["qty"]) if position else 0
                    sell_qty = min(quantity, held)
                    if sell_qty <= 0:
                        rejected += 1
                        continue
                    sell_gross = sell_qty * price
                    sell_commission = max(round(sell_gross * friction["commission_rate"], 2), friction["min_commission"])
                    stamp = round(sell_gross * friction["stamp_duty_rate"], 2)
                    sell_slippage = _slip(symbol, sell_qty, price)
                    proceeds = sell_gross - sell_commission - stamp - sell_slippage
                    realized = round((price - position["cost"]) * sell_qty - sell_commission - stamp - sell_slippage, 2)
                    cash += proceeds
                    position["qty"] -= sell_qty
                    if position["qty"] <= 0:
                        positions.pop(symbol, None)
                    trades.append(_trade(ts, symbol, "SELL", sell_qty, price, sell_commission, stamp, sell_slippage, realized))

            market_value = sum(item["qty"] * last_close.get(symbol, item["cost"]) for symbol, item in positions.items())
            curve[ts[:10]] = {"time": ts[:10], "equity": round(cash + market_value, 2)}

        return {"curve": list(curve.values()), "trades": trades, "rejected": rejected}

    def _persist(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        backtest_id = f"bt_{uuid.uuid4().hex[:12]}"
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO backtests (id, name, created_at, params, summary, result) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    backtest_id,
                    name,
                    created_at,
                    json.dumps(result["params"], ensure_ascii=False),
                    json.dumps(result["summary"], ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                ),
            )
        return {"id": backtest_id, "created_at": created_at}

    def list_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id, name, created_at, summary FROM backtests ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            item = {"id": row["id"], "name": row["name"], "created_at": row["created_at"]}
            item["summary"] = json.loads(row["summary"])
            out.append(item)
        return out

    def get_run(self, backtest_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT result FROM backtests WHERE id = ?", (backtest_id,)).fetchone()
        return json.loads(row["result"]) if row else None


def _trade(ts: str, symbol: str, side: str, quantity: int, price: float,
           commission: float, stamp: float, slippage: float, realized: float) -> dict[str, Any]:
    return {
        "timestamp": ts,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": round(price, 4),
        "commission": commission,
        "stamp_duty": stamp,
        "slippage": slippage,
        "realized": realized,
    }


def _in_range(timestamp: str, start: str | None, end: str | None) -> bool:
    day = timestamp[:10]
    if start and day < start:
        return False
    if end and day > end:
        return False
    return True


def _required(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or value == "":
        raise ValueError(f"{key} is required")
    return str(value)


def _symbols(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [part.strip().upper() for part in value.split(",") if part.strip()]
    else:
        items = [str(part).strip().upper() for part in value if str(part).strip()]
    if not items:
        raise ValueError("symbols must not be empty")
    return items


def _float(value: Any, default: float) -> float:
    if value is None or value == "":
        return float(default)
    return float(value)
