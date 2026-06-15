"""量化模拟盘 Agent SDK —— 零依赖(仅标准库)的 REST 客户端。

任何 agent 都能 import 这个文件来驱动模拟盘:开户、导入策略、回测、读绩效、自审查。
故意只用 urllib(不依赖 requests),这样打包/分发后在任何有 Python 的环境都能跑。

典型用法:
    from paper_trading_client import PaperTradingClient
    pt = PaperTradingClient()           # 默认 http://127.0.0.1:8000
    pt.check_compatible()               # 先确认服务在线且 API 版本兼容
    sid = pt.import_strategy("我的动量", CODE)["strategy"]["id"]
    result = pt.run_backtest(sid, symbols="000001.SZ", start="2024-01-01", end="2025-01-01")
    print(result["metrics"]["sharpe"])
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class PaperTradingError(Exception):
    """所有客户端错误统一抛这个;消息里带上服务端返回的具体原因。"""


class PaperTradingClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ------------------------------ 传输层 ------------------------------ #
    def _request(self, method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
        url = self.base_url + path
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
            if query:
                url += "?" + query
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                message = json.loads(raw).get("error", raw)
            except json.JSONDecodeError:
                message = raw
            raise PaperTradingError(f"{method} {path} → HTTP {exc.code}: {message}") from None
        except urllib.error.URLError as exc:
            raise PaperTradingError(
                f"无法连接 {self.base_url}（模拟盘服务没启动?用 `python3 -m backend.server` 启动）: {exc.reason}"
            ) from None
        if not text:
            return {}
        if path.endswith("/export"):
            return text  # 导出端点返回 CSV/JSON 原文
        return json.loads(text)

    def _get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: Any = None) -> Any:
        return self._request("POST", path, body=body if body is not None else {})

    # ------------------------------ 元信息 ------------------------------ #
    def meta(self) -> dict:
        return self._get("/api/meta")

    def health(self) -> dict:
        return self._get("/api/health")

    def check_compatible(self, min_api_version: int = 1) -> dict:
        """握手:确认服务在线且 API 大版本不低于要求。agent 在干活前先调一次。"""
        meta = self.meta()
        if int(meta.get("api_version", 0)) < min_api_version:
            raise PaperTradingError(
                f"API 版本不兼容: 服务 v{meta.get('api_version')} < 需要 v{min_api_version}"
            )
        return meta

    # ------------------------------ 账户/sleeve ------------------------------ #
    def list_accounts(self) -> list[dict]:
        return self._get("/api/accounts")["accounts"]

    def create_account(self, name: str, initial_cash: float = 10_000_000, **friction: Any) -> dict:
        return self._post("/api/accounts", {"name": name, "initial_cash": initial_cash, **friction})["account"]

    def delete_account(self, account_id: str, force: bool = False) -> dict:
        """删除账户及其全部子数据。账户仍有持仓时需 force=True 才强删。"""
        return self._post(f"/api/accounts/{account_id}/delete", {"force": force})

    def create_sleeve(self, account_id: str, name: str, strategy_id: str, allocated_cash: float) -> dict:
        return self._post(
            f"/api/accounts/{account_id}/sleeves",
            {"name": name, "strategy_id": strategy_id, "allocated_cash": allocated_cash},
        )["sleeve"]

    def set_sleeve_active(self, sleeve_id: str, active: bool) -> dict:
        return self._post(f"/api/sleeves/{sleeve_id}/active", {"active": active})["sleeve"]

    def adjust_sleeve_allocation(self, sleeve_id: str, percent: float | None = None, allocated_cash: float | None = None) -> dict:
        body: dict = {}
        if percent is not None:
            body["percent"] = percent
        if allocated_cash is not None:
            body["allocated_cash"] = allocated_cash
        return self._post(f"/api/sleeves/{sleeve_id}/allocation", body)["sleeve"]

    # ------------------------------ 模拟券商 ------------------------------ #
    def place_order(self, account_id: str, sleeve_id: str, symbol: str, side: str, quantity: int, **opts: Any) -> dict:
        body = {"account_id": account_id, "sleeve_id": sleeve_id, "symbol": symbol, "side": side, "quantity": quantity}
        body.update(opts)
        return self._post("/api/broker/orders", body)

    def cancel_order(self, order_id: str, reason: str = "cancelled by agent") -> dict:
        return self._post(f"/api/broker/orders/{order_id}/cancel", {"reason": reason})

    def backfill_trade(
        self,
        account_id: str,
        sleeve_id: str,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        trade_date: str,
        **opts: Any,
    ) -> dict:
        """交易历史补充:补录此前未记录的真实历史成交(symbol/price/side/quantity/trade_date 必填)。

        绕过择时/风控门控,但保持账本一致。仅用于补历史,不要用它造正常交易。
        opts 可带 trade_time(HH:MM)、apply_fees(默认 True)、note。
        """
        body = {
            "account_id": account_id,
            "sleeve_id": sleeve_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "trade_date": trade_date,
        }
        body.update(opts)
        return self._post("/api/broker/backfill", body)

    def list_orders(self, **filters: Any) -> list[dict]:
        return self._get("/api/broker/orders", **filters)["orders"]

    # ------------------------------ 策略 ------------------------------ #
    def list_strategies(self) -> list[dict]:
        return self._get("/api/strategies")["strategies"]

    def import_strategy(self, name: str, code: str, source_filename: str | None = None) -> dict:
        return self._post("/api/strategies", {"name": name, "code": code, "source_filename": source_filename})

    def run_strategy(self, strategy_id: str, account_id: str, sleeve_id: str, **opts: Any) -> dict:
        body = {"account_id": account_id, "sleeve_id": sleeve_id, **opts}
        return self._post(f"/api/strategies/{strategy_id}/run", body)

    def delete_strategy(self, strategy_id: str) -> dict:
        return self._post(f"/api/strategies/{strategy_id}/delete")

    # ------------------------------ 择时策略 ------------------------------ #
    def list_timing_strategies(self) -> list[dict]:
        return self._get("/api/timing-strategies")["timing_strategies"]

    def import_timing_strategy(self, name: str, code: str) -> dict:
        return self._post("/api/timing-strategies", {"name": name, "code": code})

    def bind_timing(self, timing_strategy_id: str, strategy_id: str, account_id: str, sleeve_id: str | None = None) -> dict:
        return self._post(
            f"/api/timing-strategies/{timing_strategy_id}/bind",
            {"strategy_id": strategy_id, "account_id": account_id, "sleeve_id": sleeve_id, "active": True},
        )["binding"]

    def delete_timing_strategy(self, timing_strategy_id: str) -> dict:
        return self._post(f"/api/timing-strategies/{timing_strategy_id}/delete")

    # ------------------------------ 回测 ------------------------------ #
    def run_backtest(
        self,
        strategy_id: str,
        symbols: str,
        start: str | None = None,
        end: str | None = None,
        *,
        timing_strategy_id: str | None = None,
        frequency: str = "1d",
        data_source: str = "fixture",
        initial_cash: float = 1_000_000,
        benchmark: str = "000300.SH",
        benchmark_source: str | None = None,
        name: str | None = None,
        **friction: Any,
    ) -> dict:
        body = {
            "strategy_id": strategy_id,
            "timing_strategy_id": timing_strategy_id,
            "symbols": symbols,
            "start": start,
            "end": end,
            "frequency": frequency,
            "data_source": data_source,
            "initial_cash": initial_cash,
            "benchmark": benchmark,
            "benchmark_source": benchmark_source,
            "name": name or "agent backtest",
            **friction,
        }
        return self._post("/api/backtest/run", body)

    def list_backtests(self) -> list[dict]:
        return self._get("/api/backtest/runs")["runs"]

    def get_backtest(self, backtest_id: str) -> dict:
        return self._get(f"/api/backtest/{backtest_id}")

    def export_backtest(self, backtest_id: str, fmt: str = "csv") -> str:
        return self._get(f"/api/backtest/{backtest_id}/export", format=fmt)

    # ------------------------------ 组合/绩效 ------------------------------ #
    def portfolio(self, account_id: str | None = None, data_source: str = "fixture", frequency: str = "5m") -> dict:
        return self._get("/api/portfolio/summary", account_id=account_id, data_source=data_source, frequency=frequency)

    def performance(self, account_id: str | None = None, benchmark: str = "000300.SH", benchmark_source: str | None = None) -> dict:
        return self._get(
            "/api/portfolio/performance",
            account_id=account_id,
            benchmark=benchmark,
            benchmark_source=benchmark_source,
        )

    def record_snapshot(self, account_id: str | None = None) -> dict:
        return self._post("/api/portfolio/snapshot", {"account_id": account_id})

    # ------------------------------ 自选股/行情 ------------------------------ #
    def watchlist(self, data_source: str = "fixture") -> list[dict]:
        return self._get("/api/watchlist", data_source=data_source, frequency="1d")["symbols"]

    def add_watchlist(self, symbol: str) -> list[dict]:
        return self._post("/api/watchlist", {"symbol": symbol})["symbols"]

    def remove_watchlist(self, symbol: str) -> list[dict]:
        return self._post("/api/watchlist", {"symbol": symbol, "action": "remove"})["symbols"]

    def quotes(self, symbols: str, data_source: str = "fixture", frequency: str = "1d") -> list[dict]:
        return self._get("/api/quotes", symbols=symbols, data_source=data_source, frequency=frequency)["quotes"]

    # ------------------------------ 风控 ------------------------------ #
    def set_risk_config(self, account_id: str, sleeve_id: str | None = None, **limits: Any) -> dict:
        return self._post("/api/risk/configs", {"account_id": account_id, "sleeve_id": sleeve_id, **limits})["config"]

    def list_risk_configs(self, account_id: str | None = None) -> list[dict]:
        return self._get("/api/risk/configs", account_id=account_id)["configs"]

    # ------------------------------ 审计 ------------------------------ #
    def audit_chain(self, event_id: str) -> dict:
        return self._get(f"/api/audit/chain/{event_id}")

    def audit_events(self, **filters: Any) -> list[dict]:
        return self._get("/api/audit/events", **filters)["events"]

    # ------------------------------ 数据源 ------------------------------ #
    def connectors_health(self) -> list[dict]:
        return self._get("/api/data/connectors/health")["connectors"]
