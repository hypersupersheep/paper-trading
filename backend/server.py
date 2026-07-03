from __future__ import annotations

import json
import mimetypes
import os
import threading
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from backend import admin_link
from backend import app_settings
from backend import events
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
        if not self._guard_remote():
            return
        if path == "/api/stream":
            self._stream()
            return

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
            if path == "/api/admin-link":
                self._json(admin_link.public_view())
                return
            if path == "/api/portfolio/summary":
                self._json(self._portfolio_summary(query))
                return
            if path == "/api/portfolio/performance":
                self._json(self._performance(query))
                return
            if path == "/api/portfolio/brinson":
                self._json(self._brinson(query))
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
            if path.startswith("/api/accounts/") and path.endswith("/description"):
                account_id = unquote(path.removeprefix("/api/accounts/").removesuffix("/description").strip("/"))
                self._json(self.trading.get_description(account_id))
                return
            if path.startswith("/api/accounts/") and "/files/" in path:
                # GET /api/accounts/{id}/files/{file_id} —— 下载文件原始字节(供 Admin 代理/浏览器查看)
                rest = path.removeprefix("/api/accounts/").strip("/")
                account_id, _, file_id = rest.partition("/files/")
                file = self.trading.get_file(unquote(account_id), unquote(file_id))
                if not file:
                    self._json({"error": "file not found"}, HTTPStatus.NOT_FOUND)
                    return
                from urllib.parse import quote as _q
                disp = f"inline; filename*=UTF-8''{_q(file['filename'])}"
                self._send(HTTPStatus.OK, file["content"], file["content_type"], {"Content-Disposition": disp})
                return
            if path.startswith("/api/accounts/") and path.endswith("/files"):
                account_id = unquote(path.removeprefix("/api/accounts/").removesuffix("/files").strip("/"))
                self._json({"files": self.trading.list_files(account_id)})
                return
            if path.startswith("/api/accounts/"):
                account_id = unquote(path.removeprefix("/api/accounts/").strip("/"))
                account = self.trading.get_account(account_id)
                if not account:
                    self._json({"error": f"unknown account_id: {account_id}"}, HTTPStatus.NOT_FOUND)
                    return
                account["positions"] = self.trading.list_positions(account_id)
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
                # 历史个股盈亏:当前仍持仓的标的归"现有持仓",不计入历史。
                held = set(self.trading.list_position_symbols(query.get("account_id")))
                board = self.store.realized_pnl_by_symbol(query, exclude_symbols=held)
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

    def _stream(self) -> None:
        """SSE 事件流:成交等事件即推,Admin 据此从轮询切事件驱动(成交秒级上墙)。

        走入站鉴权(do_GET 顶部 _guard_remote 已校验:远程须带 node.token)。每个连接独立线程
        (ThreadingHTTPServer),长连不阻塞别的请求。15s 心跳兼检测断连。
        """
        import queue

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = events.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    evt = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(evt, ensure_ascii=False)}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # 心跳,顺带探活
                self.wfile.flush()
        except Exception:  # noqa: BLE001 - 客户端断开/写失败 → 清理退出
            pass
        finally:
            events.unsubscribe(q)

    def _guard_remote(self) -> bool:
        """节点入站鉴权:本机放行;远程须带 X-Admin-Token=node_token。未过则 401。"""
        if admin_link.authorize(self.client_address[0], self.headers.get("X-Admin-Token")):
            return True
        self._json(
            {"error": "unauthorized: remote access requires X-Admin-Token (node admin-token)"},
            HTTPStatus.UNAUTHORIZED,
        )
        return False

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._guard_remote():
            return
        try:
            payload = self._read_json()
            if path == "/api/accounts":
                account = self.trading.create_account(payload)
                self._register_account_with_admin(account)  # 本地+远程开户都经此,登记到 Admin
                events.publish({"type": "account_created", "account_id": account["id"], "owner": account.get("owner")})
                self._json({"account": account}, HTTPStatus.CREATED)
                return
            if path == "/api/admin-link":
                admin_link.save(payload)
                self._json(admin_link.public_view())
                return
            if path == "/api/admin-link/register-all":
                self._json(self._register_all_accounts(), HTTPStatus.CREATED)
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
            if path.startswith("/api/accounts/") and path.endswith("/delete") and "/files/" not in path:
                account_id = unquote(path.removeprefix("/api/accounts/").removesuffix("/delete").strip("/"))
                result = self.trading.delete_account(account_id, payload)
                self._deregister_account_with_admin(account_id)  # 通知 Admin 注销该账户
                events.publish({"type": "account_deleted", "account_id": account_id})
                self._json(result, HTTPStatus.CREATED)
                return
            if path.startswith("/api/accounts/") and path.endswith("/update"):
                account_id = unquote(path.removeprefix("/api/accounts/").removesuffix("/update").strip("/"))
                account = self.trading.update_account(account_id, payload)
                self._register_account_with_admin(account)  # 改名/改 owner 后同步给 Admin(幂等)
                self._json({"account": account}, HTTPStatus.CREATED)
                return
            if path.startswith("/api/audit/trades/") and path.endswith("/void"):
                trade_event_id = unquote(path.removeprefix("/api/audit/trades/").removesuffix("/void").strip("/"))
                account_id = payload.get("account_id") or self._resolve_account_id(None)
                self._json(
                    self.trading.void_trade(account_id, trade_event_id, payload.get("reason", "")),
                    HTTPStatus.CREATED,
                )
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
            if path.startswith("/api/accounts/") and path.endswith("/description"):
                account_id = unquote(path.removeprefix("/api/accounts/").removesuffix("/description").strip("/"))
                self._json(self.trading.set_description(account_id, payload.get("description", "")), HTTPStatus.CREATED)
                return
            if path.startswith("/api/accounts/") and path.endswith("/files"):
                account_id = unquote(path.removeprefix("/api/accounts/").removesuffix("/files").strip("/"))
                import base64
                content = base64.b64decode(payload.get("content_base64") or "")
                meta = self.trading.add_file(account_id, payload.get("filename", ""), content)
                self._json({"file": meta}, HTTPStatus.CREATED)
                return
            if path.startswith("/api/accounts/") and "/files/" in path and path.endswith("/delete"):
                rest = path.removeprefix("/api/accounts/").removesuffix("/delete").strip("/")
                account_id, _, file_id = rest.partition("/files/")
                self._json(self.trading.delete_file(unquote(account_id), unquote(file_id)), HTTPStatus.CREATED)
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

    # ———————————————— Admin 账户级登记(opt-in) ————————————————
    def _server_port(self) -> int:
        try:
            return self.server.server_address[1]
        except Exception:  # noqa: BLE001
            return int(os.environ.get("PORT") or 8000)

    def _register_account_with_admin(self, account: dict[str, Any]) -> None:
        """开户/改配置成功后,把账户身份登记到 Admin(§3.1 单条)。未配置则跳过。

        best-effort 后台线程,Admin 不可达绝不影响本地开户。幂等键 (node.id, account.id)。
        """
        if not admin_link.is_enabled():
            return
        payload = {"node": admin_link.node_descriptor(self._server_port()), "account": admin_link.account_segment(account)}
        threading.Thread(target=admin_link.post, args=("/api/admin/accounts/register", payload), daemon=True).start()

    def _deregister_account_with_admin(self, account_id: str) -> None:
        """删账户成功后通知 Admin 注销(§3.3),立即从监控墙移除,不残留「最后已知」。"""
        if not admin_link.is_enabled():
            return
        threading.Thread(target=admin_link.post, args=(admin_link.deregister_path(account_id), {}), daemon=True).start()

    def _register_all_accounts(self) -> dict[str, Any]:
        """把本节点现有全部账户一次性批量登记到 Admin。同步执行并回传 Admin 真实响应,
        让用户一眼看到登记成功/失败原因(201/401/404/连不上),不再静默黑盒。"""
        if not admin_link.is_enabled():
            return {"registered": 0, "ok": False, "detail": "未配置 Admin 地址"}
        accounts = self.trading.list_accounts()
        port = self._server_port()
        ok, detail = admin_link.register_node_accounts(port, accounts)
        return {"registered": len(accounts), "ok": ok, "detail": detail, "node_id": admin_link.node_id()}

    def _refresh_industries(self, data_source: Any, symbols: list[str]) -> None:
        """best-effort:用数据源给未分类的标的取申万行业并缓存。连接器未提供 get_industries 则跳过。"""
        from backend import industries

        pending = sorted({s for s in symbols if s and industries.resolve(s) == industries.UNCLASSIFIED})
        if not pending:
            return
        try:
            connector = self.strategies.connectors.get(data_source)
            if not hasattr(connector, "get_industries"):
                return
            fetched = connector.get_industries(pending)
            if fetched:
                industries.update(fetched)
        except Exception:  # noqa: BLE001 - 取行业失败不影响主流程
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
        mark_error: str | None = None
        if symbols:
            # 盯市取价走数据源;数据源报错(如米筐 license 登录机器数超限、网络不通)绝不能让整个
            # 组合概览 500——降级为「无盯市价」→ 持仓按最后成交价显示,并在 metadata 记下原因。
            try:
                # 多拉几根 bar: 最新 close 做盯市价, 整段收益序列算 bar 级波动率。
                bars = connector.get_bars(symbols, frequency=frequency, limit=20)
                closes_by_symbol: dict[str, list[tuple[str, float]]] = {}
                for bar in bars:
                    symbol = str(bar["symbol"]).upper()
                    closes_by_symbol.setdefault(symbol, []).append((str(bar.get("timestamp")), float(bar["close"])))
                # 昨收(当日盈亏基线):取日线倒数第二根的收盘。best-effort,取不到则该标的当日盈亏记 0。
                prev_close = self._prev_close_map(connector, symbols)
                for symbol, series in closes_by_symbol.items():
                    series.sort(key=lambda item: item[0])
                    timestamp, price = series[-1]
                    mark_prices[symbol] = {
                        "price": price,
                        "timestamp": timestamp,
                        "data_source": data_source,
                        "frequency": frequency,
                        "volatility": _bar_volatility([close for _, close in series]),
                        "prev_close": prev_close.get(symbol),
                    }
            except Exception as exc:  # noqa: BLE001 - 数据源打嗝→降级用最后价,绝不 500
                mark_error = str(exc)
                mark_prices = {}
        try:
            connector_health = connector.healthcheck()
        except Exception:  # noqa: BLE001 - 健康探测也别拖垮请求
            connector_health = {"name": data_source, "status": "error"}
        summary = self.trading.get_portfolio_summary(
            account_id,
            mark_prices=mark_prices,
            mark_metadata={
                "mode": "position_last_price" if mark_error else "connector_close",
                "data_source": data_source,
                "frequency": frequency,
                "symbols": symbols,
                "marked_symbols": sorted(mark_prices),
                "connector": connector_health,
                "mark_error": mark_error,
            },
        )
        self._attach_day_pnl(summary)
        return summary

    def _attach_day_pnl(self, summary: dict) -> None:
        """当日盈亏 = 持仓今日浮盈变动(盯市价 vs 昨收) + 今日已实现(今日卖出结转)。"""
        today = self._today_cn()
        for account in summary.get("accounts", []):
            holdings = float(account.get("holdings_day_pnl") or 0.0)
            realized_today = 0.0
            for row in self.store.trade_summaries({"account_id": account["id"], "limit": 100000}):
                if (
                    row.get("kind") == "trade"
                    and not row.get("voided")
                    and row.get("realized_pnl") is not None
                    and str(row.get("timestamp"))[:10] == today
                ):
                    realized_today += float(row["realized_pnl"])
            account["day_realized_pnl"] = round(realized_today, 2)
            account["day_pnl"] = round(holdings + realized_today, 2)

    def _prev_close_map(self, connector: Any, symbols: list[str]) -> dict[str, float]:
        """各标的昨收(日线倒数第二根收盘)。取不到就缺省,不影响盯市。"""
        out: dict[str, float] = {}
        try:
            bars = connector.get_bars(symbols, frequency="1d", limit=2)
        except Exception:  # noqa: BLE001
            return out
        series: dict[str, list[tuple[str, float]]] = {}
        for bar in bars:
            sym = str(bar["symbol"]).upper()
            series.setdefault(sym, []).append((str(bar.get("timestamp")), float(bar["close"])))
        for sym, items in series.items():
            items.sort(key=lambda x: x[0])
            if len(items) >= 2:
                out[sym] = items[-2][1]
        return out

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
                "admin_link": True,
                "event_stream": True,
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
                "event_stream": "GET /api/stream (SSE; 远程需 X-Admin-Token=node.token)",
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
        voided = self.store.voided_trade_event_ids(account_id)
        fills = [
            {
                "timestamp": e["timestamp"],
                "symbol": e["symbol"],
                "side": (e.get("metadata") or {}).get("side", "BUY"),
                "quantity": e["quantity"],
                "price": e["price"],
            }
            for e in trade_events
            if e.get("symbol") and e.get("quantity") and e.get("price") and e["id"] not in voided
        ]
        cash_events = self.store.list_events({"ledger_type": "cash", "account_id": account_id, "limit": 100000})
        skip = {"sleeve_allocation", "sleeve_allocation_adjusted"}
        cash_flows = [
            {"timestamp": e["timestamp"], "amount": e["amount"]}
            for e in cash_events
            if e.get("amount") is not None
            and e["event_type"] not in skip
            and not str(e["event_type"]).startswith("reverse_repo")
            and (e.get("metadata") or {}).get("trade_event_id") not in voided
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

        # 业绩归因(个股盈亏贡献)+ 持仓分析(换手/集中度)。best-effort,失败不影响主绩效。
        try:
            result["attribution"] = self._contribution(account_id, mark_source, account["initial_cash"])
            result["holdings_analysis"] = self._holdings_analysis(account_id, mark_source, account["initial_cash"])
        except Exception:  # noqa: BLE001
            result["attribution"] = None
            result["holdings_analysis"] = None
        return result

    def _contribution(self, account_id: str, mark_source: str, initial_cash: float) -> dict:
        """个股盈亏贡献归因:每只票 已实现+浮动 盈亏 ÷ 初始资金 = 对总收益的贡献(百分点)。

        股票贡献之和 + 残差(现金/逆回购/费用)= 账户总盈亏,构成可对账的瀑布。
        """
        from backend import industries

        realized = self.store.realized_pnl_by_symbol({"account_id": account_id})
        rows: dict[str, dict] = {}
        for sym in realized["symbols"]:
            rows[sym["symbol"]] = {
                "symbol": sym["symbol"],
                "name": sym["name"],
                "realized_pnl": sym["realized_pnl"],
                "unrealized_pnl": 0.0,
                "fees": sym["fees"],
                "market_value": 0.0,
            }
        live = self._portfolio_summary({"account_id": account_id, "data_source": mark_source})
        account = live["accounts"][0] if live.get("accounts") else None
        self._refresh_industries(mark_source, list(rows.keys()) + [p["symbol"] for p in (account or {}).get("positions", [])])
        for pos in (account or {}).get("positions", []):
            row = rows.setdefault(
                pos["symbol"],
                {"symbol": pos["symbol"], "name": pos.get("name"), "realized_pnl": 0.0, "unrealized_pnl": 0.0, "fees": 0.0, "market_value": 0.0},
            )
            row["unrealized_pnl"] = round(row["unrealized_pnl"] + float(pos["unrealized_pnl"]), 2)
            row["market_value"] = round(row["market_value"] + float(pos["market_value"]), 2)
        equity = float(account["equity"]) if account else 0.0
        items = []
        stock_pnl_sum = 0.0
        sectors: dict[str, dict] = {}
        for row in rows.values():
            total = round(row["realized_pnl"] + row["unrealized_pnl"], 2)
            stock_pnl_sum += total
            sector = industries.resolve(row["symbol"])
            items.append({**row, "sector": sector, "total_pnl": total,
                          "contribution_pct": round(total / initial_cash, 6) if initial_cash else 0.0})
            bucket = sectors.setdefault(sector, {"sector": sector, "total_pnl": 0.0, "market_value": 0.0})
            bucket["total_pnl"] = round(bucket["total_pnl"] + total, 2)
            bucket["market_value"] = round(bucket["market_value"] + row["market_value"], 2)
        items.sort(key=lambda x: x["total_pnl"], reverse=True)
        by_sector = [
            {
                "sector": b["sector"],
                "total_pnl": b["total_pnl"],
                "contribution_pct": round(b["total_pnl"] / initial_cash, 6) if initial_cash else 0.0,
                "weight": round(b["market_value"] / equity, 6) if equity > 0 else 0.0,
            }
            for b in sectors.values()
        ]
        by_sector.sort(key=lambda x: x["total_pnl"], reverse=True)
        account_pnl = round(equity - initial_cash, 2) if account else round(stock_pnl_sum, 2)
        residual = round(account_pnl - stock_pnl_sum, 2)
        return {
            "symbols": items,
            "by_sector": by_sector,
            "stock_pnl_total": round(stock_pnl_sum, 2),
            "residual_pnl": residual,  # 现金/逆回购/费用等非个股部分
            "account_pnl": account_pnl,
            "initial_cash": round(initial_cash, 2),
        }

    def _holdings_analysis(self, account_id: str, mark_source: str, initial_cash: float) -> dict:
        """持仓分析:累计换手率 + 集中度(头号/前五权重、HHI、持仓数)。"""
        traded_notional = sum(
            float(t.get("gross_amount") or 0.0)
            for t in self.store.trade_summaries({"account_id": account_id, "limit": 100000})
            if t.get("kind") == "trade" and not t.get("voided")
        )
        turnover = round(traded_notional / initial_cash, 4) if initial_cash else 0.0
        live = self._portfolio_summary({"account_id": account_id, "data_source": mark_source})
        account = live["accounts"][0] if live.get("accounts") else None
        positions = (account or {}).get("positions", [])
        equity = float(account["equity"]) if account else 0.0
        weights = sorted(
            (float(p["market_value"]) / equity for p in positions if equity > 0),
            reverse=True,
        )
        hhi = round(sum(w * w for w in weights), 4)
        return {
            "turnover": turnover,
            "num_holdings": len(positions),
            "top_weight": round(weights[0], 4) if weights else 0.0,
            "top5_weight": round(sum(weights[:5]), 4),
            "hhi": hhi,
        }

    def _brinson(self, query: dict) -> dict:
        """Brinson-Fachler 行业归因(持仓口径单期)。需 ricequant(成分股权重+申万行业)。"""
        from backend import attribution
        from backend import industries as ind_mod

        account_id = self._resolve_account_id(query.get("account_id"))
        account = self.trading.get_account(account_id)
        if not account:
            raise ValueError(f"unknown account_id: {account_id}")
        data_source = query.get("data_source") or app_settings.default_data_source()
        connector = self.strategies.connectors.get(data_source)
        if not hasattr(connector, "index_weights") or not hasattr(connector, "get_industries"):
            return {"error": "Brinson 归因需要 ricequant 数据源(成分股权重 + 申万行业);请把数据源切到 ricequant 并填好 license。"}

        trades = self.store.list_events({"event_type": "trade_filled", "account_id": account_id, "limit": 100000})
        voided = self.store.voided_trade_event_ids(account_id)
        trade_dates = [str(t["timestamp"])[:10] for t in trades if t["id"] not in voided]
        if not trade_dates:
            return {"error": "暂无有效成交,无法归因。"}
        start, end = min(trade_dates), self._today_cn()

        summary = self._portfolio_summary({"account_id": account_id, "data_source": data_source})
        acct = summary["accounts"][0] if summary.get("accounts") else None
        positions = (acct or {}).get("positions", [])
        invested = sum(float(p["market_value"]) for p in positions)
        if invested <= 0:
            return {"error": "当前无持仓,持仓口径 Brinson 需要有在持标的。"}
        holdings = {p["symbol"]: {"weight": float(p["market_value"]) / invested} for p in positions}

        benchmark = (query.get("benchmark") or "000300.SH").upper()
        bench_weights = connector.index_weights(benchmark)
        if not bench_weights:
            return {"error": f"取不到 {benchmark} 的成分股权重(检查 ricequant 权限/日期)。"}

        union = sorted(set(holdings) | set(bench_weights))
        fetched_ind = connector.get_industries(union)
        ind_mod.update({s: v for s, v in fetched_ind.items() if v})  # 顺手把真实申万行业写进缓存
        industry_map = {s: (fetched_ind.get(s) or ind_mod.resolve(s)) for s in union}
        returns = self._window_returns(connector, union, start, end)

        rows = attribution.build_brinson_rows(
            holdings=holdings, bench_weights=bench_weights, industries=industry_map, returns=returns,
            unclassified=ind_mod.UNCLASSIFIED,
        )
        result = attribution.brinson_fachler(rows)
        result.update({
            "account_id": account_id,
            "benchmark": benchmark,
            "start_date": start,
            "end_date": end,
            "holdings_count": len(positions),
            "benchmark_count": len(bench_weights),
            "return_coverage": sum(1 for s in union if s in returns),
            "note": "持仓口径单期 Brinson-Fachler;现金未计入(组合权重归一到投资部分);Barra 风格归因需更高 license 档位。",
        })
        return result

    def _window_returns(self, connector: Any, symbols: list[str], start: str, end: str) -> dict[str, float]:
        """区间收益:每个标的 (末收盘/首收盘 - 1)。一次性批量取日线。"""
        out: dict[str, float] = {}
        try:
            bars = connector.get_bars(symbols, frequency="1d", limit=100000, start=start, end=end)
        except Exception:  # noqa: BLE001
            return out
        series: dict[str, list[tuple[str, float]]] = {}
        for bar in bars:
            close = float(bar.get("close") or 0)
            if close > 0:
                series.setdefault(str(bar["symbol"]).upper(), []).append((str(bar.get("timestamp")), close))
        for sym, items in series.items():
            items.sort(key=lambda x: x[0])
            if len(items) >= 2 and items[0][1] > 0:
                out[sym] = items[-1][1] / items[0][1] - 1
        return out

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
    # 监听地址:显式 HOST 环境变量最优先;否则——配了 Admin(admin_link.is_enabled())就自动绑
    # 0.0.0.0 让老板机能连上(远程已强制 node.token 鉴权,安全);纯本地未对接则仍只听 127.0.0.1。
    # 这样同事下载即用,无需手动配 HOST。
    host = admin_link.bind_host(os.environ.get("HOST"))
    # 绑定回退:对接 Admin 时默认监听 0.0.0.0(全网卡),但部分机器的防火墙/安全软件会拦截
    # 未签名 app 监听全网卡,bind() 直接报错 → app 永远起不来(卡"正在启动本地引擎")。
    # 这里捕获绑定失败并回退到只听 127.0.0.1,保证 app 一定能起;局域网/Admin 可达是次要的。
    try:
        server = ThreadingHTTPServer((host, port), AuditRequestHandler)
    except OSError as exc:
        if host == "127.0.0.1":
            raise
        fallback_note = (
            f"监听 {host}:{port} 失败({exc});已回退到只听 127.0.0.1。"
            f"原因通常是防火墙/安全软件拦截未签名 app 监听全部网卡。"
            f"老板机 Admin 若要轮询到本节点,需在系统里允许本 app 接受网络连接后重启。"
        )
        print(f"  [warn] {fallback_note}")
        try:
            (paths.home() / "startup_error.log").write_text(
                f"[{datetime.now(timezone.utc).isoformat()}] {fallback_note}\n", encoding="utf-8"
            )
        except Exception:  # noqa: BLE001 - 日志写不了不影响回退启动
            pass
        host = "127.0.0.1"
        server = ThreadingHTTPServer((host, port), AuditRequestHandler)
    print(f"{APP_NAME} v{__version__} (api v{API_VERSION})")
    print(f"  数据目录: {paths.home()}")
    bind_note = "局域网可达(已对接 Admin,远程需 node.token)" if host == "0.0.0.0" else "仅本机"
    print(f"  运行于:   http://{host}:{port}  · {bind_note}")
    # 配了 Admin 地址则启动补登一次(Admin 重启/重置 DB 也能自愈),不可达时重试几轮。best-effort。
    if admin_link.is_enabled():
        accounts = AuditRequestHandler.trading.list_accounts()
        threading.Thread(
            target=admin_link.register_node_accounts,
            args=(port, accounts),
            kwargs={"retries": 5, "delay": 4.0},
            daemon=True,
        ).start()
    server.serve_forever()


if __name__ == "__main__":
    run()
