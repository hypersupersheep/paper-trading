from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.connector_settings import get_connector_settings, mask_secret


FREQUENCY_ALIASES = {
    "1d": "1d",
    "day": "1d",
    "daily": "1d",
    "5m": "5m",
    "5min": "5m",
    "1m": "1m",
    "1min": "1m",
}

TDX_FREQUENCIES = {
    "5m": 0,
    "1m": 8,
    "1d": 9,
}


class FixtureDataConnector:
    """Small deterministic connector used until live TongDaXin/RiceQuant adapters are wired.

    这个 connector 不冒充真实数据源；它只提供稳定 5m bar，保证策略 runner、
    broker 和 audit chain 可以端到端运行。
    """

    def healthcheck(self) -> dict[str, Any]:
        return {
            "name": "fixture",
            "status": "ok",
            "supported_frequencies": self.supported_frequencies(),
        }

    def supported_frequencies(self) -> list[str]:
        return ["5m", "1m", "1d"]

    def get_bars(
        self,
        symbols: list[str],
        frequency: str = "5m",
        limit: int = 8,
        start: Any = None,
        end: Any = None,
    ) -> list[dict[str, Any]]:
        if frequency not in self.supported_frequencies():
            raise ValueError(f"unsupported fixture frequency: {frequency}")
        # 日线单独走"真实交易日 + 带波动的合成路径"(便于和 NAV 对齐做基准叠加,且日收益有真实方差)。
        if frequency == "1d":
            return self._daily_bars(symbols, limit, start, end)
        minute_start = datetime(2026, 6, 10, 9, 30, tzinfo=timezone.utc)
        bars: list[dict[str, Any]] = []
        for symbol_index, symbol in enumerate(symbols):
            base = _base_price(symbol) + symbol_index * 0.4
            for index in range(limit):
                drift = index * 0.18
                open_price = round(base + drift, 2)
                close_price = round(open_price + (0.12 if index % 2 == 0 else -0.04), 2)
                bars.append(
                    {
                        "symbol": symbol,
                        "timestamp": (minute_start + timedelta(minutes=5 * index)).isoformat(),
                        "frequency": frequency,
                        "open": open_price,
                        "high": round(max(open_price, close_price) + 0.08, 2),
                        "low": round(min(open_price, close_price) - 0.07, 2),
                        "close": close_price,
                        "volume": 100_000 + index * 2_500,
                        "amount": round((100_000 + index * 2_500) * close_price, 2),
                    }
                )
        return sorted(bars, key=lambda item: (item["timestamp"], item["symbol"]))

    def _daily_bars(self, symbols: list[str], limit: int, start: Any = None, end: Any = None) -> list[dict[str, Any]]:
        import math

        # 给了区间就按 [start, end] 的工作日生成,否则取最近 limit 个工作日。
        days = _business_days_between(start, end, limit) if (start or end) else _recent_business_days(limit)
        bars: list[dict[str, Any]] = []
        for symbol_index, symbol in enumerate(symbols):
            price = _base_price(symbol) + symbol_index * 0.4
            for index, day in enumerate(days):
                # 确定性"随机游走":正弦叠加,日波动约 1.3%,带轻微上行 drift。
                ret = 0.0006 + 0.013 * math.sin(index * 1.7 + symbol_index * 0.9) + 0.006 * math.sin(index * 0.55)
                prev = price
                price = round(prev * (1 + ret), 2)
                open_price = round(prev, 2)
                close_price = price
                volume = 1_000_000 + index * 15_000
                bars.append(
                    {
                        "symbol": symbol,
                        "timestamp": datetime(day.year, day.month, day.day, 7, 0, tzinfo=timezone.utc).isoformat(),
                        "frequency": "1d",
                        "open": open_price,
                        "high": round(max(open_price, close_price) * 1.004, 2),
                        "low": round(min(open_price, close_price) * 0.996, 2),
                        "close": close_price,
                        "volume": volume,
                        "amount": round(volume * close_price, 2),
                    }
                )
        return sorted(bars, key=lambda item: (item["timestamp"], item["symbol"]))


