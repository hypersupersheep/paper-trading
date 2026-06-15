from __future__ import annotations

import math
import random
import sqlite3
import statistics
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TRADING_DAYS = 252  # A 股年化口径


class PerformanceStore:
    """每日 NAV(净值) 快照 + 绩效指标(tearsheet)。

    净值序列来源:
      1. seed 时生成一段演示用日频历史(让 tearsheet 首屏就有曲线);
      2. scheduler tick 完成时自动追加快照(实盘 loop 自然积累曲线);
      3. 手动 POST /api/portfolio/snapshot。
    指标全部基于净值日收益序列计算,口径在 README 标注。
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nav_snapshots (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    equity REAL NOT NULL,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    pnl REAL NOT NULL,
                    pnl_pct REAL NOT NULL,
                    source TEXT NOT NULL,
                    UNIQUE(account_id, trade_date)
                )
                """
            )

    def record_snapshot(
        self,
        account_id: str,
        *,
        equity: float,
        cash: float,
        market_value: float,
        pnl: float,
        pnl_pct: float,
        timestamp: str | None = None,
        trade_date: str | None = None,
        source: str = "manual",
    ) -> dict[str, Any]:
        now = timestamp or datetime.now(timezone.utc).isoformat()
        day = trade_date or now[:10]
        # 同一交易日只留最新一笔(UNIQUE 冲突即覆盖),避免一天多点把曲线打毛。
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO nav_snapshots (
                    id, account_id, trade_date, timestamp, equity, cash,
                    market_value, pnl, pnl_pct, source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, trade_date) DO UPDATE SET
                    timestamp = excluded.timestamp,
                    equity = excluded.equity,
                    cash = excluded.cash,
                    market_value = excluded.market_value,
                    pnl = excluded.pnl,
                    pnl_pct = excluded.pnl_pct,
                    source = excluded.source
                """,
                (
                    f"nav_{uuid.uuid4().hex[:12]}",
                    account_id,
                    day,
                    now,
                    round(equity, 2),
                    round(cash, 2),
                    round(market_value, 2),
                    round(pnl, 2),
                    round(pnl_pct, 6),
                    source,
                ),
            )
        return {"account_id": account_id, "trade_date": day, "equity": round(equity, 2)}

    def list_snapshots(self, account_id: str, limit: int = 750) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM nav_snapshots WHERE account_id = ? ORDER BY trade_date ASC LIMIT ?",
                (account_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def seed_demo(self, account_id: str, *, equity_now: float, initial_cash: float, days: int = 90) -> None:
        if self.list_snapshots(account_id):
            return
        # 生成 days 个交易日的净值路径:首点=初始资金,末点=当前权益,中间带噪声与一段回撤。
        # days 个点之间有 days-1 段日收益;令日收益之和精确等于 log(equity_now/initial)。
        trade_dates = _recent_trading_days(days)
        n_ret = max(days - 1, 1)
        rng = random.Random(f"nav-{account_id}")
        target_log = math.log(max(equity_now, 1.0) / max(initial_cash, 1.0))
        noise = [rng.gauss(0, 0.011) for _ in range(n_ret)]
        # 注入一段回撤(约 60%~68% 处连续走弱),让曲线像真实策略。
        for i in range(int(n_ret * 0.60), int(n_ret * 0.68)):
            noise[i] -= 0.018
        mean_noise = sum(noise) / len(noise)
        drift = target_log / n_ret
        log_returns = [drift + (value - mean_noise) for value in noise]

        def _store(day: str, equity: float) -> None:
            self.record_snapshot(
                account_id,
                equity=equity,
                cash=equity * 0.4,
                market_value=equity * 0.6,
                pnl=equity - initial_cash,
                pnl_pct=(equity / initial_cash - 1) if initial_cash else 0.0,
                timestamp=f"{day}T15:00:00+08:00",
                trade_date=day,
                source="seed",
            )

        equity = initial_cash
        _store(trade_dates[0], equity)
        for index in range(n_ret):
            equity = equity * math.exp(log_returns[index])
            _store(trade_dates[index + 1], equity)

    def compute_metrics(self, account_id: str, initial_cash: float) -> dict[str, Any]:
        snaps = self.list_snapshots(account_id)
        curve = [{"time": snap["trade_date"], "equity": snap["equity"], "pnl": snap["pnl"]} for snap in snaps]
        return metrics_from_curve(curve, initial_cash)


    def compute_benchmark(self, curve: list[dict[str, Any]], bench_bars: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
        return benchmark_overlay(curve, bench_bars, symbol)


def benchmark_overlay(curve: list[dict[str, Any]], bench_bars: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    """把基准(如沪深300)日线对齐到净值序列日期,归一化叠加,并算相对指标。

    对齐口径:每个净值日期取该日(或最近一个早于它的)基准收盘,归一化到净值起点。
    相对指标:超额收益、Beta、年化 Alpha、信息比率、跑赢基准天数占比、相关系数。
    """
    if len(curve) < 3 or not bench_bars:
        return None
    bench = sorted(
        ((str(bar["timestamp"])[:10], float(bar["close"])) for bar in bench_bars if bar.get("close")),
        key=lambda item: item[0],
    )
    if len(bench) < 2:
        return None
    import bisect

    dates = [item[0] for item in bench]
    closes = [item[1] for item in bench]

    def bench_close_on(day: str) -> float | None:
        index = bisect.bisect_right(dates, day) - 1
        return closes[index] if index >= 0 else None

    aligned = [(point["time"], point["equity"], bench_close_on(point["time"])) for point in curve]
    aligned = [item for item in aligned if item[2] is not None]
    if len(aligned) < 3:
        return None

    start_equity = aligned[0][1]
    base_bench = aligned[0][2]
    series = [
        {"time": day, "value": round(start_equity * bench_px / base_bench, 2)}
        for day, _, bench_px in aligned
    ]

    strat_eq = [item[1] for item in aligned]
    bench_px = [item[2] for item in aligned]
    r_s = [strat_eq[i] / strat_eq[i - 1] - 1 for i in range(1, len(strat_eq)) if strat_eq[i - 1]]
    r_b = [bench_px[i] / bench_px[i - 1] - 1 for i in range(1, len(bench_px)) if bench_px[i - 1]]
    size = min(len(r_s), len(r_b))
    r_s, r_b = r_s[:size], r_b[:size]
    if size < 2:
        return {"symbol": symbol, "series": series, "metrics": {}}

    strat_cum = strat_eq[-1] / strat_eq[0] - 1
    bench_cum = bench_px[-1] / bench_px[0] - 1
    mean_s, mean_b = statistics.fmean(r_s), statistics.fmean(r_b)
    var_b = statistics.pvariance(r_b)
    cov = sum((a - mean_s) * (b - mean_b) for a, b in zip(r_s, r_b)) / size
    beta = cov / var_b if var_b > 0 else 0.0
    alpha_ann = (mean_s - beta * mean_b) * TRADING_DAYS
    std_s, std_b = statistics.pstdev(r_s), statistics.pstdev(r_b)
    corr = cov / (std_s * std_b) if std_s > 0 and std_b > 0 else 0.0
    diff = [a - b for a, b in zip(r_s, r_b)]
    std_diff = statistics.stdev(diff) if len(diff) >= 2 else 0.0
    info_ratio = (statistics.fmean(diff) / std_diff) * math.sqrt(TRADING_DAYS) if std_diff > 0 else 0.0
    win_vs_bench = sum(1 for a, b in zip(r_s, r_b) if a > b) / size

    return {
        "symbol": symbol,
        "series": series,
        "metrics": {
            "benchmark_cumulative": round(bench_cum, 6),
            "excess_return": round(strat_cum - bench_cum, 6),
            "beta": round(beta, 3),
            "alpha_annualized": round(alpha_ann, 6),
            "information_ratio": round(info_ratio, 3),
            "correlation": round(corr, 3),
            "win_vs_benchmark": round(win_vs_bench, 4),
        },
    }


def metrics_from_curve(curve: list[dict[str, Any]], initial_cash: float) -> dict[str, Any]:
    """从净值曲线([{time, equity, ...}]) 算全套绩效指标,并就地补 drawdown 字段。

    净值快照与回测引擎共用同一套口径:252 交易日年化,无风险利率取 0。
    """
    peak = -math.inf
    for point in curve:
        peak = max(peak, point["equity"])
        point["drawdown"] = round(point["equity"] / peak - 1, 6) if peak > 0 else 0.0

    if len(curve) < 3:
        return {"points": len(curve), "curve": curve, "metrics": _empty_metrics()}

    equities = [point["equity"] for point in curve]
    returns = [equities[i] / equities[i - 1] - 1 for i in range(1, len(equities)) if equities[i - 1]]
    periods = len(returns)
    cumulative = equities[-1] / equities[0] - 1
    years = periods / TRADING_DAYS
    annualized = (equities[-1] / equities[0]) ** (1 / years) - 1 if years > 0 and equities[0] > 0 else 0.0
    mean_r = statistics.fmean(returns)
    std_r = statistics.stdev(returns) if periods >= 2 else 0.0
    ann_vol = std_r * math.sqrt(TRADING_DAYS)
    sharpe = (mean_r / std_r) * math.sqrt(TRADING_DAYS) if std_r > 0 else 0.0
    max_drawdown = min(point["drawdown"] for point in curve)
    calmar = annualized / abs(max_drawdown) if max_drawdown < 0 else 0.0
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    win_rate = len(wins) / (len(wins) + len(losses)) if (wins or losses) else 0.0
    profit_loss_ratio = statistics.fmean(wins) / abs(statistics.fmean(losses)) if wins and losses else 0.0

    return {
        "points": len(curve),
        "curve": curve,
        "metrics": {
            "cumulative_return": round(cumulative, 6),
            "annualized_return": round(annualized, 6),
            "annualized_volatility": round(ann_vol, 6),
            "sharpe": round(sharpe, 3),
            "max_drawdown": round(max_drawdown, 6),
            "calmar": round(calmar, 3),
            "daily_win_rate": round(win_rate, 4),
            "profit_loss_ratio": round(profit_loss_ratio, 3),
            "trading_days": periods,
            "start_equity": round(equities[0], 2),
            "end_equity": round(equities[-1], 2),
            "initial_cash": round(initial_cash, 2),
        },
    }


def _empty_metrics() -> dict[str, Any]:
    return {
        "cumulative_return": 0.0,
        "annualized_return": 0.0,
        "annualized_volatility": 0.0,
        "sharpe": 0.0,
        "max_drawdown": 0.0,
        "calmar": 0.0,
        "daily_win_rate": 0.0,
        "profit_loss_ratio": 0.0,
        "trading_days": 0,
    }


def _recent_trading_days(count: int, *, end: date | None = None) -> list[str]:
    """返回最近 count 个工作日(跳过周末)的日期字符串,升序。"""
    end = end or datetime.now(timezone.utc).date()
    days: list[str] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return list(reversed(days))
