from __future__ import annotations

import importlib.util
import json
import sys
import traceback
from pathlib import Path
from typing import Any


POSITION_POLICIES = {"hold", "reduce_only", "close_all", "target_exposure"}


class TimingContext:
    def __init__(self, payload: dict[str, Any]):
        self.account_id = payload["account_id"]
        self.timing_strategy_id = payload["timing_strategy_id"]
        self.run_id = payload["run_id"]
        self.frequency = payload["frequency"]
        self.account = payload["account"]
        self.positions = {item["symbol"]: item for item in payload.get("positions", [])}
        self._bars = payload["bars"]
        self._index = 0
        self._decisions: list[dict[str, Any]] = []
        self._logs: list[dict[str, Any]] = []

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

    def set_decision(
        self,
        *,
        allow_open: bool = True,
        position_policy: str = "hold",
        target_exposure: float | None = None,
        reason: str | None = None,
        valid_until: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        current = self._bars[self._index]
        self._decisions.append(
            _normalize_decision(
                {
                    "timestamp": current["timestamp"],
                    "symbol": current["symbol"],
                    "allow_open": allow_open,
                    "position_policy": position_policy,
                    "target_exposure": target_exposure,
                    "reason": reason or "timing strategy decision",
                    "valid_until": valid_until,
                    "metadata": metadata or {},
                }
            )
        )

    def log(self, level: str, message: str) -> None:
        self._logs.append(
            {
                "event_type": "timing_log",
                "level": level,
                "message": message,
                "timestamp": self.now,
            }
        )

    def take_decisions(self) -> list[dict[str, Any]]:
        decisions = self._decisions
        self._decisions = []
        return decisions

    def take_logs(self) -> list[dict[str, Any]]:
        logs = self._logs
        self._logs = []
        return logs


def main() -> int:
    payload_path = Path(sys.argv[1])
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    try:
        module = _load_strategy(Path(payload["strategy_path"]))
        ctx = TimingContext(payload)
        if hasattr(module, "on_init"):
            module.on_init(ctx)

        emitted: list[dict[str, Any]] = []
        for index, bar in enumerate(payload["bars"]):
            ctx.set_index(index)
            result = module.on_bar(ctx, bar)
            emitted.extend(_normalize_returned_decisions(result))
            emitted.extend(ctx.take_decisions())
            emitted.extend(ctx.take_logs())

        sys.stdout.write(json.dumps({"ok": True, "decisions": emitted}, ensure_ascii=False))
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
    spec = importlib.util.spec_from_file_location(f"timing_strategy_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load timing strategy file: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "on_bar"):
        raise ValueError("timing strategy file must define on_bar(ctx, bar)")
    return module


def _normalize_returned_decisions(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, dict):
        if "decisions" in result and isinstance(result["decisions"], list):
            return [_normalize_decision(item) for item in result["decisions"] if isinstance(item, dict)]
        if _looks_like_decision(result):
            return [_normalize_decision(result)]
    if isinstance(result, list):
        return [_normalize_decision(item) for item in result if isinstance(item, dict) and _looks_like_decision(item)]
    return []


def _looks_like_decision(item: dict[str, Any]) -> bool:
    return bool({"allow_open", "position_policy", "target_exposure"}.intersection(item))


def _normalize_decision(item: dict[str, Any]) -> dict[str, Any]:
    position_policy = str(item.get("position_policy") or "hold")
    if position_policy not in POSITION_POLICIES:
        raise ValueError(f"position_policy must be one of {sorted(POSITION_POLICIES)}")
    allow_open = bool(item.get("allow_open", True))
    if position_policy in {"reduce_only", "close_all"} and "allow_open" not in item:
        allow_open = False
    return {
        "event_type": "timing_decision",
        "timestamp": item.get("timestamp"),
        "symbol": item.get("symbol"),
        "allow_open": allow_open,
        "position_policy": position_policy,
        "target_exposure": None if item.get("target_exposure") is None else float(item["target_exposure"]),
        "reason": item.get("reason") or "timing strategy decision",
        "valid_until": item.get("valid_until"),
        "metadata": item.get("metadata") or {},
    }


if __name__ == "__main__":
    raise SystemExit(main())
