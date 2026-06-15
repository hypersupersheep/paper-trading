#!/usr/bin/env python3
"""量化模拟盘 Agent CLI —— 让 agent 用一条命令驱动模拟盘并自审查。

零依赖(仅标准库)。在模拟盘服务运行时使用:

  python3 agent/cli.py meta
  python3 agent/cli.py strategies
  python3 agent/cli.py import-strategy --name 我的动量 --file my_strategy.py
  python3 agent/cli.py backtest --strategy strategy_xxx --symbols 000001.SZ --start 2024-01-01 --end 2025-01-01
  python3 agent/cli.py autoreview --name 动量 --file my_strategy.py --symbols 000001.SZ --start 2024-01-01 --end 2025-01-01

`autoreview` 是旗舰命令:一步完成「导入策略 → 回测 → quant 视角自审查」,
正是 agent 主动回测+自我审查的入口。所有命令加 --json 输出机器可读结构。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from paper_trading_client import PaperTradingClient, PaperTradingError  # noqa: E402
from review import format_review, review_backtest  # noqa: E402


def _client(args: argparse.Namespace) -> PaperTradingClient:
    return PaperTradingClient(base_url=args.base_url)


def _read_code(args: argparse.Namespace) -> tuple[str, str | None]:
    if args.file:
        path = os.path.abspath(args.file)
        return open(path, encoding="utf-8").read(), os.path.basename(path)
    if args.code:
        return args.code, None
    raise SystemExit("需要 --file 或 --code 提供策略代码")


def cmd_meta(args, pt):
    meta = pt.meta()
    if args.json:
        return meta
    lines = [
        f"{meta['name']}  v{meta['version']} (api v{meta['api_version']})",
        f"数据目录: {meta['data_home']}",
        f"数据源:   {', '.join(meta['data_sources'])}",
        f"能力:     {', '.join(k for k, v in meta['capabilities'].items() if v)}",
    ]
    return "\n".join(lines)


def cmd_accounts(args, pt):
    accounts = pt.list_accounts()
    if args.json:
        return accounts
    return "\n".join(f"{a['id']}  {a['name']}  现金 {a['unallocated_cash']:,.0f}" for a in accounts) or "(无账户)"


def cmd_strategies(args, pt):
    strategies = pt.list_strategies()
    if args.json:
        return strategies
    return "\n".join(f"{s['id']}  {s['name']}" for s in strategies) or "(无策略)"


def cmd_import_strategy(args, pt):
    code, filename = _read_code(args)
    data = pt.import_strategy(args.name, code, source_filename=filename)
    strategy = data["strategy"]
    if args.json:
        return data
    adapter = strategy.get("adapter") or {}
    note = f"(自动接入驱动: {adapter.get('entry')})" if adapter.get("mode") not in (None, "native") else ""
    return f"已导入 {strategy['name']}  id={strategy['id']} {note}"


def _backtest_from_args(args, pt) -> dict:
    return pt.run_backtest(
        args.strategy,
        symbols=args.symbols,
        start=args.start,
        end=args.end,
        timing_strategy_id=args.timing,
        frequency=args.frequency,
        data_source=args.data_source,
        initial_cash=args.initial_cash,
        benchmark=args.benchmark,
        benchmark_source=args.benchmark_source,
        commission_rate=args.commission,
        stamp_duty_rate=args.stamp,
        slippage_value=args.slippage,
        name=args.name,
    )


def cmd_backtest(args, pt):
    result = _backtest_from_args(args, pt)
    if args.json:
        out = {"id": result.get("id"), "summary": result["summary"], "metrics": result["metrics"]}
        if not args.no_review:
            out["review"] = review_backtest(result)
        return out
    text = _summarize_backtest(result)
    if not args.no_review:
        text += "\n\n" + format_review(review_backtest(result))
    return text


def cmd_review(args, pt):
    result = pt.get_backtest(args.backtest_id)
    review = review_backtest(result)
    return review if args.json else (_summarize_backtest(result) + "\n\n" + format_review(review))


def cmd_autoreview(args, pt):
    code, filename = _read_code(args)
    strategy = pt.import_strategy(args.name, code, source_filename=filename)["strategy"]
    args.strategy = strategy["id"]
    result = _backtest_from_args(args, pt)
    review = review_backtest(result)
    if args.json:
        return {"strategy_id": strategy["id"], "backtest_id": result.get("id"), "summary": result["summary"], "review": review}
    return (
        f"策略已导入: {strategy['name']} (id={strategy['id']})\n"
        + _summarize_backtest(result)
        + "\n\n"
        + format_review(review)
    )


def cmd_performance(args, pt):
    perf = pt.performance(account_id=args.account_id)
    if args.json:
        return perf
    m = perf.get("metrics", {})
    return (
        f"账户 {perf.get('account_name')}  净值点 {perf.get('points')}\n"
        f"累计 {m.get('cumulative_return', 0):+.2%} · 年化 {m.get('annualized_return', 0):+.2%} · "
        f"夏普 {m.get('sharpe', 0):.2f} · 最大回撤 {m.get('max_drawdown', 0):.2%}"
    )


def cmd_quotes(args, pt):
    quotes = pt.quotes(args.symbols, data_source=args.data_source)
    if args.json:
        return quotes
    return "\n".join(
        f"{q['symbol']}  {q.get('last', '--')}  {q.get('change_pct', 0):+.2%}" if not q.get("error") else f"{q['symbol']}  {q['error']}"
        for q in quotes
    )


def cmd_backfill(args, pt):
    data = pt.backfill_trade(
        args.account_id,
        args.sleeve_id,
        args.symbol,
        args.side.upper(),
        args.quantity,
        args.price,
        args.date,
        trade_time=args.time,
        apply_fees=not args.no_fees,
        note=args.note,
    )
    if args.json:
        return data
    costs = data.get("costs", {})
    return (
        f"已补录 {data['side']} {data['symbol']} {data['quantity']}@{data['price']}  "
        f"日期 {data['timestamp'][:10]}\n"
        f"  持仓 -> {data['position_after']} · sleeve 现金 -> {data['cash_after']:,.2f} · "
        f"佣金 {costs.get('commission', 0):.2f} 印花税 {costs.get('stamp_duty', 0):.2f}"
    )


def _summarize_backtest(result: dict) -> str:
    s = result.get("summary", {})
    m = result.get("metrics", {})
    timing = f" · 择时 {s.get('timing_decisions')} 决策" if s.get("timing_strategy_id") else " · 无择时"
    return (
        f"回测 {result.get('id', '')}  {', '.join(s.get('symbols', []))}  {s.get('start')}~{s.get('end')}  "
        f"{m.get('trading_days', 0)} 日{timing}\n"
        f"  累计 {m.get('cumulative_return', 0):+.2%} · 年化 {m.get('annualized_return', 0):+.2%} · "
        f"夏普 {m.get('sharpe', 0):.2f} · 最大回撤 {m.get('max_drawdown', 0):.2%}\n"
        f"  成交 {s.get('total_trades', 0)} 笔(平仓 {s.get('closed_trades', 0)}) · "
        f"被拒 {s.get('rejected_orders', 0)} · 末净值 {s.get('final_equity', 0):,.0f}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="量化模拟盘 Agent CLI")
    parser.add_argument("--base-url", default=os.environ.get("PAPER_TRADING_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("meta", help="服务能力与版本").set_defaults(func=cmd_meta)
    sub.add_parser("accounts", help="列出账户").set_defaults(func=cmd_accounts)
    sub.add_parser("strategies", help="列出策略").set_defaults(func=cmd_strategies)

    p = sub.add_parser("import-strategy", help="导入选股策略")
    p.add_argument("--name", required=True)
    p.add_argument("--file")
    p.add_argument("--code")
    p.set_defaults(func=cmd_import_strategy)

    def add_backtest_args(p: argparse.ArgumentParser, need_strategy: bool) -> None:
        if need_strategy:
            p.add_argument("--strategy", required=True, help="策略 id")
        p.add_argument("--symbols", default="000001.SZ")
        p.add_argument("--start")
        p.add_argument("--end")
        p.add_argument("--timing", help="可选择时策略 id")
        p.add_argument("--frequency", default="1d")
        p.add_argument("--data-source", default="fixture")
        p.add_argument("--initial-cash", type=float, default=1_000_000)
        p.add_argument("--benchmark", default="000300.SH")
        p.add_argument("--benchmark-source")
        p.add_argument("--commission", type=float, default=0.00025)
        p.add_argument("--stamp", type=float, default=0.0005)
        p.add_argument("--slippage", type=float, default=2.0)
        p.add_argument("--no-review", action="store_true", help="只出回测,不自动审查")
        if need_strategy:
            # autoreview 用它自己的 --name(策略名)兼作回测名,这里只给独立 backtest 命令加。
            p.add_argument("--name", default="agent backtest")

    p = sub.add_parser("backtest", help="跑回测(默认附自审查)")
    add_backtest_args(p, need_strategy=True)
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("autoreview", help="旗舰:导入策略文件 → 回测 → 自审查,一步到位")
    p.add_argument("--name", required=True)
    p.add_argument("--file")
    p.add_argument("--code")
    add_backtest_args(p, need_strategy=False)
    p.set_defaults(func=cmd_autoreview)

    p = sub.add_parser("review", help="审查一个已存在的回测")
    p.add_argument("--backtest-id", required=True)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("backfill", help="交易历史补充:补录此前未记录的历史成交(只补历史,勿造正常交易)")
    p.add_argument("--account-id", required=True)
    p.add_argument("--sleeve-id", required=True)
    p.add_argument("--symbol", required=True, help="如 600519.SH")
    p.add_argument("--side", required=True, choices=["BUY", "SELL", "buy", "sell"])
    p.add_argument("--quantity", type=int, required=True)
    p.add_argument("--price", type=float, required=True, help="真实成交价")
    p.add_argument("--date", required=True, help="成交日期 YYYY-MM-DD(至少到日)")
    p.add_argument("--time", help="可选成交时间 HH:MM")
    p.add_argument("--no-fees", action="store_true", help="不计佣金/印花税,只按本金调整")
    p.add_argument("--note", default="", help="备注")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("performance", help="账户绩效")
    p.add_argument("--account-id")
    p.set_defaults(func=cmd_performance)

    p = sub.add_parser("quotes", help="批量行情")
    p.add_argument("--symbols", required=True)
    p.add_argument("--data-source", default="fixture")
    p.set_defaults(func=cmd_quotes)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    pt = _client(args)
    try:
        output = args.func(args, pt)
    except PaperTradingError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
