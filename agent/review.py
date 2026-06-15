"""回测结果自审查分析器。

把一份回测结果翻译成 agent 可读、可行动的"体检报告":总评 + 红旗 + 逐项评估 + 改进建议。
目的:即使是通用 agent,也能拿到一套"quant 会怎么看这份回测"的结构化判断,从而主动迭代策略。
纯函数、零依赖、可单测。口径与系统的绩效指标一致(252 年化, rf=0)。
"""

from __future__ import annotations

from typing import Any


def review_backtest(result: dict[str, Any]) -> dict[str, Any]:
    """输入 /api/backtest/run 的返回,输出体检报告。"""
    metrics = result.get("metrics") or {}
    summary = result.get("summary") or {}
    benchmark = result.get("benchmark") or {}
    bench_metrics = benchmark.get("metrics") or {}

    flags: list[str] = []          # 红旗:必须正视的问题
    assessment: list[str] = []     # 逐项中性评估
    suggestions: list[str] = []    # 改进建议

    total_trades = int(summary.get("total_trades") or 0)
    closed_trades = int(summary.get("closed_trades") or 0)
    sharpe = _num(metrics.get("sharpe"))
    cumulative = _num(metrics.get("cumulative_return"))
    annualized = _num(metrics.get("annualized_return"))
    max_dd = _num(metrics.get("max_drawdown"))
    win_rate = _num(summary.get("trade_win_rate"))
    trading_days = int(metrics.get("trading_days") or 0)

    # --- 交易行为 --- #
    if total_trades == 0:
        flags.append("零成交:策略在该区间从未下单——下单条件没触发,或标的/区间无数据。")
        suggestions.append("检查 on_bar 的下单分支是否会被触发;确认标的代码、区间、数据源有数据。")
    elif closed_trades == 0:
        flags.append("只买不卖:没有平仓交易,已实现盈亏为 0,本质只测了「买入持有」。")
        suggestions.append("加入卖出/止盈止损逻辑,才能评估一个完整的交易闭环,而非单边持有。")
    else:
        assessment.append(f"成交 {total_trades} 笔、平仓 {closed_trades} 笔,交易闭环完整。")
        if trading_days and total_trades / trading_days > 0.6:
            flags.append(f"换手过高:平均每个交易日 {total_trades / trading_days:.1f} 笔,实盘摩擦会吃掉大量收益。")
            suggestions.append("降低交易频率或加入冷却期/信号过滤,减少无效换手。")

    # --- 风险调整后收益 --- #
    if sharpe is not None:
        if sharpe < 0:
            flags.append(f"夏普 {sharpe:.2f}:为负,风险调整后是亏损的,不具备配置价值。")
        elif sharpe < 0.5:
            assessment.append(f"夏普 {sharpe:.2f}:偏低,收益不足以补偿承担的波动。")
        elif sharpe < 1.0:
            assessment.append(f"夏普 {sharpe:.2f}:中等。")
        else:
            assessment.append(f"夏普 {sharpe:.2f}:良好。")

    # --- 回撤 --- #
    if max_dd is not None:
        if max_dd < -0.30:
            flags.append(f"最大回撤 {max_dd:.1%}:超过 30%,回撤过深,实盘很难拿得住。")
            suggestions.append("加入仓位管理或择时门控,在风险期降低暴露以控制回撤。")
        else:
            assessment.append(f"最大回撤 {max_dd:.1%}。")

    # --- 胜率 --- #
    if win_rate is not None and closed_trades > 0:
        if win_rate < 0.4:
            assessment.append(f"交易胜率 {win_rate:.1%}:偏低,需靠盈亏比取胜(确认大赢小亏)。")
        else:
            assessment.append(f"交易胜率 {win_rate:.1%}。")

    # --- 相对基准 --- #
    bench_symbol = benchmark.get("symbol", "基准")
    if bench_metrics:
        excess = _num(bench_metrics.get("excess_return"))
        beta = _num(bench_metrics.get("beta"))
        info_ratio = _num(bench_metrics.get("information_ratio"))
        if excess is not None:
            if excess < 0:
                flags.append(f"跑输基准 {excess:.1%}(vs {bench_symbol}):还不如直接买基准。")
                suggestions.append(f"策略需要跑出正的超额收益才有意义;否则不如指数定投 {bench_symbol}。")
            else:
                assessment.append(f"超额收益 +{excess:.1%}(vs {bench_symbol})。")
        if info_ratio is not None:
            assessment.append(f"信息比率 {info_ratio:.2f}(超额收益的稳定性,>0.5 较好)。")
        if beta is not None and abs(beta) > 1.3:
            assessment.append(f"Beta {beta:.2f}:对基准暴露较大,涨跌大头来自市场而非策略。")
    elif benchmark.get("error"):
        assessment.append(f"基准未对齐({benchmark['error']});换真实行情源可做相对收益评估。")
    else:
        assessment.append("未设基准;建议设 000300.SH 评估相对收益(光看绝对收益不够)。")

    # --- 样本量提醒 --- #
    if trading_days and trading_days < 60:
        flags.append(f"样本太短:仅 {trading_days} 个交易日,统计意义弱,结论不可靠。")
        suggestions.append("拉长回测区间(建议至少 1 年/250 个交易日)再下结论。")

    verdict = _verdict(flags, sharpe, bench_metrics)
    headline = _headline(verdict, cumulative, annualized, sharpe, max_dd)

    return {
        "verdict": verdict,
        "headline": headline,
        "flags": flags,
        "assessment": assessment,
        "suggestions": suggestions,
        "key_metrics": {
            "cumulative_return": cumulative,
            "annualized_return": annualized,
            "sharpe": sharpe,
            "max_drawdown": max_dd,
            "trade_win_rate": win_rate,
            "total_trades": total_trades,
            "closed_trades": closed_trades,
            "trading_days": trading_days,
            "excess_return": _num(bench_metrics.get("excess_return")),
            "benchmark": bench_symbol if bench_metrics else None,
        },
    }