class TongDaXinDataConnector:
    """Online TongDaXin HQ connector backed by optional mootdx.

    `mootdx` is intentionally optional so the app can run without network/data
    dependencies. When selected, failures are explicit and auditable.
    """

    def __init__(self, home_dir: str | Path | None = None):
        self.home_dir = Path(home_dir) if home_dir else Path(tempfile.gettempdir()) / "paper-trading-tdx-home"
        # 会话级名称目录:{market_int: {code6: name}},首次拉取后缓存,避免每次取名都拉全市场。
        self._name_catalog: dict[int, dict[str, str]] = {}

    # 类级共享:探测到的可用 HQ 服务器 (ip, port),所有 registry 实例复用,避免各自重探。
    _server: tuple[str, int] | None = None

    def _probe_working_server(self, quotes_module: Any) -> tuple[str, int] | None:
        """挨个试内置 HQ 服务器,**用一次真实取数验证**(不是 mootdx 那种只比 ping——快但可能一取数就
        reset 的服务器会被 ping 选中),返回第一台真能返数的 (ip, port)。
        背景:mootdx 默认/缓存常落到死节点(110.41.147.114 端口通但返空)或 ping 快却 reset 的服务器,
        新机器(同事)/过期缓存都会连不上。逐台真实验证最稳。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        try:
            from mootdx.consts import CONFIG
            hosts = [(str(e[1]), int(e[2])) for e in CONFIG.get("SERVER", {}).get("HQ", []) if len(e) >= 3]
        except Exception:  # noqa: BLE001
            return None
        hosts = hosts[:16]
        if not hosts:
            return None

        def _try(host: tuple[str, int]) -> tuple[str, int] | None:
            ip, port = host
            client = None
            try:
                client = quotes_module.Quotes.factory(market="std", server=[ip, port], bestip=False, timeout=4)
                frame = client.bars(symbol="000001", frequency=9, start=0, offset=1)
                return host if (frame is not None and len(frame) > 0) else None
            except Exception:  # noqa: BLE001
                return None
            finally:
                if client is not None and callable(getattr(client, "close", None)):
                    client.close()

        # 并发验证:一拿到第一台真能返数的就返回,不等慢服务器(它们各自 timeout 后自行结束)。
        ex = ThreadPoolExecutor(max_workers=len(hosts))
        try:
            futures = [ex.submit(_try, h) for h in hosts]
            for fut in as_completed(futures):
                res = fut.result()
                if res:
                    return res
            return None
        finally:
            ex.shutdown(wait=False)

    def _factory(self, quotes_module: Any):
        """建指向**已验证可用服务器**的 mootdx 客户端。首次(或上次失效后)真实探测一台会返数的,
        缓存到类级 _server 复用;探测不到才退回 mootdx 自己的 bestip(总比不给强)。"""
        if TongDaXinDataConnector._server is None:
            TongDaXinDataConnector._server = self._probe_working_server(quotes_module)
        if TongDaXinDataConnector._server is not None:
            ip, port = TongDaXinDataConnector._server
            return quotes_module.Quotes.factory(market="std", server=[ip, port], bestip=False, timeout=10)
        return quotes_module.Quotes.factory(market="std", bestip=True, timeout=10)

    def get_names(self, symbols: list[str]) -> dict[str, str]:
        """用 mootdx 的全市场证券表取个股中文名(SH=market 1, SZ=market 0)。失败返回空,绝不抛错。"""
        if not symbols:
            return {}
        try:
            quotes_module = self._import_mootdx()
            client = self._factory(quotes_module)
            result: dict[str, str] = {}
            for symbol in symbols:
                code, suffix = _split_symbol(symbol)
                market = 1 if suffix == "SH" else 0
                catalog = self._market_catalog(client, market)
                name = catalog.get(code)
                if name:
                    result[f"{code}.{suffix}"] = name
            return result
        except Exception:
            return {}

    def _market_catalog(self, client: Any, market: int) -> dict[str, str]:
        if market not in self._name_catalog:
            frame = client.stocks(market=market)
            catalog: dict[str, str] = {}
            if hasattr(frame, "to_dict"):
                for rec in frame[["code", "name"]].to_dict("records"):
                    catalog[str(rec["code"])] = str(rec["name"]).strip()
            self._name_catalog[market] = catalog
        return self._name_catalog[market]

    def healthcheck(self) -> dict[str, Any]:
        try:
            self._import_mootdx()
            return {
                "name": "tongdaxin",
                "status": "ok",
                "supported_frequencies": self.supported_frequencies(),
                "home_dir": str(self.home_dir),
            }
        except Exception as exc:  # noqa: BLE001 - health endpoint should expose connector failure.
            return {
                "name": "tongdaxin",
                "status": "unavailable",
                "supported_frequencies": self.supported_frequencies(),
                "error": str(exc),
                "install": "python3 -m pip install mootdx pandas",
            }

    def supported_frequencies(self) -> list[str]:
        return ["5m", "1m", "1d"]

    def get_bars(
        self,
        symbols: list[str],
        frequency: str = "5m",
        limit: int = 8,
        start: Any = None,
        end: Any = None,
    ) -> list[dict[str, Any]]:
        normalized_frequency = normalize_frequency(frequency)
        if normalized_frequency not in self.supported_frequencies():
            raise ValueError(f"TongDaXin connector does not support frequency: {frequency}")
        self._prepare_home()
        quotes_module = self._import_mootdx()
        # 服务器可能抽风(连接重置/返空);失败就强制重新 bestip 探测换一台,重试一次。
        last_err: Exception | None = None
        for _attempt in range(2):
            client = None
            try:
                client = self._factory(quotes_module)
                return self._fetch_bars(client, symbols, normalized_frequency, limit, start, end)
            except Exception as exc:  # noqa: BLE001 - 换服务器重试一次
                last_err = exc
                TongDaXinDataConnector._server = None  # 当前服务器失效,下次 _factory 重新探测换一台
            finally:
                if client is not None:
                    close = getattr(client, "close", None)
                    if callable(close):
                        close()
        raise last_err if last_err else ValueError("TongDaXin returned no bars")

    def _fetch_bars(self, client: Any, symbols: list[str], normalized_frequency: str, limit: int, start: Any, end: Any) -> list[dict[str, Any]]:
        tdx_frequency = _mootdx_frequency(normalized_frequency)
        start_date = _parse_date(start)
        end_date = _parse_date(end)
        bars: list[dict[str, Any]] = []
        for symbol in symbols:
            raw_symbol = _tdx_symbol(symbol)
            if start_date is not None:
                # 给了历史区间:向更早翻页直到覆盖 start_date(mootdx 的 start 是从最新往回的偏移)。
                records = self._paged_records(client, raw_symbol, tdx_frequency, start_date)
            else:
                frame = client.bars(symbol=raw_symbol, frequency=tdx_frequency, start=0, offset=limit)
                records = _frame_records(frame)[-limit:]
            if not records:
                raise ValueError(f"TongDaXin returned no bars for {symbol}")
            for row in records:
                bar = _normalize_tdx_row(symbol, normalized_frequency, row)
                day = bar["timestamp"][:10]
                if start_date and day < start_date.isoformat():
                    continue
                if end_date and day > end_date.isoformat():
                    continue
                bars.append(bar)
        return sorted(bars, key=lambda item: (item["timestamp"], item["symbol"]))

    @staticmethod
    def _paged_records(client: Any, raw_symbol: str, tdx_frequency: int, start_date: Any) -> list[dict[str, Any]]:
        """向更早翻页累积 K 线,直到覆盖 start_date 或翻到尽头(最多 16 页)。"""
        page = 800
        collected: list[dict[str, Any]] = []
        for index in range(16):
            try:
                frame = client.bars(symbol=raw_symbol, frequency=tdx_frequency, start=index * page, offset=page)
            except Exception:  # noqa: BLE001 - 翻页失败就用已取到的部分。
                break
            records = _frame_records(frame)
            if not records:
                break
            collected = records + collected  # 越翻越早,拼到前面
            earliest = min((str(_normalize_tdx_row("X", "1d", row)["timestamp"])[:10] for row in records), default="")
            if earliest and earliest <= start_date.isoformat():
                break
            if len(records) < page:
                break
        return collected

    def _prepare_home(self) -> None:
        self.home_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("TONGDAXIN_HOME", str(self.home_dir))
        os.environ.setdefault("HOME", str(self.home_dir))

    @staticmethod
    def _import_mootdx():
        try:
            from mootdx.quotes import Quotes  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("mootdx is not installed") from exc
        return type("MootdxQuotesModule", (), {"Quotes": Quotes})


class RiceQuantDataConnector:
    """RiceQuant(米筐) rqdatac connector。

    用户只需在数据源页保存 license key；init、symbol 转换、K 线规范化都在这里完成。
    rqdatac 是可选依赖：没装或没配置密钥时 health 给出明确状态，不影响核心 app。
    """

    def __init__(self, settings_path: Path | None = None):
        self.settings_path = settings_path
        # init 成功后缓存对应的 key：换 key 要重新 init，同 key 不重复连。
        self._inited_key: str | None = None

    def _license_key(self) -> str | None:
        settings = get_connector_settings("ricequant", self.settings_path)
        key = settings.get("license_key")
        return str(key) if key else None

    def get_names(self, symbols: list[str]) -> dict[str, str]:
        """用 rqdatac.instruments 取个股中文名。失败/未配置时返回空,绝不抛错。"""
        key = self._license_key()
        if not key or not symbols:
            return {}
        try:
            rqdatac = self._import_rqdatac()
            if self._inited_key != key:
                rqdatac.init("license", key)
                self._inited_key = key
            info = rqdatac.instruments([_rq_symbol(s) for s in symbols])
            items = info if isinstance(info, list) else [info]
            result: dict[str, str] = {}
            for inst in items:
                obid = getattr(inst, "order_book_id", None)
                name = getattr(inst, "symbol", None)
                if obid and name:
                    result[_from_rq_symbol(str(obid))] = str(name)
            return result
        except Exception:
            return {}

    def _ensure_init(self):
        """确保 rqdatac 已用当前 license 初始化;未配置返回 None。"""
        key = self._license_key()
        if not key:
            return None
        rqdatac = self._import_rqdatac()
        if self._inited_key != key:
            rqdatac.init("license", key)
            self._inited_key = key
        return rqdatac

    def get_industries(self, symbols: list[str]) -> dict[str, str]:
        """申万一级行业:{symbol: 行业名}。失败/未配置返回空,绝不抛错。"""
        if not symbols:
            return {}
        try:
            rqdatac = self._ensure_init()
            if rqdatac is None:
                return {}
            frame = rqdatac.shenwan_instrument_industry([_rq_symbol(s) for s in symbols])
            out: dict[str, str] = {}
            if hasattr(frame, "reset_index"):
                for rec in frame.reset_index().to_dict("records"):
                    obid = rec.get("order_book_id") or rec.get("index")
                    name = rec.get("index_name")
                    if obid and name:
                        out[_from_rq_symbol(str(obid))] = str(name)
            return out
        except Exception:
            return {}

    def index_weights(self, index_symbol: str, date: str | None = None) -> dict[str, float]:
        """指数成分股权重:{symbol: weight}(权重和≈1)。失败/未配置返回空。"""
        try:
            rqdatac = self._ensure_init()
            if rqdatac is None:
                return {}
            series = rqdatac.index_weights(_rq_symbol(index_symbol), date=date)
            out: dict[str, float] = {}
            if hasattr(series, "items"):
                for obid, weight in series.items():
                    out[_from_rq_symbol(str(obid))] = float(weight)
            return out
        except Exception:
            return {}

    def supported_frequencies(self) -> list[str]:
        return ["5m", "1m", "1d"]

    def healthcheck(self) -> dict[str, Any]:
        base = {"name": "ricequant", "supported_frequencies": self.supported_frequencies()}
        key = self._license_key()
        if not key:
            return {
                **base,
                "status": "not_configured",
                "hint": "在数据源页输入米筐 license key 即可启用",
            }
        try:
            self._import_rqdatac()
        except Exception as exc:  # noqa: BLE001
            return {
                **base,
                "status": "unavailable",
                "license_key_masked": mask_secret(key),
                "error": str(exc),
                "install": ".venv/bin/pip install rqdatac",
            }
        # 不在 health 里做网络验证(页面会频繁轮询)；连接状态以最近一次成功 init 为准。
        verified = self._inited_key == key
        return {
            **base,
            "status": "ok" if verified else "configured",
            "license_key_masked": mask_secret(key),
            "hint": None if verified else "已保存密钥，首次拉取行情或点「保存并测试」时验证连接",
        }

    def get_bars(
        self,
        symbols: list[str],
        frequency: str = "5m",
        limit: int = 8,
        start: Any = None,
        end: Any = None,
    ) -> list[dict[str, Any]]:
        normalized = normalize_frequency(frequency)
        if normalized not in self.supported_frequencies():
            raise ValueError(f"RiceQuant connector does not support frequency: {frequency}")
        key = self._license_key()
        if not key:
            raise ValueError("RiceQuant 未配置: 请在数据源页保存 license key")
        rqdatac = self._import_rqdatac()
        if self._inited_key != key:
            rqdatac.init("license", key)
            self._inited_key = key

        order_book_ids = [_rq_symbol(symbol) for symbol in symbols]
        # rqdatac 原生支持任意历史 date range:给了区间就直接按区间拉(历史回测的正解)。
        explicit_range = bool(start)
        end_date = _parse_date(end) or date.today()
        start_date = _parse_date(start)
        if start_date is None:
            lookback_days = max(limit * 2 + 10, 15) if normalized == "1d" else max(limit // 40 + 10, 15)
            start_date = end_date - timedelta(days=lookback_days)
        frame = rqdatac.get_price(
            order_book_ids,
            start_date=start_date,
            end_date=end_date,
            frequency=normalized,
            fields=["open", "high", "low", "close", "volume", "total_turnover"],
            adjust_type="none",
            expect_df=True,
        )
        if frame is None or len(frame) == 0:
            raise ValueError(f"RiceQuant returned no bars for {symbols}")

        bars: list[dict[str, Any]] = []
        per_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in frame.reset_index().to_dict(orient="records"):
            symbol = _from_rq_symbol(str(row.get("order_book_id") or order_book_ids[0]))
            per_symbol.setdefault(symbol, []).append(_normalize_rq_row(symbol, normalized, row))
        for symbol, rows in per_symbol.items():
            rows.sort(key=lambda item: item["timestamp"])
            # 显式区间:返回整段;否则 tail(limit) 取最近 N 根。
            bars.extend(rows if explicit_range else rows[-limit:])
        return sorted(bars, key=lambda item: (item["timestamp"], item["symbol"]))

    @staticmethod
    def _import_rqdatac():
        try:
            import rqdatac  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("rqdatac is not installed") from exc
        return rqdatac


class WindDataConnector:
    """辉隆私募提供的 Wind 只读残血库(MySQL `wind_data`)。

    只有日频落地数据(无分钟/tick/盘口),所以仅支持 1d。股票走 ASHAREEODPRICES,
    指数走 AINDEXEODPRICES,统一按 TRADE_DT 区间查询(EOD 大表无 code 索引,必须用日期范围约束)。
    连接需通过 OpenVPN 进内网;凭证存 gitignored 的 connector_settings.json,不进源码/仓库。
    """

    EOD_FIELDS = "S_INFO_WINDCODE, TRADE_DT, S_DQ_OPEN, S_DQ_HIGH, S_DQ_LOW, S_DQ_CLOSE, S_DQ_VOLUME, S_DQ_AMOUNT"
    TABLES = ("ASHAREEODPRICES", "AINDEXEODPRICES")

    def __init__(self, settings_path: Path | None = None):
        self.settings_path = settings_path

    def _config(self) -> dict[str, Any]:
        return get_connector_settings("wind", self.settings_path)

    def supported_frequencies(self) -> list[str]:
        return ["1d"]  # 残血库只有日频

    def healthcheck(self) -> dict[str, Any]:
        base = {"name": "wind", "supported_frequencies": self.supported_frequencies()}
        config = self._config()
        if not config.get("host"):
            return {**base, "status": "not_configured", "hint": "在数据源页填写 Wind 连接信息(需先连内网 VPN)"}
        try:
            self._import_pymysql()
        except Exception as exc:  # noqa: BLE001
            return {**base, "status": "unavailable", "error": str(exc), "install": ".venv/bin/pip install pymysql"}
        # 不在 health 里真连(VPN/网络慢会卡页面);连接状态以最近一次成功取数为准。
        return {
            **base,
            "status": "configured",
            "endpoint": f"{config.get('host')}:{config.get('port', 3306)}/{config.get('database', 'wind_data')}",
            # 非敏感字段回传供前端预填表单(密码不回传)。
            "host": config.get("host"),
            "port": config.get("port", 3306),
            "user": config.get("user"),
            "database": config.get("database", "wind_data"),
            "password_set": bool(config.get("password")),
            "hint": "已保存连接信息;需连内网 VPN 后才能取数。日频(1d)专用,无分钟/tick。",
        }

    def get_bars(
        self,
        symbols: list[str],
        frequency: str = "1d",
        limit: int = 8,
        start: Any = None,
        end: Any = None,
    ) -> list[dict[str, Any]]:
        if normalize_frequency(frequency) != "1d":
            raise ValueError("Wind 残血库仅提供日频(1d)数据,无分钟/tick;请把频率设为 1d 或换数据源")
        config = self._config()
        if not config.get("host"):
            raise ValueError("Wind 未配置: 请在数据源页填写连接信息(host/user/password 等)")
        pymysql = self._import_pymysql()
        end_dt = _parse_date(end) or datetime.now(timezone.utc).date()
        explicit_range = bool(start)
        start_dt = _parse_date(start) or (end_dt - timedelta(days=max(limit * 2 + 20, 40)))
        codes = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
        if not codes:
            return []

        conn = pymysql.connect(
            host=config["host"],
            port=int(config.get("port", 3306)),
            user=config.get("user", ""),
            password=config.get("password", ""),
            database=config.get("database", "wind_data"),
            charset="utf8mb4",
            connect_timeout=6,
            read_timeout=30,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )
        placeholders = ",".join(["%s"] * len(codes))
        rows: list[dict[str, Any]] = []
        try:
            with conn.cursor() as cur:
                cur.execute("SET SESSION MAX_EXECUTION_TIME=15000")
                for table in self.TABLES:
                    sql = (
                        f"SELECT {self.EOD_FIELDS} FROM {table} "
                        f"WHERE TRADE_DT BETWEEN %s AND %s AND S_INFO_WINDCODE IN ({placeholders}) "
                        "ORDER BY TRADE_DT"
                    )
                    cur.execute(sql, [start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d"), *codes])
                    rows.extend(cur.fetchall())
        finally:
            conn.close()
        if not rows:
            raise ValueError(f"Wind 未返回数据(代码 {codes},区间 {start_dt}~{end_dt});确认已连 VPN、代码与区间正确")

        per_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            bar = _normalize_wind_row(row)
            per_symbol.setdefault(bar["symbol"], []).append(bar)
        bars: list[dict[str, Any]] = []
        for series in per_symbol.values():
            series.sort(key=lambda item: item["timestamp"])
            bars.extend(series if explicit_range else series[-limit:])
        return sorted(bars, key=lambda item: (item["timestamp"], item["symbol"]))

    @staticmethod
    def _import_pymysql():
        try:
            import pymysql  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("pymysql is not installed") from exc
        return pymysql


class IFinDDataConnector:
    """iFinD(同花顺)HTTP API connector(辉隆内部,Mac 原生,纯 HTTPS 无需 SDK/VM)。

    鉴权:用户在数据源页保存 refresh_token(长期密钥)→ 换 access_token(缓存 6 天,过期/错误自动重取)。
    日线走 cmd_history_quotation(**CPS=1 不复权**,与 ricequant/wind 一致,保证历史日线缓存的不可变假设成立);
    分钟走 high_frequency。全程 stdlib urllib,不引入 requests,保持打包 app 自包含。
    试用账号有两条硬边界:历史仅近 1 年(-4309)、EDB 受限;转正式解除。取数失败原样把 iFinD 错误码/信息带出。
    """

    BASE = "https://quantapi.51ifind.com/api/v1"
    _INDICATORS = "open,high,low,close,volume,amount"

    def __init__(self, settings_path: Path | None = None):
        self.settings_path = settings_path
        self._access_token: str | None = None
        self._access_exp: float = 0.0
        self._token_for: str | None = None  # 换了 refresh_token 就作废 access_token

    def _refresh_token(self) -> str | None:
        settings = get_connector_settings("ifind", self.settings_path)
        tok = settings.get("refresh_token")
        return str(tok).strip() if tok else None

    def supported_frequencies(self) -> list[str]:
        return ["1d", "5m", "1m"]

    def _http_post(self, endpoint: str, headers: dict[str, str], payload: dict | None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else b""
        req = urllib.request.Request(f"{self.BASE}/{endpoint}", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        # 显式无代理 opener:打包 app 里 urllib 走系统代理探测可能异常,直连 API 更稳;并把 HTTP 错误体带出。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "ignore")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise ValueError(f"iFinD {endpoint} HTTP {exc.code}: {body or exc.reason}") from exc

    def _get_access_token(self, force: bool = False) -> str:
        rt = self._refresh_token()
        if not rt:
            raise ValueError("iFinD 未配置: 请在数据源页保存 refresh_token")
        if not force and self._access_token and self._token_for == rt and self._access_exp > time.time() + 3600:
            return self._access_token
        j = self._http_post("get_access_token", {"refresh_token": rt}, None)
        tok = (j.get("data") or {}).get("access_token")
        if not tok:
            raise ValueError(f"iFinD get_access_token 失败: {str(j.get('errmsg') or j)[:200]}")
        self._access_token, self._token_for, self._access_exp = tok, rt, time.time() + 6 * 86400
        return tok

    def _call(self, endpoint: str, payload: dict) -> dict:
        """带 access_token 调数据端点;HTTP 层失败(如缓存 token 失效返 401)或 JSON 层 token 错误,
        都强制换新 access_token 重试一次。"""
        tok = self._get_access_token()
        try:
            j = self._http_post(endpoint, {"access_token": tok}, payload)
        except ValueError:
            tok = self._get_access_token(force=True)  # 换新 token 重试一次
            j = self._http_post(endpoint, {"access_token": tok}, payload)
        ec = j.get("errorcode")
        if ec not in (0, None) and ("token" in str(j.get("errmsg", "")).lower() or ec in (-1001, -1010, -1011)):
            tok = self._get_access_token(force=True)
            j = self._http_post(endpoint, {"access_token": tok}, payload)
        return j

    def healthcheck(self) -> dict[str, Any]:
        base = {"name": "ifind", "supported_frequencies": self.supported_frequencies()}
        rt = self._refresh_token()
        if not rt:
            return {**base, "status": "not_configured", "hint": "在数据源页保存 iFinD refresh_token(超级命令→工具→refresh_token 查询)即可启用"}
        return {
            **base,
            "status": "ok" if (self._access_token and self._token_for == rt) else "configured",
            "refresh_token_masked": mask_secret(rt),
            "hint": "已保存 refresh_token;首次取数或「保存并测试」时换 access_token 验证。试用账号历史仅近 1 年。",
        }

    def get_bars(
        self,
        symbols: list[str],
        frequency: str = "1d",
        limit: int = 8,
        start: Any = None,
        end: Any = None,
    ) -> list[dict[str, Any]]:
        normalized = normalize_frequency(frequency)
        if normalized not in self.supported_frequencies():
            raise ValueError(f"iFinD connector does not support frequency: {frequency}")
        codes = ",".join(str(s).upper() for s in symbols if str(s).strip())
        if not codes:
            return []
        explicit_range = bool(start)
        if normalized == "1d":
            end_date = _parse_date(end) or datetime.now(timezone.utc).date()
            start_date = _parse_date(start) or (end_date - timedelta(days=max(limit * 2 + 10, 15)))
            payload = {
                "codes": codes,
                "indicators": self._INDICATORS,
                "startdate": start_date.isoformat(),
                "enddate": end_date.isoformat(),
                # CPS=1 不复权:与 ricequant(adjust_type=none)/wind(S_DQ_CLOSE)一致,历史日线才可被不可变缓存。
                "functionpara": {"Interval": "D", "CPS": "1", "Currency": "RMB", "Fill": "Blank"},
            }
            j = self._call("cmd_history_quotation", payload)
        else:
            minutes = "1" if normalized == "1m" else "5"
            now_cn = datetime.now(timezone(timedelta(hours=8)))
            end_dt = _parse_date(end)
            end_str = f"{end_dt.isoformat()} 16:00:00" if end_dt else now_cn.strftime("%Y-%m-%d %H:%M:%S")
            start_dt = _parse_date(start)
            start_str = f"{start_dt.isoformat()} 09:00:00" if start_dt else (now_cn - timedelta(days=7)).strftime("%Y-%m-%d 09:00:00")
            payload = {
                "codes": codes,
                "indicators": self._INDICATORS,
                "starttime": start_str,
                "endtime": end_str,
                "functionpara": {"Interval": minutes, "Fill": "Original"},
            }
            j = self._call("high_frequency", payload)

        parsed = _bars_from_ifind(j, normalized)
        if not parsed:
            raise ValueError(f"iFinD returned no bars for {symbols} (errorcode={j.get('errorcode')}, {j.get('errmsg')})")
        per_symbol: dict[str, list[dict[str, Any]]] = {}
        for bar in parsed:
            per_symbol.setdefault(bar["symbol"], []).append(bar)
        bars: list[dict[str, Any]] = []
        for _sym, rows in per_symbol.items():
            rows.sort(key=lambda item: item["timestamp"])
            bars.extend(rows if explicit_range else rows[-limit:])
        return sorted(bars, key=lambda item: (item["timestamp"], item["symbol"]))


class DataConnectorRegistry:
    def __init__(self):
        self._connectors = {
            "fixture": FixtureDataConnector(),
            "tongdaxin": TongDaXinDataConnector(),
            "ricequant": RiceQuantDataConnector(),
            "wind": WindDataConnector(),
            "ifind": IFinDDataConnector(),
        }

    def names(self) -> list[str]:
        return list(self._connectors.keys())

    def get(self, name: str | None):
        # 未指定时回退到全局默认数据源(用户在数据源页设的;代码默认 tongdaxin)。
        if not name:
            from backend import app_settings

            name = app_settings.default_data_source()
        connector_name = str(name).lower()
        if connector_name not in self._connectors:
            raise ValueError(f"unknown data source: {connector_name}")
        return self._connectors[connector_name]

    def health(self) -> list[dict[str, Any]]:
        results = []
        for connector in self._connectors.values():
            started = time.perf_counter()
            item = connector.healthcheck()
            item["checked_in_ms"] = round((time.perf_counter() - started) * 1000, 1)
            item["checked_at"] = datetime.now(timezone.utc).isoformat()
            results.append(item)
        return results


def normalize_frequency(frequency: str) -> str:
    normalized = FREQUENCY_ALIASES.get(str(frequency).lower())
    if not normalized:
        raise ValueError(f"unsupported frequency: {frequency}")
    return normalized


def _recent_business_days(count: int):
    """最近 count 个工作日(跳过周末),升序;用于 fixture 日线时间轴。"""
    end = datetime.now(timezone.utc).date()
    days: list = []
    cursor = end
    while len(days) < max(count, 1):
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def _parse_date(value: Any):
    """把 'YYYY-MM-DD' 或 date 解析成 date;无法解析返回 None。"""
    if value in (None, ""):
        return None
    if hasattr(value, "year") and hasattr(value, "month"):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _business_days_between(start: Any, end: Any, fallback_count: int):
    """[start, end] 内的工作日(升序);start 缺省时取末端往前 fallback_count 个工作日。"""
    end_date = _parse_date(end) or datetime.now(timezone.utc).date()
    start_date = _parse_date(start)
    if start_date is None:
        days: list = []
        cursor = end_date
        while len(days) < max(fallback_count, 1):
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor -= timedelta(days=1)
        return list(reversed(days))
    days = []
    cursor = start_date
    # 上限保护:避免极端区间生成过多点。
    while cursor <= end_date and len(days) < 8000:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _base_price(symbol: str) -> float:
    from backend import repo

    if repo.is_repo_symbol(symbol):
        return 1.8  # 国债逆回购:close=年化利率%,fixture 给 ~1.8% 档(带日内小波动)
    if symbol.startswith("600519"):
        return 1725.0
    if symbol.startswith("000858"):
        return 126.0
    if symbol.startswith("000300"):  # 沪深300 指数
        return 3800.0
    if symbol.startswith("000001"):
        return 10.0
    if symbol.startswith("000002"):
        return 12.0
    return 20.0


def _tdx_symbol(symbol: str) -> str:
    code = symbol.upper().split(".")[0]
    return code


def _split_symbol(symbol: str) -> tuple[str, str]:
    """600229.SH → ("600229", "SH");没带后缀按 6 开头沪市、其余深市推断。"""
    code, _, market = str(symbol).upper().partition(".")
    if market not in {"SH", "SZ"}:
        market = "SH" if code.startswith("6") else "SZ"
    return code, market


def _rq_symbol(symbol: str) -> str:
    """000001.SZ → 000001.XSHE, 600519.SH → 600519.XSHG(米筐 order_book_id 格式)。"""
    code, _, market = symbol.upper().partition(".")
    if market in {"SZ", "XSHE"}:
        return f"{code}.XSHE"
    if market in {"SH", "XSHG"}:
        return f"{code}.XSHG"
    if not market:
        # 没带市场后缀时按 A 股惯例推断: 6 开头沪市, 其余深市。
        return f"{code}.XSHG" if code.startswith("6") else f"{code}.XSHE"
    raise ValueError(f"unsupported symbol for RiceQuant: {symbol}")


def _from_rq_symbol(order_book_id: str) -> str:
    code, _, market = order_book_id.upper().partition(".")
    if market == "XSHE":
        return f"{code}.SZ"
    if market == "XSHG":
        return f"{code}.SH"
    return order_book_id


def _ifind_ts(value: Any) -> str:
    """iFinD time → ISO。日线 '2026-06-30' → '2026-06-30T00:00:00';分钟 '2026-06-30 09:35:00' → 'T' 连接。"""
    s = str(value or "").strip()
    if not s:
        return s
    if len(s) == 10:  # 纯日期
        return f"{s}T00:00:00"
    return s.replace(" ", "T", 1)


def _bars_from_ifind(j: dict, frequency: str) -> list[dict[str, Any]]:
    """iFinD 标准响应 tables:[{thscode,time:[..],table:{ind:[..]}}] → 标准 bar 列表。errorcode!=0 抛清晰错误。"""
    ec = j.get("errorcode")
    if ec not in (0, None):
        raise ValueError(f"iFinD 返回错误 {ec}: {j.get('errmsg')}")

    def at(seq: Any, i: int):
        return seq[i] if isinstance(seq, list) and i < len(seq) else None

    bars: list[dict[str, Any]] = []
    for tb in (j.get("tables") or []):
        code = str(tb.get("thscode") or "").upper()
        times = tb.get("time") or []
        tab = tb.get("table") or {}
        for i in range(len(times)):
            close = _float(at(tab.get("close"), i))
            volume = _float(at(tab.get("volume"), i))
            bars.append({
                "symbol": code,
                "timestamp": _ifind_ts(times[i]),
                "frequency": frequency,
                "open": _float(at(tab.get("open"), i)),
                "high": _float(at(tab.get("high"), i)),
                "low": _float(at(tab.get("low"), i)),
                "close": close,
                "volume": volume,
                "amount": _float(at(tab.get("amount"), i) or close * volume),
            })
    return bars


def _normalize_wind_row(row: dict[str, Any]) -> dict[str, Any]:
    """Wind EOD 行 → 标准 bar。TRADE_DT 是 'YYYYMMDD' 字符串,转成 ISO 日期。"""
    trade_dt = str(row.get("TRADE_DT") or "")
    timestamp = f"{trade_dt[:4]}-{trade_dt[4:6]}-{trade_dt[6:8]}T00:00:00" if len(trade_dt) >= 8 else trade_dt
    close = _float(row.get("S_DQ_CLOSE"))
    volume = _float(row.get("S_DQ_VOLUME"))
    return {
        "symbol": str(row.get("S_INFO_WINDCODE") or "").upper(),
        "timestamp": timestamp,
        "frequency": "1d",
        "open": _float(row.get("S_DQ_OPEN")),
        "high": _float(row.get("S_DQ_HIGH")),
        "low": _float(row.get("S_DQ_LOW")),
        "close": close,
        "volume": volume,
        "amount": _float(row.get("S_DQ_AMOUNT") or close * volume),
    }


def _normalize_rq_row(symbol: str, frequency: str, row: dict[str, Any]) -> dict[str, Any]:
    timestamp = row.get("datetime") or row.get("date") or row.get("trading_date")
    if hasattr(timestamp, "isoformat"):
        timestamp_value = timestamp.isoformat()
    else:
        timestamp_value = str(timestamp)
    volume = _float(row.get("volume"))
    close = _float(row.get("close"))
    return {
        "symbol": symbol,
        "timestamp": timestamp_value,
        "frequency": frequency,
        "open": _float(row.get("open")),
        "high": _float(row.get("high")),
        "low": _float(row.get("low")),
        "close": close,
        "volume": volume,
        "amount": _float(row.get("total_turnover") or close * volume),
    }


def _mootdx_frequency(frequency: str) -> int:
    return TDX_FREQUENCIES[frequency]


def _frame_records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    columns_attr = getattr(frame, "columns", [])
    columns = set(list(columns_attr))
    has_time_column = bool(columns.intersection({"datetime", "date", "time"}))
    if hasattr(frame, "reset_index") and not has_time_column:
        frame = frame.reset_index()
    if hasattr(frame, "to_dict"):
        return list(frame.to_dict(orient="records"))
    if isinstance(frame, list):
        return [dict(item) for item in frame]
    return []


def _normalize_tdx_row(symbol: str, frequency: str, row: dict[str, Any]) -> dict[str, Any]:
    timestamp = row.get("datetime") or row.get("date") or row.get("time") or row.get("index")
    if hasattr(timestamp, "isoformat"):
        timestamp_value = timestamp.isoformat()
    else:
        timestamp_value = str(timestamp)
    volume = _float(row.get("volume") or row.get("vol") or 0)
    close = _float(row.get("close"))
    return {
        "symbol": symbol,
        "timestamp": timestamp_value,
        "frequency": frequency,
        "open": _float(row.get("open")),
        "high": _float(row.get("high")),
        "low": _float(row.get("low")),
        "close": close,
        "volume": volume,
        "amount": _float(row.get("amount") or row.get("money") or close * volume),
    }


def _float(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    return round(float(value), 4)
