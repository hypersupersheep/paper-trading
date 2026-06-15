# 模拟盘 App 交接报告

生成日期：2026-06-11  
项目目录：`/Users/chenyangsun/Documents/Codex/2026-06-10/goal-here-is-the-thing-app`

## Table of Contents

- [1. 总目标](#1-总目标)
- [2. 当前状态](#2-当前状态)
- [3. 已完成模块](#3-已完成模块)
- [4. 已有 API](#4-已有-api)
- [5. 前端能力](#5-前端能力)
- [6. 验证状态](#6-验证状态)
- [7. 运行与检查命令](#7-运行与检查命令)
- [8. 关键文件](#8-关键文件)
- [9. 注意事项](#9-注意事项)
- [10. 建议下一步](#10-建议下一步)

## 1. 总目标

我们正在为量化小组开发一个自用 `paper trading app(模拟盘应用)`。

目标是让策略像实盘一样运行，但不连接真实券商下单。系统应能：

- 导入 `stock selection strategy(选股策略)` 文件。
- 导入独立的 `timing strategy(择时策略)` 文件。
- 连接外部行情源，例如 `TongDaXin(通达信)`、RiceQuant 等。
- 支持 `5m/1m/second-level(五分钟/一分钟/秒级)` 数据兼容，后续 connector 按数据源能力扩展。
- 支持多账户、多策略、多 `sleeve(策略资金单元)` 并行运行。
- 自定义初始资金、手续费、滑点、印花税、国债逆回购利率。
- 记录每笔订单、成交、现金、持仓、择时拦截、策略信号、系统事件。
- 在 K 线图上展示买卖点，并能点击查看完整 `audit chain(审计链路)`。
- 支持 CSV/JSON 导出，方便 Obsidian/Jupyter Lab 复盘。

当前项目还不是最终完成品，而是一个可运行的 MVP 骨架。核心交易、审计、策略、择时、调度、K 线和净值模块已经打通。

## 2. 当前状态

- 后端：Python stdlib `http.server` + SQLite，本地轻量原型。
- 前端：static web app，位于 `public/`，不是 React/Next 项目。
- 数据库：`data/audit.sqlite3`。
- 之前本地服务曾运行在 `http://127.0.0.1:8000`，接手时需要重新确认进程是否仍在。
- Git 状态此前显示大部分文件为 untracked，因此不要只依赖 `git diff` 判断项目内容。

## 3. 已完成模块

### 3.1 Audit & Replay Log(审计与复盘日志)

已实现结构化审计层，底层使用 SQLite。

已覆盖：

- `order_ledger(订单流水)`：创建、提交、撤单、拒单、部分成交、全部成交。
- `trade_ledger(成交流水)`：symbol、方向、数量、价格、成交时间、策略、账户、sleeve。
- `cash_ledger(现金流水)`：买卖扣款、手续费、印花税、滑点、国债逆回购投入、国债逆回购利息、资金分配。
- `position_ledger(持仓流水)`：数量、可用数量、成本价、市值、浮动盈亏变化。
- `decision_log(决策日志)`：策略信号、择时决策、订单被门控拦截的原因。
- `system_event_log(系统日志)`：数据源更新、策略启动/停止、异常、connector health(连接器健康状态)。
- `portfolio_snapshot(组合快照)`：账户净值与组合状态。

每条核心记录已带有：

- `timestamp`
- `account_id`
- `sleeve_id`
- `strategy_id`
- `run_id`
- `event_type`
- `symbol`
- `amount`
- `quantity`
- `price`
- `before_state`
- `after_state`
- `reason`
- `source_event_id`
- `metadata`

`audit chain(审计链路)` 已能追溯：

信号 -> 择时门控 -> 订单 -> 成交 -> 现金变化 -> 持仓变化 -> 净值变化

### 3.2 Paper Account / Sleeve / Broker(模拟账户/策略资金单元/模拟券商)

已实现：

- 自定义账户。
- 自定义初始资金。
- 账户未分配现金。
- `sleeve(策略资金单元)` 现金分配。
- BUY/SELL 订单。
- A 股 100 股 lot 校验。
- 订单状态：`created`、`submitted`、`partially_filled`、`filled`、`cancelled`、`rejected`。
- `fill_quantity` 支持：
  - 省略：全部成交。
  - `0`：只提交订单，不成交。
  - 小于订单数量：部分成交。
- open/partial 订单撤单。
- 交易后自动更新现金、持仓、审计日志。

### 3.3 Trading Friction(交易摩擦)

已支持：

- `commission_rate(手续费率)`。
- `min_commission(最低手续费)`。
- `stamp_duty_rate(印花税率)`。
- `slippage_model(滑点模型)`。
- `slippage_value(滑点值)`。

手续费、印花税、滑点分别写入现金流水，未合并成一个 cost 字段。

### 3.4 Reverse Repo(国债逆回购)

已支持：

- 账户级别 reverse repo 配置。
- 国债逆回购投入现金流水。
- 到期利息到账现金流水。
- 更新账户未分配现金。

### 3.5 Strategy Module(策略模块)

已支持导入策略 `.py` 文件或粘贴代码。

策略文件要求定义：

```python
def on_bar(ctx, bar):
    ...
```

策略通过 subprocess worker 运行。

`ctx` 已支持：

- `history(symbol, fields, window, frequency=None)`
- `order_market(symbol, quantity, side="BUY", reason=None)`
- `order_target_percent(symbol, weight, reason=None)`
- `log(level, message)`
- `account_id`
- `sleeve_id`
- `strategy_id`
- `frequency`
- `now`

导入时会记录 `source_filename` 到审计 metadata。

### 3.6 Timing Strategy Module(择时策略模块)

择时策略模块已和选股策略独立。

择时策略文件要求定义：

```python
def on_bar(ctx, bar):
    ctx.set_decision(...)
```

标准 `TimingDecision` 字段：

- `allow_open`
- `position_policy`
- `target_exposure`
- `reason`
- `valid_until`
- `metadata`

支持的 `position_policy`：

- `hold`
- `reduce_only`
- `close_all`
- `target_exposure`

当前逻辑为 fail-closed：

- 绑定择时策略后，BUY 必须有有效允许开仓决策。
- 没有最新择时决策，或者 `allow_open=false`，则 BUY 被拦截。
- SELL 可以继续执行。
- 拦截原因会进入 `decision_log(决策日志)`。

### 3.7 Data Connectors(数据源连接器)

已实现 connector registry。

当前 connector：

- `fixture`：确定性测试行情，支持 `5m/1m/1d`。
- `tongdaxin`：`TongDaXin(通达信)` 原型 connector，依赖可选 `mootdx`，支持 online HQ server，健康状态可查询。

RiceQuant 暂未实现，需要后续根据账号、API 权限、数据字段和频率支持补 connector。

### 3.8 Chart / K-line(K线图)

已实现：

- K 线数据 API。
- 成交 marker API。
- 前端 canvas K 线图。
- 成交买卖点 marker。
- 点击买卖点打开对应 `audit chain(审计链路)`。

### 3.9 Scheduler(实时模拟调度)

已实现本地 scheduler。

任务字段包括：

- `account_id`
- `sleeve_id`
- `strategy_id`
- `timing_strategy_id`
- `data_source`
- `symbols`
- `frequency`
- `interval`
- `bar_limit`
- `calendar`
- `calendar_enabled`
- `dedupe_bars`
- `last_bar_key`
- `last_bar_at`
- `ticks_completed`
- `ticks_skipped`

执行顺序：

行情 -> 择时策略 -> 选股策略 -> 模拟券商 -> 审计日志

已有能力：

- start/stop 本地 daemon thread。
- manual tick。
- A 股交易时段门控：周一至周五 09:30-11:30、13:00-15:00，Asia/Shanghai。
- latest bar 去重，重复 bar 会跳过并记录 `scheduler_tick_skipped`。

目前还没有真实节假日日历。

### 3.10 Portfolio / NAV(组合净值)

已实现账户组合汇总。

净值公式：

```text
equity = unallocated_cash + sleeve available_cash + position market_value
```

支持用 connector 最新 close 做 `mark-to-market(盯市)`。

`GET /api/portfolio/summary` 支持：

- `account_id`
- `data_source`
- `frequency`

返回中包含：

- account equity
- pnl
- cash
- market value
- exposure
- sleeve rows
- position rows
- mark metadata
- connector health

上一轮 smoke test 中，`000001.SZ` 的 `last_price=10.9` 被 fixture 5m close 标记为 `mark_price=10.12`，说明盯市链路已跑通。

## 4. 已有 API

### Audit API

```http
GET /api/audit/events
GET /api/audit/trades
GET /api/audit/orders
GET /api/audit/cash
GET /api/audit/positions
GET /api/audit/chain/{event_id}
GET /api/audit/export?format=csv|json
```

### Account / Broker API

```http
GET /api/accounts
POST /api/accounts
POST /api/accounts/{account_id}/sleeves
GET /api/broker/orders
POST /api/broker/orders
POST /api/broker/orders/{order_id}/cancel
POST /api/accounts/{account_id}/reverse-repo
GET /api/portfolio/summary
```

### Strategy API

```http
GET /api/strategies
POST /api/strategies
POST /api/strategies/{strategy_id}/run
GET /api/strategies/{strategy_id}/signals
```

### Timing Strategy API

```http
GET /api/timing-strategies
POST /api/timing-strategies
POST /api/timing-strategies/{timing_strategy_id}/bind
POST /api/timing-strategies/{timing_strategy_id}/run
GET /api/timing-strategies/{timing_strategy_id}/signals
```

### Data / Chart API

```http
GET /api/data/connectors/health
GET /api/chart/bars
GET /api/chart/markers
```

### Scheduler API

```http
GET /api/scheduler/tasks
POST /api/scheduler/tasks
POST /api/scheduler/tasks/{task_id}/start
POST /api/scheduler/tasks/{task_id}/stop
POST /api/scheduler/tasks/{task_id}/tick
GET /api/scheduler/tasks/{task_id}/ticks
```

## 5. 前端能力

前端位于 `public/`。

已实现：

- sidebar dashboard。
- 账户创建。
- sleeve 创建。
- 下单表单。
- 撤单。
- 国债逆回购表单。
- 组合净值面板。
- 数据源选择。
- 频率选择。
- K 线图。
- 买卖点 marker。
- 策略导入、运行。
- 择时策略导入、绑定、运行。
- scheduler task 创建、启动、停止、manual tick。
- audit log 筛选。
- audit chain drill-down。
- CSV/JSON 导出按钮。

前端文件：

- `public/index.html`
- `public/app.js`
- `public/styles.css`

## 6. 验证状态

上一轮完整验证通过。

测试命令：

```bash
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests
```

结果：

```text
33 tests OK
```

语法检查通过：

```bash
.venv/bin/python -m py_compile backend/trading_store.py backend/server.py
node --check public/app.js
```

Browser smoke test 通过：

- portfolio panel 显示 `fixture 5m close · marked 3/3`。
- refresh button 可用。
- console 无 errors/warnings。
- 390px mobile viewport 无水平 overflow。
- K 线和 audit chain 之前已通过交互验证。

## 7. 运行与检查命令

确认服务是否正在运行：

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

启动服务：

```bash
.venv/bin/python -m backend.server
```

打开：

```text
http://127.0.0.1:8000
```

跑测试：

```bash
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests
```

Python 语法检查：

```bash
.venv/bin/python -m py_compile backend/*.py
```

前端 JavaScript 检查：

```bash
node --check public/app.js
```

查看当前文件：

```bash
rg --files
```

注意：因为 repo 文件此前大多 untracked，`git diff` 可能不能反映全部项目内容。

## 8. 关键文件

### Backend

- `backend/server.py`：HTTP server、路由、demo seed、模块装配。
- `backend/audit_store.py`：审计日志 SQLite 层。
- `backend/trading_store.py`：账户、sleeve、订单、成交、持仓、现金、净值。
- `backend/strategy_store.py`：策略保存、导入、运行管理。
- `backend/strategy_worker.py`：策略 subprocess worker。
- `backend/timing_store.py`：择时策略保存、绑定、信号管理。
- `backend/timing_worker.py`：择时策略 subprocess worker。
- `backend/data_connectors.py`：fixture 与 TongDaXin connector。
- `backend/chart_service.py`：K 线和成交 marker 服务。
- `backend/scheduler_store.py`：scheduler task、tick、bar dedupe、交易时段门控。

### Frontend

- `public/index.html`
- `public/app.js`
- `public/styles.css`

### Tests

- `tests/test_audit_store.py`
- `tests/test_trading_store.py`
- `tests/test_strategy_store.py`
- `tests/test_timing_store.py`
- `tests/test_scheduler_store.py`
- `tests/test_data_connectors.py`
- `tests/test_chart_service.py`

### Data / Runtime

- `data/audit.sqlite3`
- `strategies/`
- `timing_strategies/`
- `outputs/`

## 9. 注意事项

1. 不要把当前目标标记完成。当前只是 MVP 骨架和关键模块初版。

2. 当前 server session 不一定还活着。接手后先跑：

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

3. 如果需要重启服务，先确认旧进程，避免多个 server 抢同一 SQLite 文件。

4. Browser 文件选择器不一定稳定。验证策略文件导入时，可以用 API smoke test 或直接模拟 file content。

5. `TongDaXin(通达信)` connector 是原型，依赖 `mootdx`。如果本地没装或 HQ server 不通，health 会显示 unavailable，这不是核心 app 逻辑失败。

6. RiceQuant connector 尚未实现。不要假设已有 RiceQuant 账号、token、权限或字段 schema。

7. `5m/1m/1d` 已有 fixture 支持，秒级数据还没有真实 connector。后续应在 connector 层统一 frequency contract。

8. 当前 scheduler 是 in-process local thread，适合 MVP，不适合多人/高频生产环境。

9. 交易撮合目前是简化模拟：默认即时成交，支持请求中传 `fill_quantity` 模拟部分成交。还不是完整 order book matching engine。

10. 普通 system log 和 audit ledger 已分开。后续不要把复盘关键数据混进杂乱运行日志。

## 10. 建议下一步

上一位 agent 在中断前准备做 `pre-trade risk gate(交易前风控门控)`，但尚未开始代码编辑。建议下一步直接实现此模块。

### 10.1 Pre-trade Risk Gate(交易前风控门控)

建议能力：

- 账户级和 sleeve 级风险配置。
- `max_order_notional(单笔最大订单金额)`。
- `max_sleeve_exposure(资金单元最大敞口)`。
- `max_symbol_position(单标的最大持仓)`。
- `min_cash_buffer(最小现金缓冲)`。
- 可选 `max_orders_per_tick(每 tick 最大订单数)`。
- 可选 `max_orders_per_day(每日最大订单数)`。

建议行为：

- 订单进入 broker 前先经过 risk gate。
- 被拦截时生成 `order_rejected`。
- 同时写入 `decision_log(决策日志)`，event_type 可用 `risk_blocked`。
- `reason` 必须说明命中的具体规则和当前值。
- `audit chain(审计链路)` 必须能从被拒订单追溯到策略信号和风控原因。

建议测试：

- 单笔金额超过上限时拒单。
- sleeve exposure 超限时拒单。
- 单标的持仓超限时拒单。
- cash buffer 不足时拒单。
- scheduler 触发的策略订单也会被 risk gate 拦截。
- 风控拒单必须有 decision log 和 order ledger。

### 10.2 后续大模块

完成 risk gate 后，建议按以下顺序推进：

1. RiceQuant connector。
2. 秒级/tick 数据 connector contract。
3. 真实交易日历和节假日表。
4. 更真实的 fill engine，包括 limit order、成交延迟、盘口滑点。
5. portfolio 自动盯市任务，而不是只靠手动刷新。
6. macOS wrapper 或桌面应用包装。
7. 团队多人使用时迁移 PostgreSQL。
8. 权限、用户、账户隔离和审计权限控制。
