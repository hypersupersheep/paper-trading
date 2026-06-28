from __future__ import annotations

from typing import Any

from backend import app_settings
from backend.audit_store import AuditStore
from backend.data_connectors import DataConnectorRegistry, normalize_frequency


class ChartService:
    def __init__(self, audit_store: AuditStore, connectors: DataConnectorRegistry):
        self.audit_store = audit_store
        self.connectors = connectors

    def get_bars(self, query: dict[str, str | None]) -> dict[str, Any]:
        symbol = _required(query, "symbol").upper()
        frequency = normalize_frequency(query.get("frequency") or "5m")
        data_source = (query.get("data_source") or app_settings.default_data_source()).lower()
        limit = min(max(int(query.get("limit") or 80), 1), 800)
        connector = self.connectors.get(data_source)
        bars = connector.get_bars([symbol], frequency=frequency, limit=limit)
        return {
            "symbol": symbol,
            "frequency": frequency,
            "data_source": data_source,
            "bars": bars,
            "connector": connector.healthcheck(),
        }

    def get_markers(self, query: dict[str, str | None]) -> dict[str, Any]:
        symbol = _required(query, "symbol").upper()
        filters = {
            "ledger_type": "trade",
            "symbol": symbol,
            "account_id": query.get("account_id"),
            "strategy_id": query.get("strategy_id"),
            "run_id": query.get("run_id"),
            "limit": query.get("limit") or "500",
        }
        trades = self.audit_store.list_events(filters)
        markers = []
        for trade in reversed(trades):
            side = str(trade.get("metadata", {}).get("side", "")).upper()
            markers.append(
                {
                    "id": trade["id"],
                    "time": trade["timestamp"],
                    "symbol": trade["symbol"],
                    "side": side or "TRADE",
                    "price": trade["price"],
                    "quantity": trade["quantity"],
                    "amount": trade["amount"],
                    "account_id": trade["account_id"],
                    "strategy_id": trade["strategy_id"],
                    "run_id": trade["run_id"],
                    "source_event_id": trade["source_event_id"],
                }
            )
        return {"symbol": symbol, "markers": markers}


def _required(query: dict[str, str | None], key: str) -> str:
    value = query.get(key)
    if value is None or value == "":
        raise ValueError(f"{key} is required")
    return str(value)
