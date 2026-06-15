"""从账本重建账户每日净值(NAV)曲线。

与"seed 合成路径"不同:本引擎逐日重放真实成交 + 现金流,逐日盯市,并给闲置现金
计提国债逆回购利息,得到**从首笔成交那天开始**的真实净值曲线。

口径(刻意做成非循环,便于测试与解释):
    equity(日) = 交易现金 + 累计逆回购利息 + 持仓市值
  - 交易现金 = 初始资金 + 截至当日的账本现金流(剔除 sleeve 内部划转、旧 repo 事件)
  - 逆回购利息 = 当日闲置(=交易)现金 × 年化 × (到下个交易日的天数)/365,逐日累加
    (忽略"利息再生息"的二阶项,日利率极小、影响可忽略;换来无循环、可复算)
  - 持仓市值 = Σ 持仓数量 × 当日收盘(缺当日则用最近一个已知收盘,再退化为成交价)

只用截至当前日的数据,无前视。交易日按工作日(周一~周五)近似(无节假日历)。
"""

from __future__ import annotations

import bisect
from datetime import date, datetime, timedelta
from typing import Any


def _as_date(value: Any) -> date:
    s = str(value)[:10]
    return datetime.strptime(s, "%Y-%m-%d").date()


def _trading_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:  # 周一~周五
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def reconstruct(
    *,
    initial_cash: float,
    fills: list[dict[str, Any]],
    cash_flows: list[dict[str, Any]],
    daily_closes: dict[str, dict[str, float]],
    repo_annual_rate: float,
    today: str,
    repo_enabled: bool = True,
) -> dict[str, Any]:
    """重建净值曲线 + 逆回购逐日计划。

    fills:       [{timestamp, symbol, side(BUY/SELL), quantity, price}]
    cash_flows:  [{timestamp, amount}]  已剔除内部划转的带符号真实现金变动
    daily_closes:{symbol: {"YYYY-MM-DD": close}}
    today:       "YYYY-MM-DD"(曲线终点)
    返回 {curve, repo_schedule, start_date}。无成交时 curve 为空。
    """
    if not fills:
        return {"curve": [], "repo_schedule": [], "start_date": None}

    end = _as_date(today)
    start = min(_as_date(f["timestamp"]) for f in fills)
    if start > end:
        end = start
    days = _trading_days(start, end)
    if not days:
        days = [start]

    # 按日期分桶
    fills_by_day: dict[date, list[dict[str, Any]]] = {}
    for f in fills:
        fills_by_day.setdefault(_as_date(f["timestamp"]), []).append(f)
    cash_by_day: dict[date, float] = {}
    for c in cash_flows:
        cash_by_day[_as_date(c["timestamp"])] = cash_by_day.get(_as_date(c["timestamp"]), 0.0) + float(c["amount"])

    # 每标的:有序收盘序列(用于"当日或最近一个早于它的收盘")+ 成交价兜底
    close_dates: dict[str, list[str]] = {}
    close_vals: dict[str, list[float]] = {}
    for symbol, series in daily_closes.items():
        items = sorted((d, float(px)) for d, px in series.items())
        close_dates[symbol] = [d for d, _ in items]
        close_vals[symbol] = [px for _, px in items]
    fallback_px: dict[str, float] = {}
    for f in fills:
        fallback_px[str(f["symbol"]).upper()] = float(f["price"])

    def close_on(symbol: str, day_str: str) -> float:
        dates = close_dates.get(symbol)
        if dates:
            idx = bisect.bisect_right(dates, day_str) - 1
            if idx >= 0:
                return close_vals[symbol][idx]
        return fallback_px.get(symbol, 0.0)

    positions: dict[str, int] = {}
    trading_cash = float(initial_cash)
    cum_interest = 0.0
    curve: list[dict[str, Any]] = []
    repo_schedule: list[dict[str, Any]] = []

    for i, day in enumerate(days):
        # 1) 当日成交 → 持仓
        for f in fills_by_day.get(day, []):
            sym = str(f["symbol"]).upper()
            qty = int(f["quantity"])
            positions[sym] = positions.get(sym, 0) + (qty if str(f["side"]).upper() == "BUY" else -qty)
            if positions[sym] <= 0:
                positions.pop(sym, None)
        # 2) 当日现金流(成交本金+费用等)
        trading_cash = round(trading_cash + cash_by_day.get(day, 0.0), 2)

        # 3) 闲置现金计提逆回购(到下个交易日的天数)
        day_str = day.isoformat()
        if repo_enabled and trading_cash > 0 and repo_annual_rate > 0:
            gap = (days[i + 1] - day).days if i + 1 < len(days) else 1
            interest = round(trading_cash * repo_annual_rate * gap / 365, 2)
            cum_interest = round(cum_interest + interest, 2)
            repo_schedule.append({
                "trade_date": day_str,
                "principal": trading_cash,
                "interest": interest,
                "annual_rate": repo_annual_rate,
                "timestamp": f"{day_str}T14:30:00+08:00",
            })

        # 4) 盯市 → 权益
        market_value = round(sum(qty * close_on(sym, day_str) for sym, qty in positions.items()), 2)
        equity = round(trading_cash + cum_interest + market_value, 2)
        curve.append({
            "time": day_str,
            "equity": equity,
            "cash": round(trading_cash + cum_interest, 2),
            "market_value": market_value,
        })

    return {"curve": curve, "repo_schedule": repo_schedule, "start_date": start.isoformat()}
