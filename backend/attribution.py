"""业绩归因:Brinson-Fachler 行业归因(纯函数,便于单测)。

把组合相对基准的超额收益拆成三块:
- 配置效应(allocation):超配/低配某行业带来的贡献 =(wP-wB)(rB_i - rB_total)
- 选股效应(selection):在行业内选股优于基准 = wB·(rP_i - rB_i)
- 交互效应(interaction):配置×选股的交叉项 =(wP-wB)(rP_i - rB_i)
三者之和 = 组合收益 - 基准收益(代数恒等,可对账)。

口径:持仓口径(holdings-based)单期 Brinson——用期末持仓行业权重 + 区间价格收益,
不追踪期内调仓(Wind AMS 的"持仓归因"同此假设)。多期几何连接留待后续。
"""

from __future__ import annotations

from typing import Any


def brinson_fachler(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """rows: [{sector, wP, wB, rP, rB}](权重已各自归一到和=1)。返回逐行效应 + 汇总。"""
    rb_total = sum(r["wB"] * r["rB"] for r in rows)
    rp_total = sum(r["wP"] * r["rP"] for r in rows)
    sectors: list[dict[str, Any]] = []
    alloc_sum = sel_sum = inter_sum = 0.0
    for r in rows:
        wp, wb, rp, rb = r["wP"], r["wB"], r["rP"], r["rB"]
        allocation = (wp - wb) * (rb - rb_total)
        selection = wb * (rp - rb)
        interaction = (wp - wb) * (rp - rb)
        alloc_sum += allocation
        sel_sum += selection
        inter_sum += interaction
        sectors.append(
            {
                "sector": r["sector"],
                "portfolio_weight": round(wp, 6),
                "benchmark_weight": round(wb, 6),
                "portfolio_return": round(rp, 6),
                "benchmark_return": round(rb, 6),
                "allocation": round(allocation, 6),
                "selection": round(selection, 6),
                "interaction": round(interaction, 6),
                "total": round(allocation + selection + interaction, 6),
            }
        )
    sectors.sort(key=lambda s: s["total"], reverse=True)
    return {
        "sectors": sectors,
        "allocation": round(alloc_sum, 6),
        "selection": round(sel_sum, 6),
        "interaction": round(inter_sum, 6),
        "total_excess": round(rp_total - rb_total, 6),
        "portfolio_return": round(rp_total, 6),
        "benchmark_return": round(rb_total, 6),
    }


def build_brinson_rows(
    *,
    holdings: dict[str, dict[str, float]],
    bench_weights: dict[str, float],
    industries: dict[str, str],
    returns: dict[str, float],
    unclassified: str = "未分类",
) -> list[dict[str, Any]]:
    """把"持仓 + 基准权重 + 行业 + 区间收益"汇总成逐行业的 {sector,wP,wB,rP,rB}。

    holdings: {symbol: {"weight": 组合权重(已归一到投资部分和=1)}}
    bench_weights: {symbol: 基准权重}; industries: {symbol: 行业}; returns: {symbol: 区间收益}
    """
    sectors = set()
    port: dict[str, dict[str, float]] = {}
    bench: dict[str, dict[str, float]] = {}

    for sym, info in holdings.items():
        sec = industries.get(sym, unclassified)
        sectors.add(sec)
        w = float(info["weight"])
        r = float(returns.get(sym, 0.0))
        b = port.setdefault(sec, {"w": 0.0, "wr": 0.0})
        b["w"] += w
        b["wr"] += w * r

    for sym, w in bench_weights.items():
        sec = industries.get(sym, unclassified)
        sectors.add(sec)
        w = float(w)
        r = float(returns.get(sym, 0.0))
        b = bench.setdefault(sec, {"w": 0.0, "wr": 0.0})
        b["w"] += w
        b["wr"] += w * r

    rows: list[dict[str, Any]] = []
    for sec in sorted(sectors):
        p = port.get(sec, {"w": 0.0, "wr": 0.0})
        b = bench.get(sec, {"w": 0.0, "wr": 0.0})
        rp = p["wr"] / p["w"] if p["w"] > 0 else 0.0
        rb = b["wr"] / b["w"] if b["w"] > 0 else 0.0
        rows.append({"sector": sec, "wP": p["w"], "wB": b["w"], "rP": rp, "rB": rb})
    return rows
