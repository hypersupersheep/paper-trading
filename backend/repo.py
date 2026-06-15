"""国债逆回购:把"行情"映射成年化利率。

交易所国债逆回购(GC001 等)的报价 close 本身就是**年化利率(百分数)**,
例如 204001.SH 收 1.85 表示年化 1.85%。所以"按实时行情买逆回购"= 拉它最新 close、
换算成利率分数(close/100)用于计息;用户也可手填自定义利率。
"""

from __future__ import annotations

from typing import Any

# 常见交易所国债逆回购品种(供前端下拉;默认 GC001 一天期,最常用于隔夜闲置资金)。
INSTRUMENTS = [
    {"symbol": "204001.SH", "name": "GC001", "term_days": 1, "desc": "上证1天"},
    {"symbol": "204002.SH", "name": "GC002", "term_days": 2, "desc": "上证2天"},
    {"symbol": "204007.SH", "name": "GC007", "term_days": 7, "desc": "上证7天"},
    {"symbol": "131810.SZ", "name": "R-001", "term_days": 1, "desc": "深证1天"},
    {"symbol": "131801.SZ", "name": "R-007", "term_days": 7, "desc": "深证7天"},
]
DEFAULT_SYMBOL = "204001.SH"

_REPO_CODES = {item["symbol"].split(".")[0] for item in INSTRUMENTS} | {
    "204003", "204004", "204014", "204028", "204091", "204182",
    "131800", "131811", "131802", "131803", "131809",
}


def is_repo_symbol(symbol: str) -> bool:
    return str(symbol).upper().split(".")[0] in _REPO_CODES


def term_days(symbol: str) -> int:
    for item in INSTRUMENTS:
        if item["symbol"].upper() == str(symbol).upper():
            return int(item["term_days"])
    return 1


def name_of(symbol: str) -> str:
    for item in INSTRUMENTS:
        if item["symbol"].upper() == str(symbol).upper():
            return item["name"]
    return str(symbol).upper()


def rate_from_close(close: Any) -> float | None:
    """close(年化%)→ 利率分数;越界/异常返回 None。"""
    try:
        rate = float(close) / 100.0
    except (TypeError, ValueError):
        return None
    if rate <= 0 or rate > 0.30:  # 逆回购年化合理区间,过滤脏数据(如把价格当利率)
        return None
    return round(rate, 6)


def fetch_latest_rate(connector: Any, symbol: str) -> dict[str, Any] | None:
    """拉逆回购最新年化利率。失败返回 None(调用方回退到自定义/账户默认)。"""
    try:
        bars = connector.get_bars([symbol], frequency="1d", limit=1)
        candidates = [b for b in bars if str(b.get("symbol", "")).upper() == symbol.upper()]
        latest = max(candidates, key=lambda b: str(b.get("timestamp") or "")) if candidates else None
        if not latest:
            return None
        rate = rate_from_close(latest.get("close"))
        if rate is None:
            return None
        return {"symbol": symbol.upper(), "annual_rate": rate, "timestamp": latest.get("timestamp")}
    except Exception:
        return None


def fetch_daily_rates(connector: Any, symbol: str, start: Any = None, end: Any = None) -> dict[str, float]:
    """拉逆回购逐日年化利率 {YYYY-MM-DD: 利率分数},供 NAV 重建按日计息。失败返回 {}。"""
    try:
        bars = connector.get_bars([symbol], frequency="1d", limit=2000, start=start, end=end)
    except Exception:
        return {}
    out: dict[str, float] = {}
    for bar in bars:
        if str(bar.get("symbol", "")).upper() != symbol.upper():
            continue
        rate = rate_from_close(bar.get("close"))
        if rate is not None:
            out[str(bar.get("timestamp"))[:10]] = rate
    return out
