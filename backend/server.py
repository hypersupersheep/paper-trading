from __future__ import annotations

import json
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from backend import app_settings
from backend import names as security_names
from backend import paths
from backend import repo
from backend.audit_store import AuditEvent, LEDGER_TYPES, AuditStore
from backend.backtest_store import BacktestStore
from backend.chart_service import ChartService
from backend.connector_settings import mask_secret, save_connector_settings
from backend.data_connectors import normalize_frequency
from backend.nav_reconstruction import prev_trading_day as _prev_trading_day, reconstruct as reconstruct_nav
from backend.performance_store import PerformanceStore, metrics_from_curve
from backend.risk_store import RiskStore
from backend.scheduler_store import SchedulerStore
from backend.strategy_store import StrategyStore
from backend.timing_store import TimingStore
from backend.trading_store import TradingStore
from backend.version import API_VERSION, APP_NAME, __version__
from backend.watchlist_store import WatchlistStore


# 路径全部从 backend.paths 取(可由 PAPER_TRADING_HOME 覆盖,默认=代码根)。
PUBLIC_DIR = paths.public_dir()
DB_PATH = paths.db_path()


class AuditRequestHandler(BaseHTTPRequestHandler):
    store = AuditStore(DB_PATH)
    trading = TradingStore(DB_PATH, store)
    risk = RiskStore(DB_PATH, store, trading)
    trading.risk_store = risk
    timing = TimingStore(DB_PATH, store, trading, paths.timing_strategies_dir())
    strategies = StrategyStore(DB_PATH, store, trading, paths.strategies_dir(), timing)
    scheduler = SchedulerStore(DB_PATH, store, trading, strategies, timing)
    charts = ChartService(store, strategies.connectors)
    watchlist = WatchlistStore(DB_PATH)
    performance = PerformanceStore(DB_PATH)
    scheduler.performance = performance  # scheduler tick 完成时自动追加 NAV 快照
    backtest = BacktestStore(DB_PATH, strategies, timing, strategies.connectors)
    # broker 复用 strategy store 的 connector registry，下单省略价格时按行情 close 定价。
    trading.connectors = strategies.connectors

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}

        try:
            if path == "/api/health":
                self._json({"status": "ok", "database": str(DB_PATH)})
                return
            if path == "/api/meta":
                self._json(self._meta())
                return
            if path == "/api/settings/data-location":
                self._json(self._data_location())
                return
            if path == "/api/settings/data-source":
                self._json({"default_data_source": app_settings.default_data_source(), "data_sources": self.strategies.connectors.names()})
                return
            if path == "/api/accounts":
                self._json({"accounts": self.trading.list_accounts()})
                return
            if path == "/api/strategies":
                self._json({"strategies": self.strategies.list_strategies(), "runs": self.strategies.list_runs()})
                return
            if path == "/api/timing-strategies":
                self._json(
                    {
                        "timing_strategies": self.timing.list_timing_strategies(),
                        "runs": self.timing.list_runs(),
                        "bindings": self.timing.list_bindings(),
                        "decisions": self.timing.list_decisions(),
                    }
                )
                return
            if path == "/api/scheduler/tasks":
                self._json({"tasks": self.scheduler.list_tasks(), "ticks": self.scheduler.list_ticks()})
                return
            if path == "/api/broker/orders":
                self._json({"orders": self.trading.list_orders(query)})
                return
            if path == "/api/portfolio/summary":
                self._json(self._portfolio_summary(query))
                return
            if path == "/api/portfolio/performance":
                self._json(self._performance(query))
                return
            if path.startswith("/api/scheduler/tasks/") and path.endswith("/ticks"):
                task_id = unquote(path.removeprefix("/api/scheduler/tasks/").removesuffix("/ticks").strip("/"))
                self._json({"ticks": self.scheduler.list_ticks(task_id)})
                return
            if path.startswith("/api/timing-strategies/") and path.endswith("/signals"):
                timing_strategy_id = unquote(path.removeprefix("/api/timing-strategies/").removesuffix("/signals").strip("/"))
                query["timing_strategy_id"] = timing_strategy_id
                self._json({"decisions": self.timing.list_decisions(query)})
                return
            if path == "/api/risk/configs":
                self._json({"configs": self.risk.list_configs(query.get("account_id"))})
                return
            if path == "/api/backtest/runs":
                self._json({"runs": self.backtest.list_runs()})
                return
            if path.startswith("/api/backtest/") and path.endswith("/export"):
                backtest_id = unquote(path.removeprefix("/api/backtest/").removesuffix("/export").strip("/"))
                run = self.backtest.get_run(backtest_id)
                if not run:
                    self._json({"error": f"unknown backtest: {backtest_id}"}, HTTPStatus.NOT_FOUND)
                    return
                export_format = query.get("format", "json")
                content_type, body = _export_backtest(run, export_format)
                self._send(
                    HTTPStatus.OK,
                    body.encode("utf-8"),
                    content_type,
                    {"Content-Disposition": f'attachment; filename="backtest-{backtest_id}.{export_format}"'},
                )
                return
            if path.startswith("/api/backtest/"):
                backtest_id = unquote(path.removeprefix("/api/backtest/").strip("/"))
                run = self.backtest.get_run(backtest_id)
                if not run:
                    self._json({"error": f"unknown backtest: {backtest_id}"}, HTTPStatus.NOT_FOUND)
                    return
                self._json(run)
                return
            if path == "/api/watchlist":
                self._json({"symbols": self._watchlist_with_quotes(query)})
                return
            if path == "/api/quotes":
                symbols = (query.get("symbols") or "").split(",")
                self._json({"quotes": self._quote_symbols(symbols, query.get("data_source"), query.get("frequency"))})
                return
            if path == "/api/data/connectors/health":
                self._json({"connectors": self.strategies.connectors.health()})
                return
            if path == "/api/repo/instruments":
                self._json({"instruments": repo.INSTRUMENTS, "default": repo.DEFAULT_SYMBOL})
                return
            if path == "/api/repo/rate":
                symbol = (query.get("symbol") or repo.DEFAULT_SYMBOL).upper()
                connector = self.strategies.connectors.get(query.get("data_source"))
                quote = repo.fetch_latest_rate(connector, symbol)
                self._json(quote or {"symbol": symbol, "annual_rate": None, "error": "行情取不到该逆回购利率"})
                return
            if path == "/api/chart/bars":
                self._json(self.charts.get_bars(query))
                return
            if path == "/api/chart/markers":
                self._json(self.charts.get_markers(query))
                return
            if path.startswith("/api/accounts/") and path.endswith("/reverse-repo"):
                account_id = unquote(path.removeprefix("/api/accounts/").removesuffix("/reverse-repo").strip("/"))
                self._json(self.trading.list_reverse_repo(account_id))
                return
            if path.startswith("/api/accounts/"):
                account_id = unquote(path.removeprefix("/api/accounts/").strip("/"))
                account = self.trading.get_account(account_id)
                if not account:
                    self._json({"error": f"unknown account_id: {account_id}"}, HTTPStatus.NOT_FOUND)
                    return
                account["sleeves"] = self.trading.list_sleeves(account_id)
                self._json({"account": account})
                return
            if path.startswith("/api/audit/chain/"):
                event_id = unquote(path.removeprefix("/api/audit/chain/"))
                self._json(self.store.get_chain(event_id))
                return
            if path == "/api/audit/export":
                export_format = query.pop("format", "csv")
                content_type, body = self.store.export_events(query, export_format)
                filename = f"audit-export.{export_format}"
                self._send(
                    HTTPStatus.OK,
                    body.encode("utf-8"),
                    content_type,
                    {"Content-Disposition": f'attachment; filename="{filename}"'},
                )
                return
            if path == "/api/audit/trades":
                trades = self.store.trade_summaries(query)
                self._fill_trade_names(query.get("data_source"), trades)
                self._json({"trades": trades})
                return
            if path == "/api/audit/pnl":
                board = self.store.realized_pnl_by_symbol(query)
                self._fill_trade_names(query.get("data_source"), board["symbols"])
                self._json(board)
                return
            if path.startswith("/api/audit/"):
                ledger_key = path.removeprefix("/api/audit/").strip("/")
                if ledger_key not in LEDGER_TYPES:
                    self._json({"error": f"unknown ledger endpoint: {ledger_key}"}, HTTPStatus.NOT_FOUND)
                    return
                ledger_type = LEDGER_TYPES[ledger_key]
                if ledger_type:
                    query["ledger_type"] = ledger_type
                self._json({"events": self.store.list_events(query)})
                return

            self._static(path)
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - keeps local server usable during prototype work.
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            payload = self._read_json()
            if path == "/api/accounts":
                self._json({"account": self.trading.create_account(payload)}, HTTPStatus.CREATED)
                return
            if path == "/api/strategies":
                self._json({"strategy": self.strategies.create_strategy(payload)}, HTTPStatus.CREATED)
                return
            if path == "/api/timing-strategies":
                self._json({"timing_strategy": self.timing.create_timing_strategy(payload)}, HTTPStatus.CREATED)
                return
            if path == "/api/scheduler/tasks":
                self._json({"task": self.scheduler.create_task(payload)}, HTTPStatus.CREATED)
                return
            if path == "/api/risk/configs":
                self._json({"config": self.risk.upsert_config(payload)}, HTTPStatus.CREATED)
                return
            if path == "/api/data/connectors/ricequant/credentials":
                self._json(self._save_ricequant_credentials(payload), HTTPStatus.CREATED)
                return
            if path == "/api/data/connectors/wind/credentials":
                self._json(self._save_wind_credentials(payload), HTTPStatus.CREATED)
                return
            if path == "/api/settings/data-location":
                self._json(self._set_data_location(payload), HTTPStatus.CREATED)
                return
            if path == "/api/settings/data-location/reset":
                paths.clear_home_pointer()
                self._json({"ok": True, "restart_required": True, "default": str(self._default_home())}, HTTPStatus.CREATED)
                return
            if path == "/api/settings/data-source":
                self._json(app_settings.set_default_data_source(payload.get("default_data_source")), HTTPStatus.CREATED)
                return
            if path == "/api/portfolio/snapshot":
                self._json(self._record_snapshot(payload), HTTPStatus.CREATED)
                return
            if path == "/api/backtest/run":
                self._json(self.backtest.run(payload), HTTPStatus.CREATED)
                return
            if path == "/api/watchlist":
                symbol = payload.get("symbol")
                if (payload.get("action") or "add") == "remove":
                    self.watchlist.remove(symbol)
                else:
                    self.watchlist.add(symbol, note=payload.get("note"))
                self._json({"symbols": self.watchlist.list_symbols()}, HTTPStatus.CREATED)
                return
            if path.startswith("/api/strategies/") and path.endswith("/delete"):
                strategy_id = unquote(path.removeprefix("/api/strategies/").removesuffix("/delete").strip("/"))
                self._json(self.strategies.delete_strategy(strategy_id), HTTPStatus.CREATED)
                return
            if path.startswith("/api/timing-strategies/") and path.endswith("/delete"):
                timing_strategy_id = unquote(path.removeprefix("/api/timing-strategies/").removesuffix("/delete").strip("/"))
                self._json(self.timing.delete_timing_strategy(timing_strategy_id), HTTPStatus.CREATED)
                return
            if path.startswith("/api/accounts/") and path.endswith("/delete"):
                account_id = unquote(path.removeprefix("/api/accounts/").removesuffix("/delete").strip("/"))
                self._json(self.trading.delete_account(account_id, payload), HTTPStatus.CREATED)
                return
            if path.startswith("/api/strategies/") and path.endswith("/run"):
                strategy_id = unquote(path.removeprefix("/api/strategies/").removesuffix("/run").strip("/"))
                self._json(self.strategies.run_strategy(strategy_id, payload), HTTPStatus.CREATED)
                return
            if path.startswith("/api/scheduler/tasks/") and path.endswith("/start"):
                task_id = unquote(path.removeprefix("/api/scheduler/tasks/").removesuffix("/start").strip("/"))
                self._json({"task": self.scheduler.start_task(task_id)}, HTTPStatus.CREATED)
                return
            if path.startswith("/api/scheduler/tasks/") and path.endswith("/stop"):
                task_id = unquote(path.removeprefix("/api/scheduler/tasks/").removesuffix("/stop").strip("/"))
                self._json({"task": self.scheduler.stop_task(task_id)}, HTTPStatus.CREATED)
                return
            if path.startswith("/api/scheduler/tasks/") and path.endswith("/tick"):
                task_id = unquote(path.removeprefix("/api/scheduler/tasks/").removesuffix("/tick").strip("/"))
                self._json({"tick": self.scheduler.tick_once(task_id, force=bool(payload.get("force", False)))}, HTTPStatus.CREATED)
                return
            if path.startswith("/api/timing-strategies/") and path.endswith("/run"):
                timing_strategy_id = unquote(path.removeprefix("/api/timing-strategies/").removesuffix("/run").strip("/"))
                self._json(self.timing.run_timing_strategy(timing_strategy_id, payload), HTTPStatus.CREATED)
                return
            if path.startswith("/api/timing-strategies/") and path.endswith("/bind"):
                timing_strategy_id = unquote(path.removeprefix("/api/timing-strategies/").removesuffix("/bind").strip("/"))
                self._json({"binding": self.timing.bind_strategy(timing_strategy_id, payload)}, HTTPStatus.CREATED)
                return
            if path.startswith("/api/accounts/") and path.endswith("/sleeves"):
                account_id = unquote(path.removeprefix("/api/accounts/").removesuffix("/sleeves").strip("/"))
                self._json({"sleeve": self.trading.create_sleeve(account_id, payload)}, HTTPStatus.CREATED)
                return
            if path.startswith("/api/sleeves/") and path.endswith("/active"):
                sleeve_id = unquote(path.removeprefix("/api/sleeves/").removesuffix("/active").strip("/"))
                self._json({"sleeve": self.trading.set_sleeve_active(sleeve_id, payload)}, HTTPStatus.CREATED)
                return
            if path.startswith("/api/sleeves/") and path.endswith("/allocation"):
                sleeve_id = unquote(path.removeprefix("/api/sleeves/").removesuffix("/allocation").strip("/"))
                self._json({"sleeve": self.trading.adjust_sleeve_allocation(sleeve_id, payload)}, HTTPStatus.CREATED)
                return
            if path.startswith("/api/accounts/") and path.endswith("/reverse-repo/reconcile"):
                account_id = unquote(path.removeprefix("/api/accounts/").removesuffix("/reverse-repo/reconcile").strip("/"))
                account = self.trading.get_account(account_id)
                if not account:
                    raise ValueError(f"unknown account_id: {account_id}")
                recon = self._reconstruct_nav(account, payload.get("data_source") or app_settings.default_data_source())
                self._json(self.trading.sync_auto_repo(account_id, recon["repo_schedule"]), HTTPStatus.CREATED)
                return
            if path.startswith("/api/accounts/") and path.endswith("/reverse-repo"):
                account_id = unquote(path.removeprefix("/api/accounts/").removesuffix("/reverse-repo").strip("/"))
                self._json(self.trading.run_reverse_repo(account_id, payload), HTTPStatus.CREATED)
                return
            if path.startswith("/api/broker/orders/") and path.endswith("/cancel"):
                order_id = unquote(path.removeprefix("/api/broker/orders/").removesuffix("/cancel").strip("/"))
                self._json(self.trading.cancel_order(order_id, payload), HTTPStatus.CREATED)
                return
            if path == "/api/broker/orders":
                self._json(self.trading.place_order(payload), HTTPStatus.CREATED)
                return
            if path == "/api/broker/backfill":
                # 交易历史补充:仅补录历史成交,绕过门控但保持账本一致(见 TradingStore.backfill_trade)。
                self._json(self.trading.backfill_trade(payload), HTTPStatus.CREATED)
                return
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format: str, *args: object) -> None:
        return

    @staticmethod
    def _refresh_names(connector: Any, symbols: list[str]) -> None:
        """best-effort:用数据源给尚未命名(resolve 后仍等于代码)的标的取名并缓存。"""
        if not symbols or not hasattr(connector, "get_names"):
            return
        pending = [s for s in symbols if security_names.resolve(s) == str(s).upper()]
        if not pending:
            return
        try:
            fetched = connector.get_names(pending)
            if fetched:
                security_names.update(fetched)
        except Exception:
            pass

    def _fill_trade_names(self, data_source: Any, rows: list[dict[str, Any]]) -> None:
        """日志页自愈:对仍只有代码(name==symbol)的标的,用数据源批量取名并缓存,再回填本次结果。"""
        unknown = sorted({r["symbol"] for r in rows if r.get("symbol") and r.get("name") == r["symbol"]})
        if not unknown:
            return
        try:
            connector = self.strategies.connectors.get(data_source)
            self._refresh_names(connector, unknown)
        except Exception:  # noqa: BLE001 - 取名失败不影响主流程,继续显示代码
            pass
        for row in rows:
            if row.get("symbol") and row.get("name") == row["symbol"]:
                row["name"] = security_names.resolve(row["symbol"])

    def _portfolio_summary(self, query: dict[str, str | None]) -> dict:
        account_id = query.get("account_id")
        data_source = query.get("data_source")
        if not data_source:
            return self.trading.get_portfolio_summary(account_id)

        frequency = normalize_frequency(query.get("frequency") or "5m")
        connector = self.strategies.connectors.get(data_source)
        symbols = self.trading.list_position_symbols(account_id)
        # 顺带用数据源给未命名的持仓取名并缓存(best-effort,绝不影响盯市)。
        self._refresh_names(connector, symbols)
        mark_prices: dict[str, dict] = {}
        if symbols:
            # 多拉几根 bar: 最新 close 做盯市价, 整段收益序列算 bar 级波动率。
            bars = connector.get_bars(symbols, frequency=frequency, limit=20)
            closes_by_symbol: dict[str, list[tuple[str, float]]] = {}
            for bar in bars:
                symbol = str(bar["symbol"]).upper()
                closes_by_symbol.setdefault(symbol, []).append((str(bar.get("timestamp")), float(bar["close"])))
            for symbol, series in closes_by_symbol.items():
                series.sort(key=lambda item: item[0])
                timestamp, price = series[-1]
                mark_prices[symbol] = {
                    "price": price,
                    "timestamp": timestamp,
                    "data_source": data_source,
                    "frequency": frequency,
                    "volatility": _bar_volatility([close for _, close in series]),
                }
        return self.trading.get_portfolio_summary(
            account_id,
            mark_prices=mark_prices,
            mark_metadata={
                "mode": "connector_close",
                "data_source": data_source,
                "frequency": frequency,
                "symbols": symbols,
                "marked_symbols": sorted(mark_prices),
                "connector": connector.healthcheck(),
            },
        )

    @staticmethod
    def _default_home():
        return paths.user_data_dir() if paths.frozen() else paths.ROOT

    def _data_location(self) -> dict:
        return {
            "current": str(paths.home()),
            "is_custom": paths._read_pointer() is not None,
            "default": str(self._default_home()),
            "frozen": paths.frozen(),
        }

    def _set_data_location(self, payload: dict) -> dict:
        target = str(payload.get("path") or "").strip()
        if not target:
            raise ValueError("path is required")
        old_home = paths.home()
        new_home = paths.set_home_pointer(target)
        moved: list[str] = []
        if payload.get("move_existing") and old_home.resolve() != new_home.resolve():
            import shutil

            for sub in ("data", "strategies", "timing_strategies"):
                src = old_home / sub
                if src.exists():
                    shutil.copytree(src, new_home / sub, dirs_exist_ok=True)
                    moved.append(sub)
        return {"ok": True, "path": str(new_home), "moved": moved, "restart_required": True}

    def _meta(self) -> dict:
        """能力发现端点:agent / skill 的统一入口。读 api_version 做兼容判断,读 capabilities 决定能做什么。

        这是"演进而不打破旧客户端"的锚点——新增能力时这里加标志位,破坏性改动才 +1 api_version。
        """
        return {
            "name": APP_NAME,
            "version": __version__,
            "api_version": API_VERSION,
            "data_home": str(paths.home()),
            "data_sources": self.strategies.connectors.names(),
            "default_data_source": app_settings.default_data_source(),
            "capabilities": {
                "accounts": True,
                "sleeves": True,
                "paper_broker": True,
                "market_pricing": True,
                "risk_gate": True,
                "stock_strategies": True,
                "timing_strategies": True,
                "strategy_adapter": True,
                "scheduler": True,
                "backtest": True,
                "performance_tearsheet": True,
                "benchmark_overlay": True,
                "watchlist": True,
                "audit_chain": True,
                "trade_backfill": True,
                "export": ["csv", "json"],
            },
            # agent 发现用的主端点目录(按域分组);详细参数见 skill/README。
            "endpoints": {
                "meta": "GET /api/meta",
                "accounts": "GET|POST /api/accounts",
                "delete_account": "POST /api/accounts/{id}/delete",
                "place_order": "POST /api/broker/orders",
                "trade_backfill": "POST /api/broker/backfill",
                "strategies": "GET|POST /api/strategies",
                "run_strategy": "POST /api/strategies/{id}/run",
                "timing_strategies": "GET|POST /api/timing-strategies",
                "backtest_run": "POST /api/backtest/run",
                "backtest_list": "GET /api/backtest/runs",
                "performance": "GET /api/portfolio/performance",
                "portfolio": "GET /api/portfolio/summary",
                "watchlist": "GET|POST /api/watchlist",
                "quotes": "GET /api/quotes",
                "audit_chain": "GET /api/audit/chain/{event_id}",
                "connectors_health": "GET /api/data/connectors/health",
            },
        }

    def _resolve_account_id(self, account_id: str | None) -> str:
        if account_id:
            return account_id
        accounts = self.trading.list_accounts()
        if not accounts:
            raise ValueError("no account available")
        return accounts[0]["id"]

    @staticmethod
    def _today_cn() -> str:
        return datetime.now(timezone(timedelta(hours=8))).date().isoformat()

    def _reconstruct_nav(self, account: dict, data_source: str) -> dict:
        """从账本(成交+现金流)重建净值曲线 + 逐日逆回购计划。供绩效展示与逆回购补全共用。"""
        account_id = account["id"]
        trade_events = self.store.list_events({"event_type": "trade_filled", "account_id": account_id, "limit": 100000})
        fills = [
            {
                "timestamp": e["timestamp"],
                "symbol": e["symbol"],
                "side": (e.get("metadata") or {}).get("side", "BUY"),
                "quantity": e["quantity"],
                "price": e["price"],
            }
            for e in trade_events
            if e.get("symbol") and e.get("quantity") and e.get("price")
        ]
        cash_events = self.store.list_events({"ledger_type": "cash", "account_id": account_id, "limit": 100000})
        skip = {"sleeve_allocation", "sleeve_allocation_adjusted"}
        cash_flows = [
            {"timestamp": e["timestamp"], "amount": e["amount"]}
            for e in cash_events
            if e.get("amount") is not None
            and e["event_type"] not in skip
            and not str(e["event_type"]).startswith("reverse_repo")
        ]
        today = self._today_cn()
        daily_closes: dict[str, dict[str, float]] = {}
        repo_rates: dict[str, float] = {}
        symbols = sorted({str(f["symbol"]).upper() for f in fills})
        if fills and symbols:
            start_date = min(str(f["timestamp"])[:10] for f in fills)
            connector = self.strategies.connectors.get(data_source or app_settings.default_data_source())
            try:
                bars = connector.get_bars(symbols, frequency="1d", limit=2000, start=start_date, end=today)
                for bar in bars:
                    sym = str(bar["symbol"]).upper()
                    daily_closes.setdefault(sym, {})[str(bar["timestamp"])[:10]] = float(bar["close"])
            except Exception:  # noqa: BLE001 - 盯市取数失败就用成交价兜底
                daily_closes = {}
            # 闲置现金的逐日逆回购利率:按实时行情(GC001 逐日年化),取不到则回退账户默认利率。
            repo_rates = repo.fetch_daily_rates(connector, repo.DEFAULT_SYMBOL, start=start_date, end=today)
        return reconstruct_nav(
            initial_cash=account["initial_cash"],
            fills=fills,
            cash_flows=cash_flows,
            daily_closes=daily_closes,
            repo_annual_rate=account["reverse_repo_annual_rate"],
            today=today,
            repo_enabled=bool(account["auto_reverse_repo_enabled"]),
            repo_rates=repo_rates,
        )

    def _performance(self, query: dict) -> dict:
        account_id = self._resolve_account_id(query.get("account_id"))
        account = self.trading.get_account(account_id)
        if not account:
            raise ValueError(f"unknown account_id: {account_id}")
        mark_source = query.get("data_source") or app_settings.default_data_source()
        recon = self._reconstruct_nav(account, mark_source)
        curve = recon["curve"]
        if curve:
            # 口径一致性:① 曲线起点锚定到初始资金(首笔成交前一交易日),累计收益对初始资金算;
            #            ② 末点用与头部相同的实时权益,保证绩效"当前权益"与组合概览一致。
            initial = round(float(account["initial_cash"]), 2)
            day0 = _prev_trading_day(curve[0]["time"])
            if day0 < curve[0]["time"]:
                curve = [{"time": day0, "equity": initial, "cash": initial, "market_value": 0.0}, *curve]
            try:
                live = self._portfolio_summary({"account_id": account_id, "data_source": mark_source})
                live_equity = round(float(live["accounts"][0]["equity"]), 2)
                curve = [*curve[:-1], {**curve[-1], "equity": live_equity}]
            except Exception:  # noqa: BLE001 - 实时盯市失败就用重建末点,不影响展示
                pass
        result = metrics_from_curve(curve, account["initial_cash"])
        result["account_id"] = account_id
        result["account_name"] = account["name"]
        result["start_date"] = recon["start_date"]
        result["repo_interest_total"] = round(sum(r["interest"] for r in recon["repo_schedule"]), 2)
        result["trade_count"] = len(
            self.store.list_events({"event_type": "trade_filled", "account_id": account_id, "limit": 100000})
        )

        # 基准叠加(默认沪深300):best-effort,拉不到/对不齐就返回 None,前端只画策略曲线。
        benchmark_symbol = (query.get("benchmark") or "000300.SH").upper()
        result["benchmark"] = None
        if result.get("curve") and len(result["curve"]) >= 3:
            try:
                connector = self.strategies.connectors.get(query.get("benchmark_source") or query.get("data_source") or app_settings.default_data_source())
                bench_bars = connector.get_bars([benchmark_symbol], frequency="1d", limit=len(result["curve"]) + 30)
                result["benchmark"] = self.performance.compute_benchmark(result["curve"], bench_bars, benchmark_symbol)
            except Exception as exc:  # noqa: BLE001 - 基准失败不影响策略绩效展示。
                result["benchmark"] = {"symbol": benchmark_symbol, "error": str(exc)}
        return result

    def _record_snapshot(self, payload: dict) -> dict:
        account_id = self._resolve_account_id(payload.get("account_id"))
        summary = self.trading.get_portfolio_summary(account_id)["accounts"]
        if not summary:
            raise ValueError(f"unknown account_id: {account_id}")
        account = summary[0]
        snap = self.performance.record_snapshot(
            account_id,
            equity=account["equity"],
            cash=account["total_cash"],
            market_value=account["market_value"],
            pnl=account["pnl"],
            pnl_pct=account["pnl_pct"],
            source="manual",
        )
        return {"snapshot": snap}

    def _quote_symbols(self, symbols: list[str], data_source: str | None, frequency: str | None) -> list[dict]:
        clean = [str(item).strip().upper() for item in symbols if str(item).strip()]
        if not clean:
            return []
        freq = normalize_frequency(frequency or "1d")
        source = (data_source or app_settings.default_data_source()).lower()
        connector = self.strategies.connectors.get(source)
        try:
            bars = connector.get_bars(clean, frequency=freq, limit=2)
        except Exception as exc:  # noqa: BLE001 - 行情拉取失败时逐标的回报错误,不影响其余视图。
            return [{"symbol": symbol, "error": str(exc)} for symbol in clean]
        by_symbol: dict[str, list[dict]] = {}
        for bar in bars:
            by_symbol.setdefault(str(bar["symbol"]).upper(), []).append(bar)
        out: list[dict] = []
        for symbol in clean:
            series = sorted(by_symbol.get(symbol, []), key=lambda item: str(item.get("timestamp")))
            if not series:
                out.append({"symbol": symbol, "error": "no data"})
                continue
            last = series[-1]
            last_close = float(last["close"])
            prev_close = float(series[-2]["close"]) if len(series) >= 2 else float(last["open"])
            change = round(last_close - prev_close, 4)
            out.append(
                {
                    "symbol": symbol,
                    "last": last_close,
                    "prev_close": prev_close,
                    "change": change,
                    "change_pct": round(change / prev_close, 6) if prev_close else 0.0,
                    "volume": last.get("volume"),
                    "timestamp": last.get("timestamp"),
                    "data_source": source,
                }
            )
        return out

    def _watchlist_with_quotes(self, query: dict) -> list[dict]:
        meta = self.watchlist.list_symbols()
        quotes = {
            quote["symbol"]: quote
            for quote in self._quote_symbols([item["symbol"] for item in meta], query.get("data_source"), query.get("frequency"))
        }
        return [{**item, **quotes.get(item["symbol"], {"symbol": item["symbol"]})} for item in meta]

    def _save_wind_credentials(self, payload: dict) -> dict:
        from backend.connector_settings import get_connector_settings

        host = str(payload.get("host") or "").strip()
        if not host:
            raise ValueError("host is required")
        existing = get_connector_settings("wind")
        # 密码留空 = 沿用已保存的(改 host/user 时不必重输密码)。
        password = str(payload.get("password") or "") or existing.get("password", "")
        config = {
            "host": host,
            "port": int(payload.get("port") or 3306),
            "user": str(payload.get("user") or "").strip(),
            "password": password,
            "database": str(payload.get("database") or "wind_data").strip(),
        }
        save_connector_settings("wind", config)
        self.store.record_event(
            AuditEvent(
                ledger_type="system",
                event_type="connector_config_updated",
                account_id="workspace",
                reason="wind database connection saved",
                metadata={"connector": "wind", "endpoint": f"{config['host']}:{config['port']}/{config['database']}", "user": config["user"]},
            )
        )
        connector = self.strategies.connectors.get("wind")
        test: dict = {"ok": False}
        try:
            # 用最近一段日期取一根日 K 验证连通(VPN 没开会失败,属正常)。
            bars = connector.get_bars(["000001.SZ"], frequency="1d", limit=1)
            test = {"ok": True, "sample_bar": bars[-1] if bars else None}
        except Exception as exc:  # noqa: BLE001
            test = {"ok": False, "error": str(exc)}
        return {"saved": True, "endpoint": f"{config['host']}:{config['port']}/{config['database']}", "test": test, "health": connector.healthcheck()}

    def _save_ricequant_credentials(self, payload: dict) -> dict:
        license_key = str(payload.get("license_key") or "").strip()
        if not license_key:
            raise ValueError("license_key is required")
        save_connector_settings("ricequant", {"license_key": license_key})
        # 审计只记掩码, 完整密钥永不落审计流水。
        self.store.record_event(
            AuditEvent(
                ledger_type="system",
                event_type="connector_config_updated",
                account_id="workspace",
                reason="ricequant license key saved",
                metadata={"connector": "ricequant", "license_key_masked": mask_secret(license_key)},
            )
        )
        connector = self.strategies.connectors.get("ricequant")
        test: dict = {"ok": False}
        try:
            bars = connector.get_bars(["000001.SZ"], frequency="1d", limit=1)
            test = {"ok": True, "sample_bar": bars[-1] if bars else None}
        except Exception as exc:  # noqa: BLE001 - 把连接失败原因原样带给前端。
            test = {"ok": False, "error": str(exc)}
        return {"saved": True, "license_key_masked": mask_secret(license_key), "test": test, "health": connector.healthcheck()}

    def _static(self, path: str) -> None:
        if path in {"", "/"}:
            path = "/index.html"
        target = (PUBLIC_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(PUBLIC_DIR.resolve())) or not target.exists() or target.is_dir():
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(HTTPStatus.OK, target.read_bytes(), content_type)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def _export_backtest(run: dict, export_format: str) -> tuple[str, str]:
    if export_format == "json":
        return "application/json; charset=utf-8", json.dumps(run, ensure_ascii=False, indent=2)
    if export_format != "csv":
        raise ValueError("format must be csv or json")
    # CSV:净值曲线 + 基准 + 回撤,一行一日,方便 Excel/Obsidian 复盘。
    import csv
    from io import StringIO

    bench_by_time = {point["time"]: point["value"] for point in ((run.get("benchmark") or {}).get("series") or [])}
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", "equity", "drawdown_pct", "benchmark"])
    for point in run.get("curve", []):
        writer.writerow(
            [
                point["time"],
                point["equity"],
                round(point.get("drawdown", 0) * 100, 4),
                bench_by_time.get(point["time"], ""),
            ]
        )
    return "text/csv; charset=utf-8", output.getvalue()


