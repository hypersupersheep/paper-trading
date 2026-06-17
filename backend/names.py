"""个股代码 → 名称解析(静态内置表 + 本地缓存 + 数据源实时取名)。

优先级:本地缓存(数据源实时取过的)> 内置静态表(常见蓝筹/指数)> 退化为代码本身。
缓存落在 PAPER_TRADING_HOME 下,跟随用户数据走,实时取过的名字重启仍在。
"""

from __future__ import annotations

import json
from typing import Any

from backend import paths

# 内置静态表:常见 A 股蓝筹 + 主要指数(高置信、离线/fixture 也能显示)。
# 代码统一用 "600519.SH" / "000001.SZ" 形式(大写后缀)。
STATIC_NAMES: dict[str, str] = {
    # 指数
    "000300.SH": "沪深300", "000905.SH": "中证500", "000016.SH": "上证50",
    "000852.SH": "中证1000", "000001.SH": "上证指数", "399001.SZ": "深证成指",
    "399006.SZ": "创业板指", "399005.SZ": "中小100",
    # 沪市蓝筹
    "600519.SH": "贵州茅台", "600036.SH": "招商银行", "601318.SH": "中国平安",
    "600000.SH": "浦发银行", "600276.SH": "恒瑞医药", "600030.SH": "中信证券",
    "601398.SH": "工商银行", "601988.SH": "中国银行", "601857.SH": "中国石油",
    "600028.SH": "中国石化", "600887.SH": "伊利股份", "601166.SH": "兴业银行",
    "600585.SH": "海螺水泥", "600031.SH": "三一重工", "601012.SH": "隆基绿能",
    "600104.SH": "上汽集团", "601628.SH": "中国人寿", "601288.SH": "农业银行",
    "600009.SH": "上海机场", "600690.SH": "海尔智家", "600048.SH": "保利发展",
    "601888.SH": "中国中免", "603288.SH": "海天味业", "600196.SH": "复星医药",
    "601668.SH": "中国建筑", "600050.SH": "中国联通", "601728.SH": "中国电信",
    "601899.SH": "紫金矿业", "600406.SH": "国电南瑞", "600900.SH": "长江电力",
    "603259.SH": "药明康德", "688981.SH": "中芯国际", "688111.SH": "金山办公",
    # 深市蓝筹
    "000001.SZ": "平安银行", "000002.SZ": "万科A", "000858.SZ": "五粮液",
    "002594.SZ": "比亚迪", "300750.SZ": "宁德时代", "000333.SZ": "美的集团",
    "000651.SZ": "格力电器", "000725.SZ": "京东方A", "002415.SZ": "海康威视",
    "000568.SZ": "泸州老窖", "002304.SZ": "洋河股份", "002475.SZ": "立讯精密",
    "300059.SZ": "东方财富", "000776.SZ": "广发证券", "002714.SZ": "牧原股份",
    "300760.SZ": "迈瑞医疗", "002142.SZ": "宁波银行", "000063.SZ": "中兴通讯",
    # 用户近期交易过的中小盘(离线/无 VPN 也能显示名称;有数据源时仍以实时取名为准)
    "002849.SZ": "威星智能", "300259.SZ": "新天科技", "688628.SH": "优利德",
    "688686.SH": "奥普特", "300066.SZ": "三川智慧", "603929.SH": "亚翔集成",
    "000509.SZ": "华塑控股", "000083.SZ": "中国宝安",
}

_cache: dict[str, str] | None = None


def _normalize(symbol: str) -> str:
    return str(symbol).strip().upper()


def _load_cache() -> dict[str, str]:
    global _cache
    if _cache is None:
        path = paths.security_names_path()
        try:
            _cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            _cache = {}
    return _cache


def resolve(symbol: str) -> str:
    """返回名称:缓存 > 静态表 > 代码本身(永不抛错)。"""
    sym = _normalize(symbol)
    cache = _load_cache()
    return cache.get(sym) or STATIC_NAMES.get(sym) or sym


def resolve_many(symbols: list[str]) -> dict[str, str]:
    return {_normalize(s): resolve(s) for s in symbols}


def update(mapping: dict[str, str]) -> int:
    """把数据源实时取到的名称合并进缓存并持久化。返回新写入/更新的条数。"""
    cache = _load_cache()
    changed = 0
    for symbol, name in mapping.items():
        sym = _normalize(symbol)
        clean = str(name).strip()
        if clean and cache.get(sym) != clean:
            cache[sym] = clean
            changed += 1
    if changed:
        try:
            paths.security_names_path().write_text(
                json.dumps(cache, ensure_ascii=False, sort_keys=True, indent=0), encoding="utf-8"
            )
        except OSError:
            pass
    return changed
