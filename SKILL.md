---
name: paper-trading
description: Drive the 量化模拟盘(A-share paper trading) system via its local REST API. Use when an agent needs to import/run stock or timing strategies, place simulated orders, run isolated backtests over a date range, read performance tearsheets (Sharpe/drawdown/win-rate vs 沪深300), manage watchlists/accounts/sleeves, trace audit chains, or self-review a strategy's backtest and iterate. Talks to a locally running server (default http://127.0.0.1:8000); does NOT connect to a real broker.
---

# Paper Trading(量化模拟盘) Agent Skill

Drive a local A-share **paper trading** system. It simulates strategies like live trading but never touches a real broker. Everything is a local REST API; this skill wraps it in a zero-dependency Python SDK + CLI so any agent can operate it via code or shell.

## Core Facts

- **Local only**: server runs at `http://127.0.0.1:8000` (override with `--base-url` or `PAPER_TRADING_URL`). Start it with `python3 -m backend.server` from the app directory.
- **Simulation, not real money**: orders go to a paper broker (friction modeled: commission/stamp duty/slippage; A-share 100-share lots; T+1 in backtests).
- **Discover first**: `GET /api/meta` returns version, `api_version`, `data_home`, data sources, and a capabilities map. Always check it before acting (the SDK's `check_compatible()` does this).
- **Data isolation**: all writable data lives under `PAPER_TRADING_HOME` (default = app dir). Each user/instance can have its own.
- **Data sources**: `fixture`(synthetic, for testing), `tongdaxin`(realtime A-share via mootdx), `ricequant`(米筐 rqdatac, best for historical date ranges), `wind`(辉隆 Wind read-only MySQL, daily only, needs内网 VPN). Historical backtests need a real source with the date range; `fixture` works for any range but is synthetic.

## Tools in this skill

- `paper_trading_client.py` — `PaperTradingClient` SDK (stdlib only). Import it to drive everything programmatically.
- `cli.py` — command-line wrapper. Best for shell-driven agents.
- `review.py` — `review_backtest(result)` turns a backtest into a quant-style critique (verdict / red flags / suggestions).

## Quick Start (CLI)

```bash
# 1. Discover capabilities
python3 agent/cli.py meta

# 2. List what's there
python3 agent/cli.py strategies
python3 agent/cli.py accounts

# 3. FLAGSHIP — import a strategy file, backtest it, and self-review, in one step:
python3 agent/cli.py autoreview \
  --name "我的动量" --file my_strategy.py \
  --symbols 000001.SZ --start 2024-01-01 --end 2025-01-01 \
  --data-source fixture
```

Add `--json` to any command for machine-readable output.

## Quick Start (SDK)

```python
from paper_trading_client import PaperTradingClient
from review import review_backtest, format_review

pt = PaperTradingClient()
pt.check_compatible()                      # handshake + version guard

code = open("my_strategy.py").read()
sid = pt.import_strategy("我的动量", code)["strategy"]["id"]
result = pt.run_backtest(sid, symbols="000001.SZ", start="2024-01-01", end="2025-01-01")
print(format_review(review_backtest(result)))
```

## Strategy code convention

A strategy is a Python file. The platform drives it bar-by-bar. The canonical entry is:

```python
def on_bar(ctx, bar):
    # bar: {"symbol","timestamp","open","high","low","close","volume", ...}
    if bar["close"] > bar["open"]:
        ctx.order_market(bar["symbol"], 100, side="BUY", reason="momentum")
```

You do **not** have to name it `on_bar` — the platform auto-adapts common shapes: `handle_bar`/`handle_data`, a class with an `on_bar` method, or a single signal function that returns `"BUY"`/`"SELL"`/`dict`. `ctx` exposes `history()`, `order_market()`, `order_target_percent()`, `log()`. **No look-ahead**: signal price = current close, fill = next bar open.

Timing strategies are similar but call `ctx.set_decision(allow_open=..., position_policy=...)`; binding one gates a stock strategy's BUYs.

## Self-backtest & self-review loop (核心工作流)

This is how an agent **proactively** tests and critiques a strategy, then iterates:

1. **Write/obtain** strategy code.
2. **`autoreview`** (or SDK: import → `run_backtest` → `review_backtest`). Get back metrics + a verdict + red flags + suggestions.
3. **Read the review.** It encodes quant judgment, e.g.:
   - *只买不卖* → no exits, only tested buy-and-hold → add sell/stop logic.
   - *跑输基准* → worse than just buying 000300.SH → needs positive alpha.
   - *负夏普 / 深回撤 / 换手过高 / 样本太短* → specific, actionable.
4. **Revise** the code to address the flags, re-import (or `delete_strategy` the old one), re-backtest, **compare** metrics.
5. **Repeat** until verdict is "表现良好" or flags are resolved.

The review is intentionally opinionated so even a general agent gets a structured "what a quant looks at" critique. Verdicts: `表现良好` / `中性` / `可用但有短板` / `需要改进`.

## What the system can do (capabilities)

Accounts & sleeves (multi-strategy capital units), paper broker (market/limit orders, one-click close), **pre-trade risk gate** (per-account/sleeve limits → rejects + audit), stock + timing strategies (auto-adapted), scheduler (replay loop), **isolated backtest** (date range, friction, benchmark), **performance tearsheet** (cumulative/annualized/Sharpe/max-drawdown/Calmar/win-rate, + relative vs 沪深300: excess/Beta/alpha/info-ratio), watchlist, full **audit chain** (signal → timing → risk → order → fill → cash → position → equity), CSV/JSON export.

## Key API endpoints (for direct HTTP if not using the SDK)

```
GET  /api/meta                         能力发现(先调这个)
GET|POST /api/accounts                 账户
POST /api/accounts/{id}/sleeves        资金单元
POST /api/broker/orders                下单
GET|POST /api/strategies               选股策略(POST=导入)
POST /api/strategies/{id}/run          运行策略
POST /api/strategies/{id}/delete       删除策略
GET|POST /api/timing-strategies        择时策略
POST /api/backtest/run                 跑回测(返回 curve/metrics/benchmark/trades/summary)
GET  /api/backtest/runs                历史回测
GET  /api/backtest/{id}/export?format=csv|json   下载
GET  /api/portfolio/performance        绩效 tearsheet(可带 ?benchmark=&benchmark_source=)
GET  /api/portfolio/summary            组合盯市
GET  /api/quotes?symbols=...           批量行情
GET  /api/audit/chain/{event_id}       审计链路
GET  /api/data/connectors/health       数据源状态
```

## Conventions & gotchas

- **A-share codes**: `000001.SZ`(深), `600519.SH`(沪), index `000300.SH`(沪深300). Quantities in 100-share lots.
- **Historical backtests**: use `--data-source ricequant`(or wind) with `--start/--end`; `fixture` runs any range but is synthetic; `tongdaxin` only reaches a limited history.
- **Frequency**: backtests default `1d`. `wind` is daily-only.
- **Always set a benchmark** (default 000300.SH) so the review can judge relative performance — absolute return alone is misleading.
- Strategy/timing imports accumulate; `delete_strategy()` / `delete_timing_strategy()` to clean up when iterating.
