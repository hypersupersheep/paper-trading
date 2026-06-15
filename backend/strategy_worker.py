from __future__ import annotations

import importlib.util
import json
import sys
import traceback
from pathlib import Path
from typing import Any


class WorkerContext:
    def __init__(self, payload: dict[str, Any]):
        self.account_id = payload["account_id"]
        self.sleeve_id = payload["sleeve_id"]
        self.strategy_id = payload["strategy_id"]
        self.run_id = payload["run_id"]
        self.frequency = payload["frequency"]
        self.initial_cash = payload["sleeve"]["allocated_cash"]
        self.available_cash = payload["sleeve"]["available_cash"]
        self.positions = {item["symbol"]: item for item in payload["sleeve"].get("positions", [])}
        self._bars = payload["bars"]
        self._index = 0
        self._orders: list[dict[str, Any]] = []

    @property
    def now(self) -> str:
        return self._bars[self._index]["timestamp"]

    def set_index(self, index: int) -> None:
        self._index = index

    def history(self, symbol: str, fields: list[str] | str, window: int, frequency: str | None = None):
        wanted = [fields] if isinstance(fields, str) else fields
        rows = [
            bar
            for bar in self._bars[: self._index + 1]
            if bar["symbol"] == symbol and (frequency is None or bar["frequency"] == frequency)
        ][-window:]
        return [{field: row.get(field) for field in wanted} for row in rows]

    def order_market(self, symbol: str, quantity: int, side: str = "BUY", reason: str | None = None) -> None:
        current = self._bars[self._index]
        next_bar = self._next_bar(symbol)
        self._orders.append(
            {
                "symbol": symbol,
                "side": side.upper(),
                "quantity": int(quantity),
                "signal_price": current["close"],
                "fill_price": (next_bar or current)["open"],
                "timestamp": (next_bar or current)["timestamp"],
                "reason": reason or "strategy ctx.order_market",
                "frequency": self.frequency,
            }
        )

    def order_target_percent(self, symbol: str, weight: float, reason: str | None = None) -> None:
        current = self._bars[self._index]
        next_bar = self._next_bar(symbol)
        price = (next_bar or current)["open"]
        current_qty = int(self.positions.get(symbol, {}).get("quantity", 0))
        target_value = self.initial_cash * float(weight)
        target_qty = int(target_value / price / 100) * 100
        delta = target_qty - current_qty
        if delta == 0:
            return
        self.order_market(
            symbol,
            abs(delta),
            side="BUY" if delta > 0 else "SELL",
            reason=reason or f"strategy target percent {weight}",
        )

    def log(self, level: str, message: str) -> None:
        self._orders.append(
            {
                "event_type": "strategy_log",
                "level": level,
                "message": message,
                "timestamp": self.now,
            }
        )

    def take_orders(self) -> list[dict[str, Any]]:
        orders = self._orders
        self._orders = []
        return orders

    def _next_bar(self, symbol: str) -> dict[str, Any] | None:
        for bar in self._bars[self._index + 1 :]:
            if bar["symbol"] == symbol:
                return bar
        return None


def main() -> int:
    payload_path = Path(sys.argv[1])
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    try:
        module = _load_strategy(Path(payload["strategy_path"]))
        ctx = WorkerContext(payload)
        if hasattr(module, "on_init"):
            module.on_init(ctx)

        emitted: list[dict[str, Any]] = []
        for index, bar in enumerate(payload["bars"]):
            ctx.set_index(index)
            if hasattr(module, "on_bar"):
                result = module.on_bar(ctx, bar)
                emitted.extend(_normalize_returned_orders(result))
            emitted.extend(ctx.take_orders())

        sys.stdout.write(json.dumps({"ok": True, "orders": emitted}, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 - worker boundary must report strategy failures.
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
                ensure_ascii=False,
            )
        )
        return 1


def _load_strategy(path: Path):
    spec = importlib.util.spec_from_file_location(f"strategy_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load strategy file: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "on_bar"):
        raise ValueError("strategy file must define on_bar(ctx, bar)")
    return module


def _normalize_returned_orders(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, dict):
        if "orders" in result and isinstance(result["orders"], list):
            return result["orders"]
        if {"symbol", "quantity"}.issubset(result):
            return [result]
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    return []


if __name__ == "__main__":
    raise SystemExit(main())