def format_review(review: dict[str, Any]) -> str:
    """把体检报告渲染成人/agent 都好读的文本。"""
    lines = [f"【总评】{review['verdict']} —— {review['headline']}"]
    if review["flags"]:
        lines.append("\n⚠ 红旗:")
        lines.extend(f"  - {item}" for item in review["flags"])
    if review["assessment"]:
        lines.append("\n· 逐项:")
        lines.extend(f"  - {item}" for item in review["assessment"])
    if review["suggestions"]:
        lines.append("\n→ 改进建议:")
        lines.extend(f"  - {item}" for item in review["suggestions"])
    return "\n".join(lines)


def _verdict(flags: list[str], sharpe: float | None, bench_metrics: dict) -> str:
    has_serious = any(("跑输基准" in f) or ("夏普" in f and "为负" in f) or ("零成交" in f) for f in flags)
    if has_serious:
        return "需要改进"
    if flags:
        return "可用但有短板"
    if sharpe is not None and sharpe >= 1.0:
        excess = _num(bench_metrics.get("excess_return")) if bench_metrics else None
        if excess is None or excess > 0:
            return "表现良好"
    return "中性"


def _headline(verdict: str, cumulative: float | None, annualized: float | None, sharpe: float | None, max_dd: float | None) -> str:
    parts = []
    if cumulative is not None:
        parts.append(f"累计 {cumulative:+.1%}")
    if annualized is not None:
        parts.append(f"年化 {annualized:+.1%}")
    if sharpe is not None:
        parts.append(f"夏普 {sharpe:.2f}")
    if max_dd is not None:
        parts.append(f"最大回撤 {max_dd:.1%}")
    return " · ".join(parts) if parts else "无足够数据"


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
