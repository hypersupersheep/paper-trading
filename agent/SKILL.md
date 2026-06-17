---
name: paper-trading
description: Drive the 量化模拟盘(A-share paper trading) system via its local REST API. Use when an agent needs to import/run stock or timing strategies, place simulated orders, run isolated backtests over a date range, read performance tearsheets (Sharpe/drawdown/win-rate vs 沪深300), manage watchlists/accounts/sleeves, trace audit chains, or self-review a strategy's backtest and iterate. Talks to a locally running server (default http://127.0.0.1:8000); does NOT connect to a real broker.
---

# Paper Trading(量化模拟盘) Agent Skill

Drive a local A-share **paper trading** system. It simulates strategies like live trading but never touches a real broker. Everything is a local REST API; this skill wraps it in a zero-dependency Python SDK + CLI so any agent can operate it via code or shell.

## Core Facts

- **Local only**: server runs at `http://127.0.0.1:8000` (override with `--base-url` or `PAPER_TRADING_URL`). Start it with `python3 -m backend.server` from the app directory.
- **Simulation, not real money**: orders go to a paper broker. Friction defaults: commission 万0.8 (0.00008, both sides), stamp duty 千1 (0.001, sell only), **adaptive slippage** (square-root market-impact model `η·σ·√(order/ADV)`, applied in both backtest and live). All friction is per-account/per-backtest overridable (`slippage_model` can be `adaptive`/`bps`/`fixed_tick`). A-share 100-share lots; T+1 in backtests.
- **Sleeve is optional for trading/backfill**: `sleeve_id` can be omitted — the system uses the account's default sleeve, or auto-creates a "主仓" one. Only create/specify sleeves when you actually run multiple strategies in one account (sleeve = per-strategy capital bucket + P&L attribution).
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
POST /api/accounts/{id}/delete         删除账户(有持仓需 {"force":true};连带清 sleeve/持仓/订单)
POST /api/accounts/{id}/sleeves        资金单元
POST /api/broker/orders                下单
POST /api/broker/backfill              交易历史补充(补录历史成交,见下文)
GET|POST /api/strategies               选股策略(POST=导入)
POST /api/strategies/{id}/run          运行策略
POST /api/strategies/{id}/delete       删除策略
GET|POST /api/timing-strategies        择时策略
POST /api/backtest/run                 跑回测(返回 curve/metrics/benchmark/trades/summary)
GET  /api/backtest/runs                历史回测
GET  /api/backtest/{id}/export?format=csv|json   下载
GET  /api/portfolio/performance        绩效 tearsheet(净值曲线从账本重建,从首笔成交起;?data_source= 盯市/?benchmark=)
GET  /api/portfolio/summary            组合盯市
GET  /api/accounts/{id}/reverse-repo            国债逆回购记录(独立账本,不在主审计流水)
POST /api/accounts/{id}/reverse-repo            手动逆回购(默认14:30;rate_mode=market 按GC001实时利率,或 custom 自定义 annual_rate;**当日逆回购只能走这个手动接口**)
GET  /api/repo/rate?symbol=204001.SH            逆回购实时年化利率;GET /api/repo/instruments 品种清单
POST /api/accounts/{id}/reverse-repo/reconcile  幂等补全闲置现金的逐日逆回购(**只补今天以前**:逆回购14:30盘后才成交,当日闲置现金未定盘不自动计提;并自愈清掉当日被提前误补的 auto 记录)
GET  /api/quotes?symbols=...           批量行情
GET  /api/audit/chain/{event_id}       审计链路
GET  /api/audit/trades                 交易流水(一笔交易/一只股票折叠成一行,带名称/方向/费用/卖出已实现盈亏;支持 account_id/symbol/strategy_id 过滤)
GET  /api/audit/pnl                     个股已实现盈亏看台(按标的汇总历史买卖,平均成本法)
GET  /api/data/connectors/health       数据源状态
```

## Trade backfill 交易历史补充 (补录历史成交)

Only for recording **real historical trades that the system never logged** — not for normal trading. It bypasses the timing/risk/sleeve gates (the trade already happened, it's not a new decision) but still keeps the ledger consistent (cash, position quantity, avg cost) and tags every entry as a backfill on the audit chain.

- **Strictly required**: `symbol`, `price`, `side`, `quantity`, `trade_date` (`YYYY-MM-DD`, at least date-level). Missing any → rejected.
- Optional: `trade_time` (`HH:MM`), `apply_fees` (default true → applies commission + stamp duty, no slippage), `note`.
- Consistency (enforced): a SELL is validated against the position **held as of its own `trade_date`** (reconstructed from prior fills by timestamp), not the current live position — so backfill the matching BUY *first, with an earlier/equal date*, or the SELL is rejected. A BUY needs sufficient sleeve cash.
- Price sanity (enforced): a fill `price` deviating >2.5x or <0.4x from that day's market close is rejected as a likely typo (skipped only if no market data is reachable). Same as-of-date chronology check also guards `place_order` whenever you pass an explicit `timestamp` (backdated orders) — you cannot sell what wasn't yet held at that time.
- Do **not** use this to fabricate normal trades — only to fill gaps. For live paper trading use `place_order`; for what-if testing use backtest.

```bash
python3 agent/cli.py backfill \
  --account-id acct_x --sleeve-id sleeve_x \
  --symbol 600519.SH --side BUY --quantity 200 --price 1500 \
  --date 2024-02-05 --note "历史漏记的建仓"
```

SDK: `pt.backfill_trade(account_id, sleeve_id, symbol, side, quantity, price, trade_date, trade_time=..., apply_fees=..., note=...)`. HTTP: `POST /api/broker/backfill`.

## Conventions & gotchas

- **A-share codes**: `000001.SZ`(深), `600519.SH`(沪), index `000300.SH`(沪深300). Quantities in 100-share lots.
- **Historical backtests**: use `--data-source ricequant`(or wind) with `--start/--end`; `fixture` runs any range but is synthetic; `tongdaxin` only reaches a limited history.
- **Frequency**: backtests default `1d`. `wind` is daily-only.
- **Always set a benchmark** (default 000300.SH) so the review can judge relative performance — absolute return alone is misleading.
- Strategy/timing imports accumulate; `delete_strategy()` / `delete_timing_strategy()` to clean up when iterating.