def _bar_volatility(closes: list[float]) -> float | None:
    """bar 级波动率: 相邻 close 收益率的样本标准差。样本不足时返回 None。"""
    if len(closes) < 3:
        return None
    returns = [
        (closes[index] / closes[index - 1]) - 1
        for index in range(1, len(closes))
        if closes[index - 1]
    ]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return round(variance ** 0.5, 6)


def run() -> None:
    port = int(os.environ.get("PORT", "8000"))
    AuditRequestHandler.trading.seed_demo()
    AuditRequestHandler.timing.seed_demo()
    AuditRequestHandler.strategies.seed_demo()
    AuditRequestHandler.scheduler.seed_demo()
    AuditRequestHandler.watchlist.seed_demo()
    # 为每个账户 seed 一段日频 NAV 历史,绩效 tearsheet 首屏即有曲线。
    for account in AuditRequestHandler.trading.list_accounts():
        summary = AuditRequestHandler.trading.get_portfolio_summary(account["id"])["accounts"]
        if summary:
            AuditRequestHandler.performance.seed_demo(
                account["id"],
                equity_now=summary[0]["equity"],
                initial_cash=account["initial_cash"],
            )
    AuditRequestHandler.store.seed_demo()
    # 监听地址可由 HOST 覆盖(默认 127.0.0.1 只本机;打包/局域网共享时可设 0.0.0.0)。
    host = os.environ.get("HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), AuditRequestHandler)
    print(f"{APP_NAME} v{__version__} (api v{API_VERSION})")
    print(f"  数据目录: {paths.home()}")
    print(f"  运行于:   http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
