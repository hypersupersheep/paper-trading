# Paper Trading · A股量化模拟盘

本地优先的 A 股模拟交易与策略研究平台:模拟撮合、交易前风控、隔离回测、绩效分析、多数据源行情,并提供 AI agent 接口。核心零第三方依赖,数据留在本机,可打包成 macOS / Windows 桌面应用。

## 快速开始

**桌面应用(推荐给非开发者)**
到 [Releases](https://github.com/hypersupersheep/paper-trading/releases/latest) 下载对应平台的压缩包,解压双击运行。首次启动弹出原生窗口,数据自动存到各自用户目录,互不干扰。

**从源码运行**

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m backend.server     # http://127.0.0.1:8000
```

**用 agent 驱动**
把 [`SKILL.md`](SKILL.md) 交给支持技能的 agent,即可用自然语言导入策略、下单、回测并自审查。详见 [Agent 接口](#agent-接口)。

## 功能

- **模拟交易** — 多账户与 sleeve(按策略隔离资金、持仓与盈亏归因)、市价/限价单、A 股一手 100 股、手续费/印花税/自适应滑点、一键平仓。
- **交易前风控** — 账户与 sleeve 两级限额(单笔金额、敞口、单标的持仓、现金缓冲、下单频率),超限拒单并写入审计。
- **策略与择时** — 导入任意形态的 Python 策略文件(自动适配 `on_bar`、信号函数或类),择时 gate 控制选股策略是否开仓。
- **隔离回测** — 选策略、区间、摩擦、基准,一键产出净值/基准/回撤与整套指标,无前视,支持导出 CSV/JSON。
- **绩效分析** — 从账本重建净值曲线,quantstats 风格 tearsheet:净值、回撤、滚动夏普、月度收益热力图,以及相对沪深300 的超额、Beta、Alpha、信息比率。
- **国债逆回购** — 闲置现金按交易日计息,独立账本与面板。
- **审计链** — 信号 → 择时 → 风控 → 订单 → 成交 → 现金 → 持仓 → 净值,任一事件可下钻溯源,可导出。
- **实时调度** — 按交易时段轮询执行"择时 → 选股 → broker → 审计"循环。

## 数据源

| 数据源 | 频率 | 说明 |
| --- | --- | --- |
| tongdaxin | 1m / 5m / 1d | 免密钥拉实时 K 线(mootdx);历史深度有限 |
| ricequant | 1m / 5m / 1d | 需 license key;历史区间回测首选 |
| wind | 1d | 只读 MySQL 落地库,需内网 VPN;仅日频 |
| fixture | 1m / 5m / 1d | 内置合成行情,离线可用,供测试与演示 |

默认数据源在「数据源」页一键设置,作用于全站;各模块可单独覆盖(例如默认通达信、回测单独用 wind)。凭证存本机 `data/connector_settings.json`,已被 gitignore,不入库。

## 写策略

策略是一个 Python 文件,平台逐 bar 驱动。导入时会自动适配常见写法,不强制固定入口:

```python
def on_bar(ctx, bar):
    if bar["close"] > bar["open"]:
        ctx.order_market(bar["symbol"], 100, side="BUY", reason="momentum")
```

`ctx` 提供 `history()`、`order_market()`、`order_target_percent()`、`log()`。择时策略类似,用 `ctx.set_decision(allow_open=..., position_policy=...)` 输出门控决策。无前视:信号价取当前 close,成交价取下一根 bar 的 open。

## Agent 接口

`agent/` 是一套零依赖的 HTTP 客户端,让任意 agent 用代码或命令行驱动模拟盘:

- `paper_trading_client.py` — Python SDK,覆盖账户、下单、策略、回测、绩效、审计。
- `cli.py` — 命令行;旗舰命令 `autoreview` 一步完成"导入 → 回测 → 自审查"。
- `review.py` — 把回测结果转成量化视角的评价(总评、红旗、建议)。
- `SKILL.md` — 技能说明书。

```bash
python3 agent/cli.py autoreview --name 动量 --file my_strategy.py \
  --symbols 000001.SZ --start 2024-01-01 --end 2025-01-01 --data-source ricequant
```

`GET /api/meta` 返回版本与能力清单,供 agent 做兼容判断;破坏性改动才升 `api_version`。

## 打包与分发

```bash
./build_app.sh             # 完整版,含真实数据源依赖
PT_LEAN=1 ./build_app.sh   # 精简版,仅 fixture,体积小
```

产出 macOS `.app` / Windows 目录与可分享 zip;数据与程序分离,更新应用不动数据。打 `v*` 标签时 GitHub Actions 自动构建双平台并发布到 Releases。细节见 [BUILD.md](BUILD.md)。

## 开发

```bash
python3 -m unittest discover -s tests
```

- 数据目录由 `PAPER_TRADING_HOME` 决定(环境变量 > 指针文件 `~/.papertrading/home` > 默认),也可在「数据源」页可视化切换。
- `PORT`(默认 8000)、`HOST`(默认 127.0.0.1)。`PT_DEFAULT_DATA_SOURCE` 可覆盖默认数据源。
- 技术栈:Python 标准库 `http.server` + SQLite(WAL);前端原生 JS + TradingView lightweight-charts,无构建步骤。

## 说明

模拟盘不接真实券商,仅用于研究与策略验证。通达信 / mootdx 为非官方行情链路,正式研究结论请交叉校验 Wind、Choice 或交易所数据。
