"""个股代码 → 行业(申万一级口径)解析(静态内置表 + 本地缓存 + 数据源实时取)。

优先级:本地缓存(数据源实时取过的,最权威)> 内置静态表(常见股,高置信)> "未分类"。
缓存落在 PAPER_TRADING_HOME 下,跟随用户数据走。静态表只放高置信的;拿不准的留给数据源补。
"""

from __future__ import annotations

import json
from typing import Any

from backend import paths

UNCLASSIFIED = "未分类"

# 内置静态表:申万一级行业(高置信)。拿不准的不放,等数据源(ricequant/wind)实时补到缓存。
STATIC_INDUSTRIES: dict[str, str] = {
    # 指数自身不归行业
    # 沪市
    "600519.SH": "食品饮料", "600036.SH": "银行", "601318.SH": "非银金融",
    "600000.SH": "银行", "600276.SH": "医药生物", "600030.SH": "非银金融",
    "601398.SH": "银行", "601988.SH": "银行", "601857.SH": "石油石化",
    "600028.SH": "石油石化", "600887.SH": "食品饮料", "601166.SH": "银行",
    "600585.SH": "建筑材料", "600031.SH": "机械设备", "601012.SH": "电力设备",
    "600104.SH": "汽车", "601628.SH": "非银金融", "601288.SH": "银行",
    "601888.SH": "社会服务", "603288.SH": "食品饮料", "601668.SH": "建筑装饰",
    "601899.SH": "有色金属", "600900.SH": "公用事业", "603259.SH": "医药生物",
    "688981.SH": "电子", "688111.SH": "计算机",
    # 深市
    "000001.SZ": "银行", "000002.SZ": "房地产", "000858.SZ": "食品饮料",
    "002594.SZ": "汽车", "300750.SZ": "电力设备", "000333.SZ": "家用电器",
    "000651.SZ": "家用电器", "000725.SZ": "电子", "002415.SZ": "电子",
    "000568.SZ": "食品饮料", "002304.SZ": "食品饮料", "002475.SZ": "电子",
    "300059.SZ": "非银金融", "002714.SZ": "农林牧渔", "300760.SZ": "医药生物",
    "002142.SZ": "银行", "000063.SZ": "通信",
    # 用户近期交易过的中小盘(best-effort,实时数据优先;拿不准的用相近一级行业)
    "002849.SZ": "机械设备", "300259.SZ": "机械设备", "688628.SH": "电子",
    "688686.SH": "机械设备", "300066.SZ": "机械设备", "603929.SH": "建筑装饰",
    "000509.SZ": "综合",
}

_cache: dict[str, str] | None = None


def _normalize(symbol: str) -> str:
    return str(symbol).strip().upper()


def _load_cache() -> dict[str, str]:
    global _cache
    if _cache is None:
        path = paths.security_industries_path()
        try:
            _cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            _cache = {}
    return _cache


def resolve(symbol: str) -> str:
    """返回行业:缓存 > 静态表 > 未分类(永不抛错)。"""
    sym = _normalize(symbol)
    cache = _load_cache()
    return cache.get(sym) or STATIC_INDUSTRIES.get(sym) or UNCLASSIFIED


def update(mapping: dict[str, str]) -> int:
    """把数据源取到的行业合并进缓存并持久化。返回新写入/更新的条数。"""
    cache = _load_cache()
    changed = 0
    for symbol, industry in mapping.items():
        sym = _normalize(symbol)
        clean = str(industry).strip()
        if clean and clean != UNCLASSIFIED and cache.get(sym) != clean:
            cache[sym] = clean
            changed += 1
    if changed:
        try:
            paths.security_industries_path().write_text(
                json.dumps(cache, ensure_ascii=False, sort_keys=True, indent=0), encoding="utf-8"
            )
        except OSError:
            pass
    return changed
