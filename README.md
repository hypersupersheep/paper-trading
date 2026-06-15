# 量化模拟盘 Audit & Replay Prototype

A股**模拟盘(paper trading)**:风控门控、模拟撮合、回测、绩效 tearsheet、4 个行情数据源、AI/agent 接口,可打包成 macOS/Windows 桌面 app。零外部依赖(核心纯标准库),数据各自留在本机。

## 快速上手

**① 直接用桌面 app(同事推荐)** — 不用装 Python、不用懂代码:
1. 打开 [Releases](https://github.com/hypersupersheep/paper-trading/releases/latest)
2. 下对应平台的 zip:`PaperTrading-windows-*.zip`(Windows)/ `PaperTrading-macos-*.zip`(macOS)
3. 解压,双击 `PaperTrading`(Win 是 `PaperTrading.exe`),弹出原生窗口即用。数据自动存各自用户目录,互不干扰。

**② 让 AI / agent 驱动它干活** — 把仓库根目录的 [`SKILL.md`](SKILL.md) 喂给你的 agent(如 work buddy)当 skill,它就能用自然语言/代码导入策略、下单、回测、自审查。详见下方 [Agent 接入](#agent-接入p2)。

**③ 从源码跑(开发)**:`python3 -m backend.server`,浏览器开 http://127.0.0.1:8000 。

---

这是一个零外部依赖的本地 prototype，用于验证模拟盘的 `Audit & Replay Log(审计与复盘日志)`、`paper account(模拟账户)`、`virtual sleeve(虚拟子账户)` 和 `paper broker(模拟券商)` 闭环。

## 运行

```bash
python3 -m backend.server
```

数据存储位置:数据源页有「本地数据存储」卡,可改数据目录(原生窗口里弹系统文件夹选择器,浏览器里手动填路径),可勾选「把现有数据复制过去」,重启生效。位置记在固定指针文件 `~/.papertrading/home`。优先级:环境变量 `PAPER_TRADING_HOME` > 指针文件 > (打包?平台用户目录:代码根)。API:`GET/POST /api/settings/data-location`、`POST /api/settings/data-location/reset`。

环境变量:
- `PAPER_TRADING_HOME`：所有可写数据(SQLite、连接凭证、导入的策略文件)的根目录。默认=代码根(开发态行为不变)。打包成桌面 app 或多用户时,把它指向用户数据目录,数据即与代码分离、各用户隔离、更新 app 不动数据。
- `PORT`(默认 8000)、`HOST`(默认 127.0.0.1,仅本机)。

### 打包成桌面 App(P3)

```bash
.venv/bin/python -m pip install pyinstaller
./build_app.sh         # 产出 dist/PaperTrading/ 和可分享的 zip
```

`launcher.py` 是桌面入口:找空闲端口、起服务,然后用 **pywebview(macOS WKWebView)弹出原生应用窗口**(不是浏览器标签页),关窗或 Cmd-Q 即退;启动时先显示 splash,服务就绪后载入界面。被当 worker 子进程调用时派发(PyInstaller 冻结后 `sys.executable` 是 app 本身,worker 改走 `app __worker__ <kind> <payload>`)。macOS 产出 `dist/PaperTrading.app`(带自定义 `.icns` 图标,可拖进「应用程序」)。数据落用户目录(`PAPER_TRADING_HOME` 默认为平台用户数据目录),更新 app 不动数据、各用户隔离。无 pywebview/设 `PT_NO_WINDOW` 时回退到浏览器/前台服务(便于开发与自动化测试)。详见 `BUILD.md`。app 约 28MB。

### Agent 接入(P2)

`agent/` 目录是一套零依赖(纯标准库)的 AI/agent 接入工具,让任何 agent 用代码或 shell 驱动模拟盘并自审查:

- `agent/paper_trading_client.py`：`PaperTradingClient` SDK,封装全部 API(账户/下单/策略/择时/回测/绩效/自选股/审计)。
- `agent/cli.py`：命令行驱动。旗舰命令 `autoreview` 一步完成「导入策略 → 回测 → quant 视角自审查」。
- `agent/review.py`：`review_backtest(result)` 把回测结果翻译成体检报告(总评/红旗/改进建议),口径与系统绩效一致。
- `agent/SKILL.md`：skill 说明书,任何 agent 读它即可上手;含自回测/自审查工作流。

```bash
python3 agent/cli.py autoreview --name 我的策略 --file my_strategy.py \
  --symbols 000001.SZ --start 2024-01-01 --end 2025-01-01 --data-source ricequant
```

`GET /api/meta`：能力发现端点,返回 `name/version/api_version/data_home/data_sources/capabilities/endpoints`。agent / 未来的 skill 以此发现能力、判断版本兼容(破坏性改动才升 `api_version`,所以后端能演进而不打破旧客户端)。

打开：

```text
http://127.0.0.1:8000
```

## 已实现

- 多账户状态表：账户初始资金、未分配现金、手续费、印花税、滑点、逆回购利率
- 策略 sleeve：账户内分配资金，独立现金、持仓、策略归因
- 组合净值概览：按账户计算 `equity(账户权益)`、现金、持仓市值、浮盈亏、仓位、sleeve PnL 和持仓明细
- 模拟下单 API：策略信号 → 择时门控 → 订单 → 成交 → 现金/持仓/净值审计链路
- 订单簿与订单生命周期：`created/submitted/partially_filled/filled/cancelled/rejected`，默认全额成交，也支持 `fill_quantity` 模拟部分成交或挂单未成交
- 策略导入与运行：支持粘贴 Python callback 或选择本地 `.py` 策略文件导入，独立 subprocess 执行，输出订单后交给 broker 撮合
- 独立择时策略模块：支持粘贴 Python timing callback 或选择本地 `.py` 择时文件导入，输出标准 `TimingDecision(择时决策)`，通过 binding 控制选股策略是否允许开仓
- Pre-trade risk gate(交易前风控门控)：account/sleeve 级限额配置，订单进 broker 前检查，超限直接拒单并写入 `risk_blocked` 决策日志
- 历史区间回测：connector 的 `get_bars` 支持 `start`/`end`——ricequant 原生 date range(rqdatac.get_price，历史回测正解)、fixture 按区间合成、tongdaxin 向更早翻页(深度有限)；区间数据不足时报错会提示数据源实际可取范围并建议改用米筐
- 策略回测(回测页)：一站式回测——选 选股策略/择时策略/标的/区间/交易摩擦/基准/初始资金 → 一键运行 → 净值曲线+沪深300基准+回撤一张图 + 全套指标卡(累计/年化/夏普/回撤/胜率/超额/Beta/信息比率) + 成交明细 + 历史回测列表 + 一键下载(CSV/JSON)。隔离回测引擎 `backend/backtest_store.py`：复用策略/择时 worker 生成无前视信号(信号价=当前 close，成交价=下一 bar open)，自己逐 bar 做组合记账(手续费/印花税/滑点 + 择时门控)与盯市，不触碰真实账户；结果持久化。`POST /api/backtest/run`、`GET /api/backtest/runs`、`GET /api/backtest/{id}`、`GET /api/backtest/{id}/export?format=csv|json`
- 绩效分析(绩效页)：量化 tearsheet——净值曲线 + **沪深300 基准叠加** + 回撤曲线(lightweight-charts) + 指标卡(累计/年化收益、夏普、最大回撤、Calmar、年化波动、日胜率、盈亏比、成交笔数；相对基准的超额收益/Beta/年化 Alpha/信息比率/跑赢天数)；每日 NAV 快照(`backend/performance_store.py`)：seed 历史 + scheduler tick 自动追加 + 手动 `POST /api/portfolio/snapshot`；`GET /api/portfolio/performance?benchmark=000300.SH&benchmark_source=`。指标口径=基于净值日收益序列(252 交易日年化，rf=0)；基准按交易日对齐+归一化，best-effort(fixture 日线已改为真实近期交易日，可在 demo 演示叠加)
- 交易工作区(模拟交易页)：自选股 Watchlist(实时 last/涨跌幅，点击联动 ticket 与图表) + 交易 Ticket(买/卖 toggle、市价/限价、¼/½/全仓快捷仓位按可用资金反推、预估金额) + 持仓 Blotter(成本/现价/市值/浮盈/波动率，一键平仓)
- 自选股与行情 API：`GET/POST /api/watchlist`(workspace 级监控池) 、`GET /api/quotes?symbols=...` 批量返回 last/涨跌额/涨跌幅
- 暗色专业交易终端主题：A 股红涨绿跌(.positive=红/.negative=绿)、状态徽章交通灯语义、tabular 等宽数字；配色 token 集中在 `public/styles.css` :root
- 顶栏常驻账户切换器 + 实时资金条(账户权益/总盈亏/可用现金/持仓市值/仓位)
- K 线图基于 TradingView lightweight-charts(vendor 在 `public/vendor/`)：红涨绿跌蜡烛 + 成交量 + 买卖点 marker + 十字光标 OHLC，点击买卖点打开审计链
- 多视图导航：侧边栏 概览 / 策略 / 择时 / 模拟交易 / 调度 / 日志 Replay / 数据源 七个视图，topbar 标题联动
- 策略持仓矩阵：策略页每个策略(sleeve) 一张表，行为持仓(代码/数量/仓位%/买入价/实时价/波动率/浮动盈亏)，策略级支持资金占比调整(`POST /api/sleeves/{id}/allocation`，percent 或 allocated_cash) 和启停(`POST /api/sleeves/{id}/active`)；停用后 BUY、策略运行、调度 tick 均被拦截(SELL 放行便于退出)，全部入审计
- 数据源视图：connector 配置卡(状态/支持频率/检查耗时/错误提示) + 行情快照面板 + 默认数据源偏好(localStorage，全站下拉自动预选)
- Wind(辉隆只读残血库) connector：MySQL `wind_data`(需先连内网 OpenVPN)，只读账号；**仅日频(1d)**——残血库无分钟/tick；股票走 `ASHAREEODPRICES`、指数走 `AINDEXEODPRICES`，按 `TRADE_DT` 区间查询，适合日频回测/绩效/基准。连接信息存 gitignored 的 `data/connector_settings.json`(不进仓库)，数据源页可填 host/port/user/password/database 并测试。`POST /api/data/connectors/wind/credentials`
- RiceQuant(米筐) connector：数据源页输入 license key，保存即后台 `rqdatac.init` 并实测拉一根日线验证；密钥存 `data/connector_settings.json`(已 gitignore)，审计只记掩码；状态四级 `not_configured/configured/ok/unavailable`；symbol 自动转换(.SZ↔.XSHE, .SH↔.XSHG)
- 实时模拟调度器：创建 live scheduler task，按轮询间隔执行“择时策略 → 选股策略 → broker → audit”的模拟盘循环
- Fixture data connector：提供稳定 5m K 线，用于端到端验证；后续可替换为 TongDaXin/RiceQuant adapter
- TongDaXin(通达信) connector：通过 `mootdx` 在线 HQ server 拉取 A 股 `1m/5m/1d` K 线
- K 线复盘：canvas K 线、成交量、买卖点 marker，点击买卖点打开 audit chain
- A 股 100 股 lot size(一手) 校验
- 交易摩擦：手续费、印花税、滑点分开记账
- 自动国债逆回购：投入、本金返回、利息流水
- `order_ledger(订单流水)`
- `trade_ledger(成交流水)`
- `cash_ledger(现金流水)`
- `position_ledger(持仓流水)`
- `decision_log(决策日志)`
- `system_event_log(系统日志)`
- `portfolio_snapshot(组合快照)`
- 审计链路：信号 → 择时 → 订单 → 成交 → 现金 → 持仓 → 净值
- CSV/JSON 导出
- Logs/Replay 页面筛选与下钻

## API

```http
GET /api/audit/events
GET /api/audit/trades
GET /api/audit/orders
GET /api/audit/cash
GET /api/audit/positions
GET /api/audit/chain/{event_id}
GET /api/audit/export?format=csv|json
```

账户与交易：

```http
GET  /api/accounts
POST /api/accounts
POST /api/accounts/{account_id}/sleeves
GET  /api/portfolio/summary
GET  /api/broker/orders
POST /api/broker/orders
POST /api/broker/orders/{order_id}/cancel
POST /api/accounts/{account_id}/reverse-repo
GET  /api/strategies
POST /api/strategies
POST /api/strategies/{strategy_id}/run
GET  /api/timing-strategies
POST /api/timing-strategies
POST /api/timing-strategies/{timing_strategy_id}/bind
POST /api/timing-strategies/{timing_strategy_id}/run
GET  /api/timing-strategies/{timing_strategy_id}/signals
GET  /api/risk/configs
POST /api/risk/configs
GET  /api/scheduler/tasks
POST /api/scheduler/tasks
POST /api/scheduler/tasks/{task_id}/start
POST /api/scheduler/tasks/{task_id}/stop
POST /api/scheduler/tasks/{task_id}/tick
GET  /api/scheduler/tasks/{task_id}/ticks
GET  /api/data/connectors/health
GET  /api/chart/bars
GET  /api/chart/markers
```

模拟下单示例：

```bash
curl -s -X POST http://127.0.0.1:8000/api/broker/orders \
  -H 'Content-Type: application/json' \
  -d '{
    "account_id": "acct_a_share_alpha",
    "sleeve_id": "sleeve_value_5m",
    "strategy_id": "strategy_value_rotation",
    "run_id": "run_manual",
    "symbol": "600519.SH",
    "side": "BUY",
    "quantity": 100,
    "signal_price": 1726,
    "fill_price": 1726.5,
    "fill_quantity": 100,
    "allow_open": true
  }'
```

`signal_price`/`fill_price` 可省略：省略时 broker 按 `data_source`(默认 fixture) 最新 K 线 close 自动定价，即市价单语义；审计 signal metadata 会记录 `price_source`。前端下单表单价格留空即走市价。

`fill_quantity` 可省略；省略时第一版 paper broker 默认全额成交。传 `0` 会生成 `submitted` 订单但不生成 trade/cash/position 流水，适合模拟挂单未成交；传小于订单数量的 100 股整数倍会生成 `partially_filled`，可用撤单 API 结束剩余数量。

组合净值口径：

- `equity = unallocated_cash + sleeve available_cash + position market_value`
- `position market_value = quantity * last_price`
- `last_price` 第一版来自最近一次成交/标记价；后续接实时行情后可升级为 connector mark-to-market(逐行情盯市)
- `GET /api/portfolio/summary?data_source=fixture&frequency=5m` 会用 connector 最新 K 线 close 作为 `mark_price` 重估持仓；拉不到价格时保留原 `last_price`
- `account pnl = equity - initial_cash`
- `sleeve pnl = sleeve equity - allocated_cash`

策略文件接口：

UI 可直接选择本地 `.py` 文件；浏览器会读取文件内容并通过 JSON API 提交，后端审计 metadata 会记录 `source_filename(源文件名)`，方便回溯策略版本。

导入时平台会**自动接入驱动**(adapter，见 `backend/strategy_adapter.py`)，不要求文件本身定义 `on_bar`。支持的写法：

1. `def on_bar(ctx, bar)`：原生入口，直接使用。
2. 常见回调名：`handle_bar` / `handle_data` / `on_data` / `on_tick` / `on_quote`，自动转接到 `on_bar`。
3. class 策略：文件里唯一一个带 `on_bar`/`handle_bar` 方法的类，平台自动实例化(要求无参构造)并代理；类的 `on_init` 方法也会被接上。
4. 任意名字的单入口函数：两个参数按 `(ctx, bar)` 调用；**一个参数按信号函数处理**——只接收 `bar`，返回值翻译成动作：
   - 选股策略：`"BUY"`/`"SELL"`(默认 1 手)、`(side, qty)`、`{"side":..., "quantity":..., "symbol":..., "reason":...}`、`None`/`"HOLD"` 忽略。
   - 择时策略：`True`/`False` 或 `"risk_on"`/`"risk_off"` 控制开仓，`dict` 透传给 `ctx.set_decision`。

适配代码会以注释块追加到策略文件尾部，审计 metadata 记录 `adapter_mode` 和 `adapter_entry`。文件无法自动适配时(多个候选入口、构造函数有必填参数、语法错误等)，导入报错会用中文说明具体原因和支持的写法。

```python
def on_init(ctx):
    ctx.log("INFO", "strategy initialized")


def on_bar(ctx, bar):
    if bar["close"] > bar["open"]:
        ctx.order_market(bar["symbol"], 100, side="BUY", reason="momentum")
```

`ctx` 当前支持：

- `ctx.history(symbol, fields, window, frequency=None)`
- `ctx.order_market(symbol, quantity, side="BUY", reason=None)`
- `ctx.order_target_percent(symbol, weight, reason=None)`
- `ctx.log(level, message)`
- `ctx.account_id / sleeve_id / strategy_id / run_id / frequency / now`

择时策略文件接口：

择时策略也支持本地 `.py` 文件导入；文件仍需定义 `def on_bar(ctx, bar)`。

```python
def on_init(ctx):
    ctx.log("INFO", "timing initialized")


def on_bar(ctx, bar):
    history = ctx.history(bar["symbol"], ["close"], 3)
    if len(history) < 3:
        return

    allow_open = bar["close"] >= history[0]["close"]
    ctx.set_decision(
        allow_open=allow_open,
        position_policy="hold" if allow_open else "reduce_only",
        reason="risk-on" if allow_open else "risk-off",
        metadata={"lookback": 3},
    )
```

`TimingDecision(择时决策)` 字段：

- `allow_open`: 是否允许被绑定的选股策略新开仓
- `position_policy`: `hold` / `reduce_only` / `close_all` / `target_exposure`
- `target_exposure`: 目标风险敞口，第一版先记录，后续可接组合调仓器
- `reason`: 复盘用原因
- `valid_until`: 可选；过期后 gate 会 fail-closed，禁止新开仓
- `metadata`: 自定义结构化信息

绑定逻辑：

- 未绑定择时策略的选股策略保持原行为。
- 已绑定但还没有最新择时决策时，系统 fail-closed：BUY 会被 `timing_blocked` 拦截，SELL 仍可用于减仓。
- 最新择时决策为 `allow_open=false` 或 `position_policy=reduce_only/close_all` 时，BUY 会被拦截并写入 audit chain。
- 择时策略本身不直接调用 broker，下单仍由选股策略和 broker 模块完成。

## Pre-trade Risk Gate(交易前风控门控)

风控配置分两层，逐字段 merge：`sleeve` 配置覆盖 `account` 配置，未设置的字段回落到账户级，留空表示不限制。

```bash
curl -s -X POST http://127.0.0.1:8000/api/risk/configs \
  -H 'Content-Type: application/json' \
  -d '{
    "account_id": "acct_a_share_alpha",
    "sleeve_id": "sleeve_value_5m",
    "max_order_notional": 100000,
    "max_sleeve_exposure": 0.8,
    "max_symbol_position": 1000,
    "min_cash_buffer": 50000,
    "max_orders_per_tick": 5,
    "max_orders_per_day": 50,
    "enabled": true
  }'
```

不传 `sleeve_id` 即为账户级配置。规则口径：

- `max_order_notional(单笔最大订单金额)`：`quantity * signal_price`，BUY/SELL 都检查。
- `max_sleeve_exposure(资金单元最大敞口)`：比例口径，`(现有持仓市值 + 本单市值) / sleeve equity`，仅 BUY 检查。
- `max_symbol_position(单标的最大持仓)`：买入后持仓股数上限，仅 BUY 检查。
- `min_cash_buffer(最小现金缓冲)`：按全额成交估算 gross + 手续费 + 滑点后，sleeve 剩余现金不得低于该值，仅 BUY 检查。
- `max_orders_per_tick(每 tick 最大订单数)`：同一 `run_id` 内非拒单订单数上限。
- `max_orders_per_day(每日最大订单数)`：按 Asia/Shanghai 交易日统计非拒单订单数。

执行顺序：策略信号 → 择时门控 → **风控门控** → paper broker。命中规则时：

- 订单状态置为 `rejected`，写 `order_rejected` 订单流水。
- 同时写 `decision_log`，`event_type=risk_blocked`，`reason` 标明命中规则、当前值与限额。
- audit chain 可从被拒订单追溯到策略信号与风控原因(`GET /api/audit/chain/{source_event_id}`)。

策略 runner 和 scheduler tick 下的订单走同一个 broker 入口，同样会被风控拦截。

## Live Scheduler(实时调度器)

第一版 scheduler 是本地进程内循环，适合小组 MVP 和本机模拟盘验证：

- `task(任务)` 持久化到 SQLite：账户、sleeve、选股策略、可选择时策略、数据源、symbols、frequency、bar_limit、interval_seconds、trading calendar(交易日历)、bar watermark(K 线水位)。
- `tick(单次调度)` 执行顺序固定：先运行择时策略并记录 `TimingDecision(择时决策)`，再运行选股策略，最后由 paper broker 结算订单。
- 每次 tick 写入 `scheduler_tick_started/completed/skipped/failed` 系统审计日志。
- `calendar_enabled=true` 时，自动 loop 只在 `CN_A(A股)` 工作日 09:30-11:30、13:00-15:00 执行；第一版不含节假日表。
- `dedupe_bars=true` 时，同一批最新 K 线只处理一次；重复 tick 会写 `scheduler_tick_skipped`，原因是 `duplicate_bar`，不会重复下单。
- `POST /tick` 可手动触发一次模拟循环；传 `force=true` 可绕过交易时段限制，但仍保留 K 线去重。
- `POST /start` 会启动本地 daemon thread，按 `interval_seconds` 持续轮询；`POST /stop` 会停止并 join 线程。
- 创建 task 时如果配置了 `timing_strategy_id`，系统会自动把择时策略绑定到对应选股策略和 sleeve。

创建 task 示例：

```bash
curl -s -X POST http://127.0.0.1:8000/api/scheduler/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "Demo 5m Live Loop",
    "account_id": "acct_a_share_alpha",
    "sleeve_id": "sleeve_value_5m",
    "strategy_id": "strategy_demo_momentum",
    "timing_strategy_id": "timing_demo_regime",
    "data_source": "fixture",
    "symbols": "000001.SZ",
    "frequency": "5m",
    "interval_seconds": 300,
    "bar_limit": 8,
    "calendar": "CN_A",
    "calendar_enabled": true,
    "dedupe_bars": true
  }'
```

手动 tick：

```bash
curl -s -X POST http://127.0.0.1:8000/api/scheduler/tasks/sched_demo_5m/tick \
  -H 'Content-Type: application/json' \
  -d '{"force": true}'
```

后续要接近 QMT 式实盘，需要把本地 thread scheduler 升级成独立 worker/service，并加入正式节假日交易日历、bar 完成确认、任务锁、异常重试和进程恢复。

## 测试

```bash
python3 -m unittest discover -s tests
```

更严格地检查 SQLite 连接是否关闭：

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests
```

## TongDaXin(通达信) 数据源

安装依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

用 `.venv` 启动服务，才能让 app import `mootdx`：

```bash
.venv/bin/python -m backend.server
```

检查数据源健康：

```bash
curl -s http://127.0.0.1:8000/api/data/connectors/health
```

运行策略时传入：

```json
{
  "data_source": "tongdaxin",
  "frequency": "5m",
  "symbols": "000001.SZ"
}
```

注意：`mootdx`/通达信 HQ server 属于非官方行情链路，适合内部研究和模拟盘验证；正式研究结论应交叉校验 Wind、Choice、交易所或其他可信源。

## K 线复盘

获取 K 线：

```bash
curl -s "http://127.0.0.1:8000/api/chart/bars?symbol=000001.SZ&data_source=tongdaxin&frequency=5m&limit=80"
```

获取买卖点：

```bash
curl -s "http://127.0.0.1:8000/api/chart/markers?symbol=000001.SZ&account_id=acct_a_share_alpha"
```

前端 `K 线复盘` 面板会把两者叠加显示；点击买卖点会打开该笔交易的完整审计链路。
