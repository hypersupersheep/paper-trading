const state = {
  events: [],
  orders: [],
  portfolio: null,
  accounts: [],
  strategies: [],
  runs: [],
  timingStrategies: [],
  timingRuns: [],
  timingBindings: [],
  timingDecisions: [],
  schedulerTasks: [],
  schedulerTicks: [],
  riskConfigs: [],
  connectors: [],
  watchlist: [],
  ticketLast: 0,
  performance: null,
  backtest: null,
  backtestRuns: [],
  currentBacktestId: null,
  strategySourceFilename: null,
  timingSourceFilename: null,
  chart: {
    bars: [],
    markers: [],
    hoverIndex: null,
  },
  selectedId: null,
  view: "overview",
};

// 侧边栏导航：每个 data-view 区块属于一个视图，切换时只显示当前视图。
const VIEW_META = {
  overview: ["组合概览", "账户净值 · sleeve 表现 · 持仓"],
  performance: ["绩效分析", "净值 · 回撤 · 滚动夏普 · 月度收益"],
  backtest: ["策略回测", "选策略 · 区间 · 摩擦 · 基准 → 一键回测"],
  strategy: ["策略工作台", "导入 Python 选股策略并回放运行"],
  timing: ["择时工作台", "导入择时策略,gate 控制选股开仓"],
  trading: ["模拟交易", "账户 · 资金 · 下单 · 风控 · 订单簿"],
  scheduler: ["实时调度", "按交易时段轮询:择时 → 选股 → broker → 审计"],
  replay: ["Audit & Replay Log", "信号 → 择时 → 风控 → 订单 → 成交 → 现金 → 持仓 → 净值"],
  data: ["数据源", "connector 健康状态与实时行情快照"],
};

function switchView(view) {
  if (!VIEW_META[view]) return;
  state.view = view;
  // 只隐藏 main 内的内容区块；sidebar 的 nav 按钮也带 data-view，不能动。
  for (const element of document.querySelectorAll("main [data-view]")) {
    element.hidden = element.dataset.view !== view;
  }
  for (const button of document.querySelectorAll("#sidebarNav button")) {
    button.classList.toggle("active", button.dataset.view === view);
  }
  const [title, subtitle] = VIEW_META[view];
  $("viewTitle").textContent = title;
  $("viewSubtitle").textContent = subtitle;
  // 图表在 hidden 容器里宽度为 0，回到视图后需等 ResizeObserver 应用新宽度再 fitContent，
  // 否则曲线会挤在右侧。延迟一帧重新自适应。
  if (view === "replay") {
    renderChart();
    setTimeout(() => chartState.chart && chartState.chart.timeScale().fitContent(), 60);
  }
  if (view === "performance") {
    renderPerfCharts(state.performance);
    setTimeout(() => {
      if (perfCharts.equity) {
        perfCharts.equity.chart.timeScale().fitContent();
        perfCharts.drawdown.chart.timeScale().fitContent();
      }
    }, 60);
  }
  if (view === "backtest" && state.backtest) {
    renderBtChart(state.backtest);
    setTimeout(() => btChartState.chart && btChartState.chart.timeScale().fitContent(), 60);
  }
  // 回测与模拟盘是分开的:回测页不显示"当前在跑账户"的账户条(选择器+实时指标)。
  const hideAccount = view === "backtest";
  document.querySelector(".account-switch").style.display = hideAccount ? "none" : "";
  document.querySelector(".ticker-strip").style.display = hideAccount ? "none" : "";
}

const $ = (id) => document.getElementById(id);

function queryParams() {
  const params = new URLSearchParams();
  const fields = [
    ["account_id", $("accountFilter").value.trim()],
    ["strategy_id", $("strategyFilter").value.trim()],
    ["symbol", $("symbolFilter").value.trim()],
    ["event_type", $("eventTypeFilter").value],
  ];
  for (const [key, value] of fields) {
    if (value) params.set(key, value);
  }
  return params;
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function loadEvents() {
  const params = queryParams();
  const data = await fetchJson(`/api/audit/events?${params}`);
  state.events = data.events;
  renderMetrics();
  renderTable();
  if (state.events.length && !state.selectedId) {
    selectEvent(state.events[0].id);
  }
}

async function loadOrders() {
  const params = new URLSearchParams({ limit: "30" });
  const accountId = $("accountFilter").value.trim();
  const symbol = $("symbolFilter").value.trim().toUpperCase();
  if (accountId) params.set("account_id", accountId);
  if (symbol) params.set("symbol", symbol);
  const data = await fetchJson(`/api/broker/orders?${params}`);
  state.orders = data.orders;
  renderOrderBook();
}

async function loadPortfolio() {
  const accountId = $("accountFilter").value.trim() || state.accounts[0]?.id || "";
  const params = new URLSearchParams();
  if (accountId) params.set("account_id", accountId);
  params.set("data_source", $("portfolioDataSource").value || "fixture");
  params.set("frequency", $("portfolioFrequency").value || "5m");
  const data = await fetchJson(`/api/portfolio/summary?${params}`);
  state.portfolio = data;
  renderPortfolio();
  renderStrategyBoard();
}

async function loadAccounts() {
  const data = await fetchJson("/api/accounts");
  state.accounts = data.accounts;
  renderAccountControls();
}

async function loadStrategies() {
  const data = await fetchJson("/api/strategies");
  state.strategies = data.strategies;
  state.runs = data.runs;
  const connectorData = await fetchJson("/api/data/connectors/health");
  state.connectors = connectorData.connectors;
  if (state.defaultDataSource === undefined) {
    try {
      state.defaultDataSource = (await fetchJson("/api/settings/data-source")).default_data_source;
    } catch (error) {
      state.defaultDataSource = "tongdaxin";
    }
  }
  renderStrategyControls();
}

async function loadTiming() {
  const data = await fetchJson("/api/timing-strategies");
  state.timingStrategies = data.timing_strategies;
  state.timingRuns = data.runs;
  state.timingBindings = data.bindings;
  state.timingDecisions = data.decisions;
  renderTimingControls();
}

async function loadScheduler() {
  const data = await fetchJson("/api/scheduler/tasks");
  state.schedulerTasks = data.tasks;
  state.schedulerTicks = data.ticks;
  renderSchedulerControls();
}

async function loadRiskConfigs() {
  const data = await fetchJson("/api/risk/configs");
  state.riskConfigs = data.configs;
  renderRiskControls();
}

// ============================ 绩效 tearsheet ============================
const perfCharts = { equity: null, drawdown: null, rolling: null };

async function loadPerformance() {
  const accountId = $("accountFilter").value.trim() || state.accounts[0]?.id || "";
  const markSource = $("perfBenchSource").value || ticketSource();
  // 净值曲线按真实账本重建,盯市用该数据源(基准也用它)。逆回购的幂等补全统一在 loadReverseRepo 里做。
  const params = new URLSearchParams();
  if (accountId) params.set("account_id", accountId);
  params.set("data_source", markSource);
  params.set("benchmark_source", markSource);
  const data = await fetchJson(`/api/portfolio/performance?${params}`);
  state.performance = data;
  renderPerfMetrics(data);
  renderPerfCharts(data);
}

function renderPerfMetrics(data) {
  const m = data.metrics || {};
  const num = (value, digits = 2) => (value === undefined || value === null ? "--" : Number(value).toFixed(digits));
  const bench = data.benchmark;
  const bm = (bench && bench.metrics) || {};
  // 按 quantstats 思路分三组:收益 / 风险 / 相对基准。
  const groups = [
    {
      title: "收益",
      cards: [
        { label: "累计收益", value: formatPercent(m.cumulative_return), cls: numberClass(m.cumulative_return) },
        { label: "年化收益", value: formatPercent(m.annualized_return), cls: numberClass(m.annualized_return) },
        { label: "成交笔数", value: formatNumber(data.trade_count) },
        { label: "交易天数", value: formatNumber(m.trading_days) },
      ],
    },
    {
      title: "风险",
      cards: [
        { label: "最大回撤", value: formatPercent(m.max_drawdown), cls: "negative" },
        { label: "年化波动", value: formatPercent(m.annualized_volatility) },
        { label: "夏普比率", value: num(m.sharpe), cls: numberClass(m.sharpe) },
        { label: "Calmar", value: num(m.calmar), cls: numberClass(m.calmar) },
        { label: "日胜率", value: formatPercent(m.daily_win_rate) },
        { label: "盈亏比", value: num(m.profit_loss_ratio) },
      ],
    },
  ];
  if (bench && bench.metrics && Object.keys(bm).length) {
    groups.push({
      title: `相对基准 (vs ${bench.symbol})`,
      cards: [
        { label: "基准累计", value: formatPercent(bm.benchmark_cumulative), cls: numberClass(bm.benchmark_cumulative) },
        { label: "超额收益", value: formatPercent(bm.excess_return), cls: numberClass(bm.excess_return) },
        { label: "Beta", value: num(bm.beta) },
        { label: "年化 Alpha", value: formatPercent(bm.alpha_annualized), cls: numberClass(bm.alpha_annualized) },
        { label: "信息比率", value: num(bm.information_ratio), cls: numberClass(bm.information_ratio) },
        { label: "跑赢基准天数", value: formatPercent(bm.win_vs_benchmark) },
      ],
    });
  }
  $("perfMetrics").innerHTML = groups
    .map(
      (g) => `<div class="perf-group">
        <h4>${g.title}</h4>
        <div class="perf-cards">${g.cards
          .map((c) => `<div class="perf-card"><span>${c.label}</span><strong class="${c.cls || ""}">${c.value}</strong></div>`)
          .join("")}</div>
      </div>`,
    )
    .join("");
  $("perfSubtitle").textContent = `${data.account_name || ""} · ${data.points || 0} 个净值点 · 起始 ¥${formatNumber(m.start_equity)} → 当前 ¥${formatNumber(m.end_equity)}`;

  if (bench && bench.series && bench.series.length) {
    $("perfLegend").innerHTML =
      `<i><span class="dot" style="background:#2f81f7"></span>策略净值</i><i><span class="dot" style="background:#8b94a3"></span>${bench.symbol} 基准</i>`;
  } else {
    $("perfLegend").innerHTML = bench && bench.error
      ? "<i>基准不可用（通达信取不到指数日线，把右上「基准源」改为 ricequant 即可叠加沪深300，盯市也更准）</i>"
      : "<i>基准未对齐（把「基准源」换成 ricequant 可叠加沪深300）</i>";
  }
}

// 金额简写:1.23亿 / 4560.0万 / 1234,便于 Y 轴与提示读数(学专业 tearsheet)。
function formatWan(value) {
  const v = Number(value) || 0;
  const abs = Math.abs(v);
  if (abs >= 1e8) return (v / 1e8).toFixed(2) + "亿";
  if (abs >= 1e4) return (v / 1e4).toFixed(1) + "万";
  return v.toFixed(0);
}

function chartLayout(extra) {
  return {
    autoSize: true,
    layout: {
      background: { type: "solid", color: "#0e1117" },
      textColor: "#9aa4b2",
      fontFamily: '"SF Mono", "JetBrains Mono", monospace',
      fontSize: 11,
      ...extra,
    },
    grid: { vertLines: { color: "rgba(33,38,45,0.6)" }, horzLines: { color: "rgba(33,38,45,0.6)" } },
    rightPriceScale: { borderColor: "#21262d", scaleMargins: { top: 0.12, bottom: 0.12 } },
    timeScale: { borderColor: "#21262d", fixLeftEdge: true, fixRightEdge: true },
    crosshair: {
      mode: 1,
      vertLine: { color: "rgba(120,130,145,0.5)", width: 1, style: 2, labelBackgroundColor: "#2f81f7" },
      horzLine: { color: "rgba(120,130,145,0.5)", width: 1, style: 2, labelBackgroundColor: "#2f81f7" },
    },
  };
}

// 十字光标悬浮卡片:专业 tearsheet 的核心可读性。
function attachChartTooltip(chart, container, render) {
  const tip = document.createElement("div");
  tip.className = "chart-tooltip";
  tip.style.display = "none";
  container.style.position = "relative";
  container.appendChild(tip);
  chart.subscribeCrosshairMove((param) => {
    if (!param.time || !param.point || param.point.x < 0 || param.point.y < 0) {
      tip.style.display = "none";
      return;
    }
    const html = render(param);
    if (!html) {
      tip.style.display = "none";
      return;
    }
    tip.innerHTML = html;
    tip.style.display = "block";
    const x = Math.min(param.point.x + 16, container.clientWidth - 150);
    tip.style.left = Math.max(8, x) + "px";
    tip.style.top = "8px";
  });
}

function ensurePerfCharts() {
  if (perfCharts.equity || typeof LightweightCharts === "undefined") return;
  const equityChart = LightweightCharts.createChart(
    $("perfEquityChart"),
    chartLayout({ localization: { priceFormatter: formatWan } }),
  );
  // 净值用 Baseline:高于初始资金=红(盈),低于=绿(亏)——A股红涨绿跌,一眼读盈亏。
  const equitySeries = equityChart.addSeries(LightweightCharts.BaselineSeries, {
    baseValue: { type: "price", price: 0 },
    topLineColor: "#f23645",
    topFillColor1: "rgba(242,54,69,0.28)",
    topFillColor2: "rgba(242,54,69,0.02)",
    bottomLineColor: "#089981",
    bottomFillColor1: "rgba(8,153,129,0.02)",
    bottomFillColor2: "rgba(8,153,129,0.28)",
    lineWidth: 2,
    priceFormat: { type: "custom", formatter: formatWan, minMove: 1 },
  });
  // 基准线(沪深300)归一化到策略起点,灰色细虚线。
  const benchSeries = equityChart.addSeries(LightweightCharts.LineSeries, {
    color: "#8b94a3",
    lineWidth: 1,
    lineStyle: 2,
    priceFormat: { type: "custom", formatter: formatWan, minMove: 1 },
    crosshairMarkerVisible: false,
    lastValueVisible: false,
    priceLineVisible: false,
  });
  perfCharts.equity = { chart: equityChart, series: equitySeries, bench: benchSeries, base: 0 };
  attachChartTooltip(equityChart, $("perfEquityChart"), (param) => {
    const pt = param.seriesData.get(equitySeries);
    if (!pt) return "";
    const eq = pt.value;
    const base = perfCharts.equity.base || eq;
    const ret = base ? eq / base - 1 : 0;
    const benchPt = param.seriesData.get(benchSeries);
    const benchRow = benchPt ? `<div><span>基准</span><b>${formatWan(benchPt.value)}</b></div>` : "";
    return `<div class="tt-date">${param.time}</div>
      <div><span>净值</span><b>${formatNumber(eq)}</b></div>
      <div><span>收益</span><b class="${ret >= 0 ? "positive" : "negative"}">${formatPercent(ret)}</b></div>${benchRow}`;
  });

  const ddChart = LightweightCharts.createChart($("perfDrawdownChart"), chartLayout());
  const ddSeries = ddChart.addSeries(LightweightCharts.AreaSeries, {
    lineColor: "#e0405a",
    topColor: "rgba(224,64,90,0.04)",
    bottomColor: "rgba(224,64,90,0.40)",
    lineWidth: 1.5,
    priceFormat: { type: "custom", formatter: (v) => v.toFixed(1) + "%", minMove: 0.01 },
  });
  perfCharts.drawdown = { chart: ddChart, series: ddSeries };
  attachChartTooltip(ddChart, $("perfDrawdownChart"), (param) => {
    const pt = param.seriesData.get(ddSeries);
    if (!pt) return "";
    return `<div class="tt-date">${param.time}</div><div><span>回撤</span><b class="negative">${pt.value.toFixed(2)}%</b></div>`;
  });

  // 滚动夏普:蓝线 + 0 参考线(>0 越高越稳健)。
  const rollChart = LightweightCharts.createChart($("perfRollingChart"), chartLayout());
  const rollSeries = rollChart.addSeries(LightweightCharts.LineSeries, {
    color: "#2f81f7",
    lineWidth: 2,
    priceFormat: { type: "custom", formatter: (v) => v.toFixed(2), minMove: 0.01 },
    crosshairMarkerVisible: true,
  });
  rollSeries.createPriceLine({ price: 0, color: "#565d68", lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
  perfCharts.rolling = { chart: rollChart, series: rollSeries };
  attachChartTooltip(rollChart, $("perfRollingChart"), (param) => {
    const pt = param.seriesData.get(rollSeries);
    if (!pt) return "";
    return `<div class="tt-date">${param.time}</div><div><span>滚动夏普</span><b class="${pt.value >= 0 ? "positive" : "negative"}">${pt.value.toFixed(2)}</b></div>`;
  });
}

// 从净值曲线算滚动 N 日年化夏普。
function computeRollingSharpe(curve, window = 30) {
  if (!curve || curve.length < window + 2) return [];
  const rets = [];
  for (let i = 1; i < curve.length; i++) {
    const a = curve[i - 1].equity;
    const b = curve[i].equity;
    rets.push({ time: curve[i].time, r: a ? b / a - 1 : 0 });
  }
  const out = [];
  for (let i = window - 1; i < rets.length; i++) {
    const win = rets.slice(i - window + 1, i + 1).map((x) => x.r);
    const mean = win.reduce((s, v) => s + v, 0) / win.length;
    const variance = win.reduce((s, v) => s + (v - mean) ** 2, 0) / win.length;
    const std = Math.sqrt(variance);
    const sharpe = std > 0 ? (mean / std) * Math.sqrt(252) : 0;
    out.push({ time: rets[i].time, value: round2(sharpe) });
  }
  return out;
}

// 从净值曲线算每月收益(以上月末净值为基);年度=当年各月复利。
function computeMonthlyReturns(curve) {
  if (!curve || curve.length < 2) return { years: [], byYear: {}, yearTotal: {} };
  const monthEnd = new Map();
  for (const p of curve) monthEnd.set(p.time.slice(0, 7), p.equity);
  const keys = [...monthEnd.keys()].sort();
  let prev = curve[0].equity;
  const byYear = {};
  const yearChain = {};
  for (const k of keys) {
    const eq = monthEnd.get(k);
    const ret = prev ? eq / prev - 1 : 0;
    const [y, mo] = k.split("-");
    (byYear[y] ||= {})[parseInt(mo, 10)] = ret;
    yearChain[y] = (yearChain[y] || 1) * (1 + ret);
    prev = eq;
  }
  const yearTotal = {};
  for (const y of Object.keys(yearChain)) yearTotal[y] = yearChain[y] - 1;
  return { years: Object.keys(byYear).sort(), byYear, yearTotal };
}

function heatCell(ret) {
  if (ret === undefined || ret === null) return '<td class="mr-empty">·</td>';
  const a = Math.min(0.82, Math.abs(ret) * 6 + 0.08); // 幅度越大越深
  const color = ret >= 0 ? `rgba(242,54,69,${a})` : `rgba(8,153,129,${a})`;
  return `<td style="background:${color}">${(ret * 100).toFixed(1)}</td>`;
}

function renderMonthlyHeatmap(curve) {
  const el = $("perfMonthly");
  const { years, byYear, yearTotal } = computeMonthlyReturns(curve);
  if (!years.length) {
    el.innerHTML = '<div class="perf-sub">样本不足(需跨月数据)</div>';
    return;
  }
  const head =
    "<tr><th>年</th>" +
    Array.from({ length: 12 }, (_, i) => `<th>${i + 1}月</th>`).join("") +
    "<th>年度</th></tr>";
  const rows = years
    .map((y) => {
      const cells = Array.from({ length: 12 }, (_, i) => heatCell(byYear[y][i + 1])).join("");
      const yt = yearTotal[y];
      const ytColor = yt >= 0 ? "var(--gain)" : "var(--loss)";
      return `<tr><th>${y}</th>${cells}<td class="mr-total" style="color:${ytColor}">${(yt * 100).toFixed(1)}%</td></tr>`;
    })
    .join("");
  el.innerHTML = `<table class="monthly-table"><thead>${head}</thead><tbody>${rows}</tbody></table>`;
}

function renderPerfCharts(data) {
  ensurePerfCharts();
  if (!perfCharts.equity || !data) return;
  const curve = data.curve || [];
  const base = data.metrics?.initial_cash || curve[0]?.equity || 0;
  perfCharts.equity.base = base;
  perfCharts.equity.series.applyOptions({ baseValue: { type: "price", price: base } });
  perfCharts.equity.series.setData(curve.map((point) => ({ time: point.time, value: point.equity })));
  perfCharts.drawdown.series.setData(curve.map((point) => ({ time: point.time, value: round2(point.drawdown * 100) })));
  const benchSeries = data.benchmark?.series || [];
  perfCharts.equity.bench.setData(benchSeries.map((point) => ({ time: point.time, value: point.value })));
  perfCharts.equity.chart.timeScale().fitContent();
  perfCharts.drawdown.chart.timeScale().fitContent();

  // 滚动夏普
  const rolling = computeRollingSharpe(curve, 30);
  perfCharts.rolling.series.setData(rolling);
  perfCharts.rolling.chart.timeScale().fitContent();
  $("perfRollingNote").textContent = rolling.length
    ? `共 ${rolling.length} 个滚动点 · 最新 ${rolling[rolling.length - 1].value.toFixed(2)}`
    : "样本不足:需 ≥32 个净值点才能算 30 日滚动夏普";

  // 月度收益热力图
  renderMonthlyHeatmap(curve);
}

function round2(value) {
  return Math.round(value * 100) / 100;
}

async function recordSnapshot() {
  const accountId = $("accountFilter").value.trim() || state.accounts[0]?.id || "";
  await postJson("/api/portfolio/snapshot", { account_id: accountId });
  showToast("已记录当前净值快照");
  await loadPerformance();
}

// ============================ 策略回测 ============================
const btChartState = { chart: null, equity: null, bench: null, dd: null };

async function runBacktest(event) {
  event.preventDefault();
  const payload = {
    name: $("btName").value.trim(),
    strategy_id: $("btStrategy").value,
    timing_strategy_id: $("btTiming").value || null,
    symbols: $("btSymbols").value,
    data_source: $("btDataSource").value,
    frequency: $("btFrequency").value,
    start: $("btStart").value || "",
    end: $("btEnd").value || "",
    initial_cash: Number($("btInitialCash").value),
    commission_rate: Number($("btCommission").value),
    stamp_duty_rate: Number($("btStamp").value),
    slippage_model: $("btSlippageModel").value,
    slippage_value: Number($("btSlippage").value),
    benchmark: $("btBenchmark").value.trim(),
    benchmark_source: $("btBenchSource").value,
  };
  if (!payload.strategy_id) {
    showToast("请先在「策略」页导入选股策略");
    return;
  }
  $("btRun").disabled = true;
  $("btRun").textContent = "回测运行中…";
  try {
    const data = await postJson("/api/backtest/run", payload);
    renderBacktestResult(data);
    showToast(`回测完成：累计 ${formatPercent(data.metrics.cumulative_return)}，${data.summary.total_trades} 笔成交`);
    await loadBacktestRuns();
  } finally {
    $("btRun").disabled = false;
    $("btRun").textContent = "运行回测";
  }
}

function renderBacktestResult(result) {
  state.backtest = result;
  state.currentBacktestId = result.id;
  const m = result.metrics || {};
  const s = result.summary || {};
  const num = (value, digits = 2) => (value === undefined || value === null ? "--" : Number(value).toFixed(digits));
  const cards = [
    { label: "累计收益", value: formatPercent(m.cumulative_return), cls: numberClass(m.cumulative_return) },
    { label: "年化收益", value: formatPercent(m.annualized_return), cls: numberClass(m.annualized_return) },
    { label: "夏普比率", value: num(m.sharpe) },
    { label: "最大回撤", value: formatPercent(m.max_drawdown) },
    { label: "年化波动", value: formatPercent(m.annualized_volatility) },
    { label: "末净值", value: `¥${formatNumber(s.final_equity)}` },
    { label: "成交笔数", value: formatNumber(s.total_trades) },
    { label: "交易胜率", value: formatPercent(s.trade_win_rate) },
    { label: "已实现盈亏", value: formatNumber(s.total_realized_pnl), cls: numberClass(s.total_realized_pnl) },
    { label: "被拒订单", value: formatNumber(s.rejected_orders) },
  ];
  const bench = result.benchmark;
  if (bench && bench.metrics && Object.keys(bench.metrics).length) {
    const bm = bench.metrics;
    cards.push(
      { label: `超额收益(vs ${bench.symbol})`, value: formatPercent(bm.excess_return), cls: numberClass(bm.excess_return) },
      { label: "Beta", value: num(bm.beta) },
      { label: "信息比率", value: num(bm.information_ratio) },
    );
  }
  $("btMetrics").innerHTML = cards
    .map((card) => `<div class="perf-card"><span>${card.label}</span><strong class="${card.cls || ""}">${card.value}</strong></div>`)
    .join("");
  const timingNote = s.timing_strategy_id
    ? ` · 择时 ${findTimingName(s.timing_strategy_id)}(${s.timing_decisions} 决策)`
    : " · 无择时";
  $("btResultSub").textContent =
    `${result.params?.strategy_name || result.params?.strategy_id || ""} · ${(s.symbols || []).join(",")} · ${s.start || ""}~${s.end || ""} · ${(result.curve || []).length} 日${timingNote}`;
  $("btLegend").innerHTML = bench && bench.series && bench.series.length
    ? `<i><span class="dot" style="background:#2f81f7"></span>策略净值</i><i><span class="dot" style="background:#8b94a3"></span>${bench.symbol} 基准</i><i><span class="dot" style="background:#d29922"></span>回撤</i>`
    : `<i><span class="dot" style="background:#2f81f7"></span>策略净值</i><i><span class="dot" style="background:#d29922"></span>回撤</i>`;

  renderBtChart(result);

  const trades = result.trades || [];
  $("btTradesSub").textContent = `${trades.length} 笔`;
  $("btTrades").innerHTML =
    trades
      .slice(-300)
      .reverse()
      .map(
        (trade) => `
        <tr>
          <td>${formatTime(trade.timestamp)}</td>
          <td>${trade.symbol}</td>
          <td class="${trade.side === "BUY" ? "positive" : "negative"}">${trade.side === "BUY" ? "买入" : "卖出"}</td>
          <td>${formatNumber(trade.quantity)}</td>
          <td>${formatNumber(trade.price)}</td>
          <td>${formatNumber(trade.commission)}</td>
          <td>${formatNumber(trade.stamp_duty)}</td>
          <td class="${numberClass(trade.realized)}">${trade.side === "SELL" ? formatNumber(trade.realized) : "--"}</td>
        </tr>`,
      )
      .join("") || '<tr class="empty"><td colspan="8">无成交</td></tr>';
}

function ensureBtChart() {
  if (btChartState.chart || typeof LightweightCharts === "undefined") return;
  const layout = chartLayout({ localization: { priceFormatter: formatWan } });
  layout.rightPriceScale = { borderColor: "#21262d", scaleMargins: { top: 0.08, bottom: 0.3 } };
  const chart = LightweightCharts.createChart($("btChart"), layout);
  const equity = chart.addSeries(LightweightCharts.BaselineSeries, {
    baseValue: { type: "price", price: 0 },
    topLineColor: "#f23645", topFillColor1: "rgba(242,54,69,0.26)", topFillColor2: "rgba(242,54,69,0.02)",
    bottomLineColor: "#089981", bottomFillColor1: "rgba(8,153,129,0.02)", bottomFillColor2: "rgba(8,153,129,0.26)",
    lineWidth: 2, priceFormat: { type: "custom", formatter: formatWan, minMove: 1 },
  });
  const bench = chart.addSeries(LightweightCharts.LineSeries, {
    color: "#8b94a3", lineWidth: 1, lineStyle: 2, priceFormat: { type: "custom", formatter: formatWan, minMove: 1 },
    crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
  });
  // 回撤画在同一张图的底部子区(独立价格轴),"净值+基准+回撤一图看全"。
  const dd = chart.addSeries(LightweightCharts.AreaSeries, {
    lineColor: "#e0405a", topColor: "rgba(224,64,90,0.02)", bottomColor: "rgba(224,64,90,0.4)",
    lineWidth: 1, priceScaleId: "dd", priceFormat: { type: "custom", formatter: (v) => v.toFixed(1) + "%", minMove: 0.01 },
  });
  dd.priceScale().applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } });
  btChartState.chart = chart;
  btChartState.equity = equity;
  btChartState.bench = bench;
  btChartState.dd = dd;
  btChartState.base = 0;
  attachChartTooltip(chart, $("btChart"), (param) => {
    const pt = param.seriesData.get(equity);
    if (!pt) return "";
    const base = btChartState.base || pt.value;
    const ddPt = param.seriesData.get(dd);
    const ddRow = ddPt ? `<div><span>回撤</span><b class="negative">${ddPt.value.toFixed(2)}%</b></div>` : "";
    return `<div class="tt-date">${param.time}</div>
      <div><span>净值</span><b>${formatNumber(pt.value)}</b></div>
      <div><span>收益</span><b class="${pt.value >= base ? "positive" : "negative"}">${formatPercent(base ? pt.value / base - 1 : 0)}</b></div>${ddRow}`;
  });
}

function renderBtChart(result) {
  ensureBtChart();
  if (!btChartState.chart || !result) return;
  const curve = result.curve || [];
  const base = result.summary?.initial_cash || curve[0]?.equity || 0;
  btChartState.base = base;
  btChartState.equity.applyOptions({ baseValue: { type: "price", price: base } });
  btChartState.equity.setData(curve.map((point) => ({ time: point.time, value: point.equity })));
  btChartState.dd.setData(curve.map((point) => ({ time: point.time, value: round2((point.drawdown || 0) * 100) })));
  const benchSeries = result.benchmark?.series || [];
  btChartState.bench.setData(benchSeries.map((point) => ({ time: point.time, value: point.value })));
  btChartState.chart.timeScale().fitContent();
}

async function loadBacktestRuns() {
  const data = await fetchJson("/api/backtest/runs");
  state.backtestRuns = data.runs;
  renderBtHistory();
}

function renderBtHistory() {
  $("btHistory").innerHTML =
    state.backtestRuns
      .map((run) => {
        const s = run.summary || {};
        return `<div class="bt-history-row" data-id="${run.id}">
          <strong>${run.name}</strong>
          <span>累计 ${formatPercent(s.cumulative_return)} · 夏普 ${(s.sharpe ?? 0).toFixed(2)} · ${formatTime(run.created_at).slice(0, 16)}</span>
        </div>`;
      })
      .join("") || '<div class="bt-history-row"><span>暂无历史回测，运行后显示</span></div>';
}

async function loadBacktest(id) {
  const data = await fetchJson(`/api/backtest/${encodeURIComponent(id)}`);
  renderBacktestResult(data);
  showToast("已载入历史回测");
}

function exportBacktest(format) {
  if (!state.currentBacktestId) {
    showToast("请先运行回测");
    return;
  }
  window.location.href = `/api/backtest/${encodeURIComponent(state.currentBacktestId)}/export?format=${format}`;
}

// ============================ 交易工作区 ============================
// 全站所有"数据源/基准源"下拉的 id(默认数据源选择器除外)。一键全改时统一设置这些。
const DATA_SOURCE_SELECTS = [
  "strategyDataSource", "chartDataSource", "portfolioDataSource", "orderDataSource",
  "perfBenchSource", "btDataSource", "btBenchSource", "quoteDataSource",
  "timingRunDataSource", "schedulerDataSource",
];

function effectiveDefaultSource() {
  return state.defaultDataSource || "tongdaxin";
}

function ticketSource() {
  // 交易票据/盯市用全局默认数据源(各视图下拉若用户单独改过,以各自下拉为准)。
  return effectiveDefaultSource();
}

async function loadWatchlist() {
  const data = await fetchJson(`/api/watchlist?data_source=${encodeURIComponent(ticketSource())}&frequency=1d`);
  state.watchlist = data.symbols;
  renderWatchlist();
}

function renderWatchlist() {
  const container = $("watchlist");
  if (!state.watchlist.length) {
    container.innerHTML = '<div class="watchlist-row"><span class="wl-sym">空</span><span></span><span></span></div>';
    return;
  }
  const current = ($("orderSymbol")?.value || "").trim().toUpperCase();
  container.innerHTML = state.watchlist
    .map((quote) => {
      const cls = quote.error ? "" : numberClass(quote.change);
      const chg = quote.error ? "—" : `${quote.change >= 0 ? "+" : ""}${formatPercent(quote.change_pct)}`;
      const last = quote.error ? "--" : formatNumber(quote.last);
      return `
        <div class="watchlist-row ${quote.symbol === current ? "active" : ""}" data-symbol="${quote.symbol}">
          <span class="wl-sym">${quote.symbol}</span>
          <span class="wl-last">${last}</span>
          <span class="wl-chg ${cls}">${chg}</span>
          <button type="button" class="wl-remove" data-action="remove" title="移除">×</button>
        </div>`;
    })
    .join("");
}

async function handleWatchlistClick(event) {
  const row = event.target.closest(".watchlist-row");
  if (!row || !row.dataset.symbol) return;
  const symbol = row.dataset.symbol;
  if (event.target.closest("button[data-action='remove']")) {
    await postJson("/api/watchlist", { symbol, action: "remove" });
    await loadWatchlist();
    return;
  }
  await selectTicketSymbol(symbol);
  $("chartSymbol").value = symbol;
}

async function addWatchlist(event) {
  event.preventDefault();
  const symbol = $("watchlistInput").value.trim().toUpperCase();
  if (!symbol) return;
  await postJson("/api/watchlist", { symbol });
  $("watchlistInput").value = "";
  await loadWatchlist();
}

// ---- 交易 ticket ----
function setTicketSide(side) {
  $("orderSide").value = side;
  for (const button of document.querySelectorAll(".side-toggle button")) {
    button.classList.toggle("active", button.dataset.side === side);
  }
  const submit = $("ticketSubmit");
  submit.classList.toggle("buy", side === "BUY");
  submit.classList.toggle("sell", side === "SELL");
  submit.textContent = side === "BUY" ? "买入下单" : "卖出下单";
}

function togglePriceType() {
  const isLimit = $("orderPriceType").value === "limit";
  $("orderSignalPrice").disabled = !isLimit;
  if (!isLimit) $("orderSignalPrice").value = "";
  updateTicketEstimate();
}

function ticketContext() {
  const account = state.accounts.find((item) => item.id === $("orderAccount").value);
  const sleeve = account?.sleeves.find((item) => item.id === $("orderSleeve").value);
  const symbol = ($("orderSymbol").value || "").trim().toUpperCase();
  const price = Number($("orderSignalPrice").value) || state.ticketLast || 0;
  return { account, sleeve, symbol, price };
}

async function selectTicketSymbol(symbol) {
  $("orderSymbol").value = symbol;
  renderWatchlist();
  await refreshTicketQuote();
}

async function refreshTicketQuote() {
  const symbol = ($("orderSymbol").value || "").trim().toUpperCase();
  if (!symbol) return;
  try {
    const data = await fetchJson(`/api/quotes?symbols=${encodeURIComponent(symbol)}&data_source=${encodeURIComponent(ticketSource())}`);
    const quote = data.quotes[0];
    if (quote && !quote.error) {
      state.ticketLast = quote.last;
      $("ticketLast").innerHTML =
        `现价 <b class="${numberClass(quote.change)}">${formatNumber(quote.last)}</b> (${quote.change >= 0 ? "+" : ""}${formatPercent(quote.change_pct)})`;
    } else {
      state.ticketLast = 0;
      $("ticketLast").textContent = "现价 --";
    }
  } catch {
    state.ticketLast = 0;
    $("ticketLast").textContent = "现价 --";
  }
  updateTicketEstimate();
}

function ticketMaxShares() {
  const { sleeve, price } = ticketContext();
  if (!sleeve || !price) return 0;
  // 按可用现金反推最大可买股数(留 0.1% 覆盖费用),A股 100 股整手。
  return Math.floor((sleeve.available_cash * 0.999) / price / 100) * 100;
}

function applyQuickQty(fraction) {
  const max = ticketMaxShares();
  if (!max) {
    showToast("请先选择 sleeve 并确认现价");
    return;
  }
  $("orderQuantity").value = Math.max(Math.floor((max * fraction) / 100) * 100, 0);
  updateTicketEstimate();
}

function updateTicketEstimate() {
  const { sleeve, price } = ticketContext();
  const qty = Number($("orderQuantity").value) || 0;
  const max = ticketMaxShares();
  $("ticketMaxShares").textContent = sleeve
    ? `可用 ¥${formatNumber(sleeve.available_cash)} · 最多 ${formatNumber(max)} 股`
    : "选择 sleeve 后显示可用资金";
  $("ticketEstimate").textContent = price
    ? `预估金额 ¥${formatNumber(qty * price)}`
    : "预估金额 --（市价单按最新行情成交）";
}

// ---- 持仓 blotter(一键平仓) ----
function renderBlotter() {
  const positions = state.portfolio?.accounts?.[0]?.positions || [];
  const body = $("blotter");
  if (!positions.length) {
    body.innerHTML = '<tr class="empty"><td colspan="9">暂无持仓 — 在右侧 Ticket 买入后显示</td></tr>';
    return;
  }
  body.innerHTML = positions
    .map(
      (position) => `
      <tr>
        <td class="sym">${position.name && position.name !== position.symbol ? `${position.name}<br><small>${position.symbol}</small>` : position.symbol}</td>
        <td>${findStrategyName(position.strategy_id)}</td>
        <td>${formatNumber(position.quantity)}</td>
        <td>${formatNumber(position.avg_cost)}</td>
        <td>${formatNumber(position.mark_price)}</td>
        <td>${formatNumber(position.market_value)}</td>
        <td class="${numberClass(position.unrealized_pnl)}">${formatNumber(position.unrealized_pnl)} (${formatPercent(position.unrealized_pnl_pct)})</td>
        <td>${position.volatility == null ? "--" : formatPercent(position.volatility)}</td>
        <td><button type="button" class="danger close-pos" data-sleeve="${position.sleeve_id}" data-symbol="${position.symbol}" data-qty="${position.quantity}" data-strategy="${position.strategy_id}">平仓</button></td>
      </tr>`,
    )
    .join("");
}

async function handleBlotterClick(event) {
  const button = event.target.closest("button.close-pos");
  if (!button) return;
  const { sleeve, symbol, qty, strategy } = button.dataset;
  const data = await postJson("/api/broker/orders", {
    account_id: state.portfolio?.accounts?.[0]?.id,
    sleeve_id: sleeve,
    strategy_id: strategy,
    symbol,
    side: "SELL",
    quantity: Number(qty),
    data_source: ticketSource(),
    signal_reason: "one-click close from positions blotter",
  });
  showToast(data.accepted ? `已平仓 ${symbol} ${formatNumber(Number(qty))} 股` : `平仓被拒：${data.reason}`);
  await refreshAll(data.source_event_id || data.event_id);
}

const CONNECTOR_DESCRIPTIONS = {
  fixture: "内置确定性行情，零依赖，用于策略/链路测试",
  tongdaxin: "通达信 HQ server(mootdx)，免密钥拉取 A 股实时 K 线",
  ricequant: "米筐 rqdatac，输入 license key 即连，专业数据源",
  wind: "辉隆 Wind 只读残血库(MySQL)，日频 A股/指数，需连内网 VPN",
};

function renderDataConnectors() {
  $("dataConnectors").innerHTML = state.connectors
    .map((connector) => {
      const checked = connector.checked_at
        ? `检查于 ${formatTime(connector.checked_at).slice(11, 19)} · ${connector.checked_in_ms}ms`
        : "";
      const ricequantConfig =
        connector.name === "ricequant"
          ? `
            <div class="connector-config">
              <input id="rqLicenseKey" type="password" autocomplete="off"
                placeholder="${connector.license_key_masked ? `已保存 ${connector.license_key_masked} · 重新输入可更新` : "输入米筐 license key"}" />
              <button type="button" data-action="save-rq-key" class="primary">保存并测试</button>
            </div>
          `
          : "";
      const windConfig =
        connector.name === "wind"
          ? `
            <div class="wind-config">
              <input id="windHost" placeholder="host" value="${connector.host || ""}" />
              <input id="windPort" type="number" placeholder="port" value="${connector.port || 3306}" />
              <input id="windUser" placeholder="user" value="${connector.user || ""}" />
              <input id="windPassword" type="password" autocomplete="off" placeholder="${connector.password_set ? "已保存 · 留空不改" : "password"}" />
              <input id="windDatabase" placeholder="database" value="${connector.database || "wind_data"}" />
              <button type="button" data-action="save-wind" class="primary">保存并测试</button>
            </div>
          `
          : "";
      return `
        <div class="connector-card ${connector.status}">
          <div>
            <strong>${connector.name}</strong>
            <span class="status">${connectorStatusLabel(connector.status)}</span>
          </div>
          <span>${CONNECTOR_DESCRIPTIONS[connector.name] || ""}</span>
          <span>支持频率：${(connector.supported_frequencies || []).join(" / ")}${checked ? ` · ${checked}` : ""}</span>
          ${connector.hint ? `<span class="hint">${escapeHtml(connector.hint)}</span>` : ""}
          ${connector.error ? `<span class="error">${escapeHtml(connector.error)}${connector.install ? ` · ${escapeHtml(connector.install)}` : ""}</span>` : ""}
          ${ricequantConfig}
          ${windConfig}
        </div>
      `;
    })
    .join("");
  fillConnectorSelect("quoteDataSource");
  renderDefaultSourceSelect();
}

function renderDefaultSourceSelect() {
  const select = $("defaultDataSource");
  const current = effectiveDefaultSource();
  select.innerHTML = state.connectors
    .map((connector) => `<option value="${connector.name}">${connector.name} · ${connectorStatusLabel(connector.status)}</option>`)
    .join("");
  if (state.connectors.some((connector) => connector.name === current)) select.value = current;
}

async function saveWindConfig() {
  const payload = {
    host: $("windHost").value.trim(),
    port: Number($("windPort").value) || 3306,
    user: $("windUser").value.trim(),
    password: $("windPassword").value,
    database: $("windDatabase").value.trim() || "wind_data",
  };
  if (!payload.host) {
    showToast("请填写 host");
    return;
  }
  showToast("正在连接 Wind 数据库…");
  const data = await postJson("/api/data/connectors/wind/credentials", payload);
  if (data.test?.ok) {
    const bar = data.test.sample_bar;
    showToast(`Wind 连接成功：${bar ? `${bar.symbol} ${bar.timestamp.slice(0, 10)} close ${bar.close}` : "已验证"}`);
  } else {
    showToast(`配置已保存，但连接测试失败(可能 VPN 未开)：${data.test?.error || "未知错误"}`);
  }
  await loadStrategies();
}

async function saveRiceQuantKey() {
  const input = $("rqLicenseKey");
  const key = input.value.trim();
  if (!key) {
    showToast("请输入米筐 license key");
    return;
  }
  showToast("正在连接米筐验证密钥…");
  const data = await postJson("/api/data/connectors/ricequant/credentials", { license_key: key });
  if (data.test?.ok) {
    const bar = data.test.sample_bar;
    showToast(`米筐连接成功：${bar ? `${bar.symbol} ${bar.timestamp} close ${bar.close}` : "已验证"}`);
  } else {
    showToast(`密钥已保存，但连接测试失败：${data.test?.error || "未知错误"}`);
  }
  await loadStrategies();
}

async function loadQuote() {
  const symbol = $("quoteSymbol").value.trim().toUpperCase();
  if (!symbol) return;
  const params = new URLSearchParams({
    symbol,
    data_source: $("quoteDataSource").value || "fixture",
    frequency: $("quoteFrequency").value || "5m",
    limit: "6",
  });
  const data = await fetchJson(`/api/chart/bars?${params}`);
  $("quoteBars").innerHTML =
    data.bars
      .slice()
      .reverse()
      .map(
        (bar) => `
          <div class="quote-row">
            <strong>${bar.symbol}</strong>
            <span>${formatTime(bar.timestamp)}</span>
            <span>O ${formatNumber(bar.open)} · H ${formatNumber(bar.high)} · L ${formatNumber(bar.low)} · C <b>${formatNumber(bar.close)}</b></span>
            <span>Vol ${formatNumber(bar.volume)}</span>
          </div>
        `,
      )
      .join("") || '<div class="quote-row"><span>无数据</span></div>';
  showToast(`已拉取 ${data.symbol} · ${data.data_source} · ${data.bars.length} bars`);
}

// ---- 本地数据存储位置 ----
function nativeApi() {
  return (typeof window !== "undefined" && window.pywebview && window.pywebview.api) || null;
}

async function loadStorageLocation() {
  const data = await fetchJson("/api/settings/data-location");
  $("storagePath").textContent = data.current;
  $("storageNote").textContent = data.is_custom ? "（自定义位置）" : "（默认位置）";
}

async function handleStoragePick() {
  const api = nativeApi();
  if (api && api.pick_folder) {
    const path = await api.pick_folder();
    if (path) await applyStorageLocation(path);
    return;
  }
  // 非原生窗口（开发/浏览器）：用文本框输入绝对路径
  const input = $("storageInput");
  if (input.hidden) {
    input.hidden = false;
    input.focus();
    $("storagePick").textContent = "应用此路径";
    return;
  }
  const path = input.value.trim();
  if (!path) {
    showToast("请输入文件夹绝对路径");
    return;
  }
  await applyStorageLocation(path);
}

async function applyStorageLocation(path) {
  const data = await postJson("/api/settings/data-location", { path, move_existing: $("storageMove").checked });
  const moved = data.moved && data.moved.length ? `（已复制 ${data.moved.join("、")}）` : "";
  showStorageRestart(`新位置已保存：${data.path}${moved}`);
  await loadStorageLocation();
}

async function resetStorageLocation() {
  const data = await postJson("/api/settings/data-location/reset", {});
  showStorageRestart(`已恢复默认位置：${data.default}`);
  await loadStorageLocation();
}

function showStorageRestart(text) {
  const api = nativeApi();
  const restartBtn = api && api.restart ? ' <button id="storageRestart" type="button" class="primary">立即重启</button>' : "（请退出并重新打开 app 生效）";
  $("storageMsg").innerHTML = `<span>${text} —— 重启后生效。</span>${restartBtn}`;
  const btn = document.getElementById("storageRestart");
  if (btn) btn.addEventListener("click", () => nativeApi().restart());
}

function renderAccountControls() {
  const accountOptions = state.accounts
    .map((account) => `<option value="${account.id}">${account.name} · ${formatNumber(account.unallocated_cash)} 可用</option>`)
    .join("");
  for (const id of [
    "sleeveAccount",
    "orderAccount",
    "repoAccount",
    "backfillAccount",
    "deleteAccountSelect",
    "strategyRunAccount",
    "timingBindAccount",
    "timingRunAccount",
    "schedulerAccount",
    "riskAccount",
  ]) {
    const select = $(id);
    const previous = select.value;
    select.innerHTML = accountOptions;
    if (previous) select.value = previous;
  }
  // 顶栏账户切换器:跟随筛选条选中的账户。
  const topSelect = $("topAccountSelect");
  topSelect.innerHTML = state.accounts.map((account) => `<option value="${account.id}">${account.name}</option>`).join("");
  const activeAccount = $("accountFilter").value.trim() || state.accounts[0]?.id;
  if (activeAccount && state.accounts.some((account) => account.id === activeAccount)) {
    topSelect.value = activeAccount;
    // 逆回购卡的账户跟随活动账户,保证"逆回购记录"展示的就是当前账户。
    if (state.accounts.some((account) => account.id === activeAccount)) $("repoAccount").value = activeAccount;
  }
  renderSleeveOptions();
  renderBackfillSleeves();
  renderStrategyRunSleeves();
  renderTimingSleeves();
  renderSchedulerSleeves();
  renderRiskSleeves();
  updateRepoAmountDefault();
}

function renderBackfillSleeves() {
  const accountId = $("backfillAccount").value || state.accounts[0]?.id;
  const account = state.accounts.find((item) => item.id === accountId);
  const previous = $("backfillSleeve").value;
  $("backfillSleeve").innerHTML = (account?.sleeves || [])
    .map((sleeve) => `<option value="${sleeve.id}">${sleeve.name} · ${formatNumber(sleeve.available_cash)} cash</option>`)
    .join("");
  if (previous) $("backfillSleeve").value = previous;
}

function updateTicker(account) {
  // 左下侧边栏"当前账户"跟随选中账户(此前写死 A-Share Alpha)。
  $("sidebarAccountName").textContent = account?.name || "--";
  if ($("sidebarAccountStatus")) {
    $("sidebarAccountStatus").textContent = account ? `权益 ¥${formatNumber(account.equity)}` : "审计流水正常";
  }
  if (!account) {
    for (const id of ["tickerEquity", "tickerPnl", "tickerCash", "tickerMv", "tickerExposure"]) {
      $(id).textContent = "--";
      $(id).className = "";
    }
    return;
  }
  $("tickerEquity").textContent = `¥${formatNumber(account.equity)}`;
  $("tickerPnl").textContent = `${account.pnl >= 0 ? "+" : ""}${formatNumber(account.pnl)} (${formatPercent(account.pnl_pct)})`;
  $("tickerPnl").className = numberClass(account.pnl);
  $("tickerCash").textContent = `¥${formatNumber(account.total_cash)}`;
  $("tickerMv").textContent = `¥${formatNumber(account.market_value)}`;
  $("tickerExposure").textContent = formatPercent(account.exposure);
}

function renderRiskSleeves() {
  const accountId = $("riskAccount").value || state.accounts[0]?.id;
  const account = state.accounts.find((item) => item.id === accountId);
  const previous = $("riskSleeve").value;
  // 空值代表账户级配置；选中 sleeve 则是 sleeve 级配置(覆盖账户级)。
  $("riskSleeve").innerHTML =
    '<option value="">账户级(全部 sleeve)</option>' +
    (account?.sleeves || [])
      .map((sleeve) => `<option value="${sleeve.id}">${sleeve.name} · ${sleeve.strategy_id}</option>`)
      .join("");
  if (previous) $("riskSleeve").value = previous;
}

function renderSleeveOptions() {
  const accountId = $("orderAccount").value || state.accounts[0]?.id;
  const account = state.accounts.find((item) => item.id === accountId);
  const options = (account?.sleeves || [])
    .map((sleeve) => `<option value="${sleeve.id}">${sleeve.name} · ${formatNumber(sleeve.available_cash)} cash</option>`)
    .join("");
  $("orderSleeve").innerHTML = options;
}

function renderStrategyRunSleeves() {
  const accountId = $("strategyRunAccount").value || state.accounts[0]?.id;
  const account = state.accounts.find((item) => item.id === accountId);
  const options = (account?.sleeves || [])
    .map((sleeve) => `<option value="${sleeve.id}">${sleeve.name} · ${sleeve.strategy_id}</option>`)
    .join("");
  $("strategyRunSleeve").innerHTML = options;
}

function renderTimingSleeves() {
  for (const [accountSelectId, sleeveSelectId] of [
    ["timingBindAccount", "timingBindSleeve"],
    ["timingRunAccount", "timingRunSleeve"],
  ]) {
    const accountId = $(accountSelectId).value || state.accounts[0]?.id;
    const account = state.accounts.find((item) => item.id === accountId);
    const options = (account?.sleeves || [])
      .map((sleeve) => `<option value="${sleeve.id}">${sleeve.name} · ${sleeve.strategy_id}</option>`)
      .join("");
    $(sleeveSelectId).innerHTML = options;
  }
}

function renderSchedulerSleeves() {
  const accountId = $("schedulerAccount").value || state.accounts[0]?.id;
  const account = state.accounts.find((item) => item.id === accountId);
  const options = (account?.sleeves || [])
    .map((sleeve) => `<option value="${sleeve.id}">${sleeve.name} · ${sleeve.strategy_id}</option>`)
    .join("");
  $("schedulerSleeve").innerHTML = options;
}

const DEFAULT_SOURCE_KEY = "pt_default_data_source";

// 全站数据源下拉统一填充：保留用户已选值，否则用全局「默认数据源」。
function fillConnectorSelect(id) {
  const select = $(id);
  const previous = select.value || effectiveDefaultSource();
  select.innerHTML = state.connectors
    .map((connector) => `<option value="${connector.name}">${connector.name} · ${connectorStatusLabel(connector.status)}</option>`)
    .join("");
  if (previous && state.connectors.some((connector) => connector.name === previous)) {
    select.value = previous;
  }
}

function connectorStatusLabel(status) {
  return { ok: "正常", configured: "已配置", not_configured: "未配置", unavailable: "不可用" }[status] || status;
}

function renderStrategyControls() {
  const previous = $("strategySelect").value;
  $("strategySelect").innerHTML = state.strategies
    .map((strategy) => `<option value="${strategy.id}">${strategy.name}</option>`)
    .join("");
  if (previous) $("strategySelect").value = previous;
  // 回测页的选股策略下拉
  const previousBt = $("btStrategy").value;
  $("btStrategy").innerHTML = state.strategies.length
    ? state.strategies.map((strategy) => `<option value="${strategy.id}">${strategy.name}</option>`).join("")
    : '<option value="">先导入选股策略</option>';
  if (previousBt) $("btStrategy").value = previousBt;
  for (const id of DATA_SOURCE_SELECTS) {
    fillConnectorSelect(id);
  }
  renderDataConnectors();
  $("connectorHealth").innerHTML = state.connectors
    .map(
      (connector) => `
        <div class="connector-row ${connector.status}">
          <strong>${connector.name}</strong>
          <span>${connector.status}</span>
          <span>${(connector.supported_frequencies || []).join(", ")}</span>
        </div>
      `,
    )
    .join("");
  $("strategyRuns").innerHTML = state.runs
    .slice(0, 5)
    .map(
      (run) => `
        <div class="run-row">
          <strong>${run.status}</strong>
          <span>${run.strategy_id}</span>
          <span>${run.orders_submitted} orders · ${run.frequency}</span>
        </div>
      `,
    )
    .join("");
  $("strategyList").innerHTML =
    state.strategies
      .map((strategy) => `<div class="managed-row" data-id="${strategy.id}"><span>${strategy.name}<small>${strategy.id}</small></span><button type="button" data-action="delete-strategy">删除</button></div>`)
      .join("") || '<div class="managed-row"><span>暂无已导入策略</span></div>';
}

function renderTimingControls() {
  const timingOptions = state.timingStrategies.length
    ? state.timingStrategies.map((strategy) => `<option value="${strategy.id}">${strategy.name}</option>`).join("")
    : '<option value="">先导入择时策略</option>';
  for (const id of ["timingBindStrategy", "timingRunStrategy"]) {
    const select = $(id);
    const previous = select.value;
    select.innerHTML = timingOptions;
    if (previous) select.value = previous;
  }

  const stockOptions = state.strategies.length
    ? state.strategies.map((strategy) => `<option value="${strategy.id}">${strategy.name}</option>`).join("")
    : '<option value="">先导入选股策略</option>';
  for (const id of ["timingBindStockStrategy", "timingRunControlledStrategy"]) {
    const select = $(id);
    const previous = select.value;
    select.innerHTML = stockOptions;
    if (previous) select.value = previous;
  }

  fillConnectorSelect("timingRunDataSource");

  $("timingBindings").innerHTML =
    state.timingBindings
      .slice(0, 6)
      .map(
        (binding) => `
          <div class="binding-row">
            <strong>${findTimingName(binding.timing_strategy_id)}</strong>
            <span>${findStrategyName(binding.strategy_id)} · ${binding.sleeve_id || "account-wide"}</span>
            <span>${binding.active ? "active" : "paused"}</span>
          </div>
        `,
      )
      .join("") || '<div class="binding-row"><span>暂无绑定</span><span>先绑定择时策略与选股策略</span><span>idle</span></div>';

  $("timingDecisions").innerHTML =
    state.timingDecisions
      .slice(0, 6)
      .map((decision) => {
        const riskClass = decision.allow_open ? "risk-on" : "risk-off";
        const label = decision.allow_open ? "allow_open" : "block_open";
        return `
          <div class="decision-row">
            <strong class="${riskClass}">${label}</strong>
            <span>${decision.position_policy}</span>
            <span>${decision.reason || "--"}</span>
          </div>
        `;
      })
      .join("") || '<div class="decision-row"><span>暂无决策</span><span>no gate</span><span>运行择时策略后显示</span></div>';

  $("timingList").innerHTML =
    state.timingStrategies
      .map((strategy) => `<div class="managed-row" data-id="${strategy.id}"><span>${strategy.name}<small>${strategy.id}</small></span><button type="button" data-action="delete-timing">删除</button></div>`)
      .join("") || '<div class="managed-row"><span>暂无已导入择时策略</span></div>';
}

async function handleManagedDelete(event, kind) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const row = button.closest(".managed-row");
  const id = row?.dataset.id;
  if (!id) return;
  const label = row.querySelector("span")?.textContent || id;
  if (!confirm(`确认删除「${label}」？此操作不可撤销。`)) return;
  const path = kind === "strategy" ? `/api/strategies/${encodeURIComponent(id)}/delete` : `/api/timing-strategies/${encodeURIComponent(id)}/delete`;
  await postJson(path, {});
  showToast("已删除");
  await refreshAll();
}

function renderSchedulerControls() {
  const strategyOptions = state.strategies.length
    ? state.strategies.map((strategy) => `<option value="${strategy.id}">${strategy.name}</option>`).join("")
    : '<option value="">先导入选股策略</option>';
  const previousStrategy = $("schedulerStrategy").value;
  $("schedulerStrategy").innerHTML = strategyOptions;
  if (previousStrategy) $("schedulerStrategy").value = previousStrategy;

  const timingOptions = [
    '<option value="">无择时 gate</option>',
    ...state.timingStrategies.map((strategy) => `<option value="${strategy.id}">${strategy.name}</option>`),
  ].join("");
  for (const id of ["schedulerTiming", "btTiming"]) {
    const select = $(id);
    const previous = select.value;
    select.innerHTML = timingOptions;
    if (previous) select.value = previous;
  }

  fillConnectorSelect("schedulerDataSource");

  $("schedulerTasks").innerHTML =
    state.schedulerTasks
      .slice(0, 8)
      .map(
        (task) => `
          <div class="task-row" data-task-id="${task.id}">
            <strong class="${task.status}">${task.status}</strong>
            <span>${task.name} · ${findStrategyName(task.strategy_id)} · ${task.frequency} · ${task.symbols.join(", ")} · done ${task.ticks_completed}/skip ${task.ticks_skipped} · last ${task.last_bar_at || "--"}</span>
            <div class="task-actions">
              <button type="button" data-action="start">Start</button>
              <button type="button" data-action="stop">Stop</button>
              <button type="button" class="primary" data-action="tick">Tick</button>
            </div>
          </div>
        `,
      )
      .join("") || '<div class="task-row"><span>暂无任务</span><span>先创建 scheduler task</span><span>idle</span></div>';

  $("schedulerTicks").innerHTML =
    state.schedulerTicks
      .slice(0, 8)
      .map(
        (tick) => `
          <div class="tick-row">
            <strong class="${tick.status}">${tick.status}</strong>
            <span>${tick.task_id} · decisions ${tick.decisions_recorded} · orders ${tick.orders_submitted} · ${tick.skip_reason || tick.bar_timestamp || "--"}</span>
            <span>${formatTime(tick.started_at)}</span>
          </div>
        `,
      )
      .join("") || '<div class="tick-row"><span>暂无 tick</span><span>手动 Tick 或 Start 后显示</span><span>--</span></div>';
}

function renderStrategyBoard() {
  const account = state.portfolio?.accounts?.[0];
  const container = $("strategyBoard");
  if (!account || !account.sleeves.length) {
    container.innerHTML = '<div class="board-empty">暂无策略 sleeve。在「模拟交易」页创建 sleeve 并分配资金后，这里会按策略展示持仓矩阵。</div>';
    return;
  }
  container.innerHTML = account.sleeves
    .map((sleeve) => {
      const allocatedPct = (Number(sleeve.allocated_pct || 0) * 100).toFixed(1);
      const rows = (sleeve.positions || [])
        .map((position) => {
          const weight = sleeve.equity ? position.market_value / sleeve.equity : 0;
          return `
            <tr>
              <td class="sym">${position.name && position.name !== position.symbol ? `${position.name}<br><small>${position.symbol}</small>` : position.symbol}</td>
              <td>${formatNumber(position.quantity)}</td>
              <td>${formatPercent(weight)}</td>
              <td>${formatNumber(position.avg_cost)}</td>
              <td>${formatNumber(position.mark_price)}</td>
              <td>${position.volatility === null || position.volatility === undefined ? "--" : formatPercent(position.volatility)}</td>
              <td class="${numberClass(position.unrealized_pnl)}">${formatNumber(position.unrealized_pnl)} (${formatPercent(position.unrealized_pnl_pct)})</td>
            </tr>
          `;
        })
        .join("");
      return `
        <div class="strategy-card ${sleeve.active ? "" : "paused"}" data-sleeve-id="${sleeve.id}">
          <div class="strategy-card-head">
            <div class="strategy-card-title">
              <strong>${findStrategyName(sleeve.strategy_id)}</strong>
              <span>${sleeve.name} · 权益 ${formatNumber(sleeve.equity)} · <b class="${numberClass(sleeve.pnl)}">${formatNumber(sleeve.pnl)}</b></span>
            </div>
            <div class="strategy-card-controls">
              <label class="alloc">占比
                <input type="number" min="0" max="100" step="0.5" value="${allocatedPct}" data-field="percent" /> %
              </label>
              <button type="button" data-action="allocate">调整</button>
              <label class="switch">
                <input type="checkbox" data-action="toggle" ${sleeve.active ? "checked" : ""} />启用
              </label>
            </div>
          </div>
          ${
            rows
              ? `<div class="strategy-table-wrap"><table class="strategy-table">
                  <thead><tr><th>代码</th><th>数量</th><th>仓位</th><th>买入价</th><th>实时价</th><th>波动率</th><th>浮动盈亏</th></tr></thead>
                  <tbody>${rows}</tbody>
                </table></div>`
              : '<div class="board-empty">该策略暂无持仓</div>'
          }
        </div>
      `;
    })
    .join("");
}

async function handleStrategyBoardAction(event) {
  const card = event.target.closest(".strategy-card");
  const sleeveId = card?.dataset.sleeveId;
  if (!sleeveId) return;
  if (event.target.matches("input[data-action='toggle']")) {
    const active = event.target.checked;
    await postJson(`/api/sleeves/${encodeURIComponent(sleeveId)}/active`, { active });
    showToast(active ? "策略已启用" : "策略已停用：新开仓与调度将被拦截，仍可卖出退出");
    await refreshAll();
    return;
  }
  const button = event.target.closest("button[data-action='allocate']");
  if (button) {
    const input = card.querySelector("input[data-field='percent']");
    await postJson(`/api/sleeves/${encodeURIComponent(sleeveId)}/allocation`, { percent: Number(input.value) });
    showToast(`占比已调整为 ${input.value}%`);
    await refreshAll();
  }
}

function renderRiskControls() {
  const limitLabels = [
    ["max_order_notional", "单笔金额"],
    ["max_sleeve_exposure", "敞口比例"],
    ["max_symbol_position", "单标的持仓"],
    ["min_cash_buffer", "现金缓冲"],
    ["max_orders_per_tick", "每Tick订单"],
    ["max_orders_per_day", "每日订单"],
  ];
  $("riskConfigs").innerHTML =
    state.riskConfigs
      .map((config) => {
        const scopeName =
          config.scope_type === "sleeve"
            ? findSleeveName(config.scope_id)
            : state.accounts.find((account) => account.id === config.scope_id)?.name || config.scope_id;
        const limits = limitLabels
          .filter(([field]) => config[field] !== null && config[field] !== undefined)
          .map(([field, label]) => `${label} ${formatNumber(config[field])}`)
          .join(" · ");
        return `
          <div class="risk-row">
            <strong>${config.scope_type === "sleeve" ? "Sleeve" : "Account"} · ${scopeName}</strong>
            <span>${limits || "--"}</span>
            <strong class="${config.enabled ? "enabled" : "disabled"}">${config.enabled ? "enabled" : "off"}</strong>
          </div>
        `;
      })
      .join("") || '<div class="risk-row"><span>暂无风控规则</span><span>保存配置后，下单前会自动检查并拦截超限订单</span><span>idle</span></div>';
}

function findSleeveName(sleeveId) {
  for (const account of state.accounts) {
    const sleeve = (account.sleeves || []).find((item) => item.id === sleeveId);
    if (sleeve) return sleeve.name;
  }
  return sleeveId;
}

function renderOrderBook() {
  const container = $("orderBook");
  if (!state.orders.length) {
    container.innerHTML = '<div class="order-row empty"><span>暂无订单</span><span>提交订单或运行策略后显示</span><span>--</span></div>';
    return;
  }
  container.innerHTML = state.orders
    .map((order) => {
      const canCancel = ["created", "submitted", "partially_filled"].includes(order.status);
      return `
        <div class="order-row" data-order-id="${order.id}">
          <div>
            <strong class="${order.status}">${order.status}</strong>
            <span>${order.symbol} · ${order.side} · ${order.order_type}/${order.time_in_force}</span>
          </div>
          <div>
            <strong>${formatNumber(order.filled_quantity)} / ${formatNumber(order.quantity)}</strong>
            <span>remaining ${formatNumber(order.remaining_quantity)} · price ${formatNumber(order.last_fill_price || order.signal_price)}</span>
          </div>
          <div>
            <span>${findStrategyName(order.strategy_id)}</span>
            <span>${formatTime(order.updated_at)}</span>
          </div>
          <div class="order-actions">
            <button type="button" data-action="chain">Chain</button>
            ${canCancel ? '<button type="button" class="danger" data-action="cancel">Cancel</button>' : ""}
          </div>
        </div>
      `;
    })
    .join("");
}

function renderPortfolio() {
  const account = state.portfolio?.accounts?.[0];
  updateTicker(account);
  renderBlotter();
  updateTicketEstimate();
  if (!account) {
    $("portfolioAccountName").textContent = "暂无账户";
    for (const id of ["portfolioEquity", "portfolioPnl", "portfolioCash", "portfolioMarketValue", "portfolioExposure"]) {
      $(id).textContent = "--";
      $(id).className = "";
    }
    $("portfolioSleeves").innerHTML = '<div class="portfolio-row"><span>暂无 sleeve</span><span>--</span></div>';
    $("portfolioPositions").innerHTML = '<div class="portfolio-row"><span>暂无持仓</span><span>--</span></div>';
    return;
  }

  $("portfolioAccountName").textContent = `${account.name} · ${account.id}`;
  $("portfolioUpdatedAt").textContent = portfolioMarkText(state.portfolio?.mark, account);
  $("portfolioEquity").textContent = formatNumber(account.equity);
  $("portfolioPnl").textContent = `${formatNumber(account.pnl)} (${formatPercent(account.pnl_pct)})`;
  $("portfolioPnl").className = numberClass(account.pnl);
  $("portfolioCash").textContent = formatNumber(account.total_cash);
  $("portfolioMarketValue").textContent = formatNumber(account.market_value);
  $("portfolioExposure").textContent = formatPercent(account.exposure);

  $("portfolioSleeves").innerHTML =
    account.sleeves
      .map(
        (sleeve) => `
          <div class="portfolio-row">
            <div>
              <strong>${sleeve.name}</strong>
              <span>${sleeve.strategy_id}</span>
            </div>
            <div>
              <strong>${formatNumber(sleeve.equity)}</strong>
              <span class="${numberClass(sleeve.pnl)}">${formatNumber(sleeve.pnl)} · ${formatPercent(sleeve.exposure)}</span>
            </div>
          </div>
        `,
      )
      .join("") || '<div class="portfolio-row"><span>暂无 sleeve</span><span>--</span></div>';

  $("portfolioPositions").innerHTML =
    account.positions
      .map(
        (position) => `
          <div class="portfolio-row">
            <div>
              <strong>${position.name && position.name !== position.symbol ? position.name : ""} <span class="pos-code">${position.symbol}</span></strong>
              <span>${position.sleeve_name} · ${position.quantity} 股 · 成本 ${formatNumber(position.avg_cost)} · 现价 ${formatNumber(position.mark_price)}</span>
            </div>
            <div>
              <strong>${formatNumber(position.market_value)}</strong>
              <span class="${numberClass(position.unrealized_pnl)}">${formatNumber(position.unrealized_pnl)} (${formatPercent(position.unrealized_pnl_pct)})</span>
            </div>
          </div>
        `,
      )
      .join("") || '<div class="portfolio-row"><span>暂无持仓</span><span>--</span></div>';
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

async function loadPythonFile(event, { textareaId, nameInputId, sourceKey }) {
  const file = event.target.files?.[0];
  if (!file) return;
  if (file.size > 512 * 1024) {
    event.target.value = "";
    throw new Error("策略文件超过 512KB，请拆分后再导入");
  }
  const code = await file.text();
  $(textareaId).value = code;
  if (shouldAutofillName($(nameInputId).value)) {
    $(nameInputId).value = displayNameFromFile(file.name);
  }
  state[sourceKey] = file.name;
  showToast(`已读取文件 ${file.name}`);
}

function shouldAutofillName(value) {
  const normalized = value.trim();
  return !normalized || normalized.startsWith("Demo ") || normalized === "Python Strategy" || normalized === "Timing Strategy";
}

function displayNameFromFile(fileName) {
  const base = fileName.replace(/\.[^.]+$/, "");
  return base
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function portfolioMarkText(mark, account) {
  if (!mark || mark.mode === "position_last_price") {
    return `${account.currency} · ${account.market} · last fill price`;
  }
  const marked = mark.marked_symbols?.length || 0;
  const total = mark.symbols?.length || 0;
  return `${account.currency} · ${mark.data_source} ${mark.frequency} close · marked ${marked}/${total}`;
}

function renderMetrics() {
  $("metricEvents").textContent = state.events.length;
  $("metricTrades").textContent = state.events.filter((event) => event.ledger_type === "trade").length;
  $("metricCash").textContent = state.events.filter((event) => event.ledger_type === "cash").length;
  $("metricBlocks").textContent = state.events.filter((event) => event.event_type === "timing_blocked").length;
  $("metricRiskBlocks").textContent = state.events.filter((event) => event.event_type === "risk_blocked").length;
}

function renderTable() {
  const body = $("eventsTable");
  body.innerHTML = "";
  for (const event of state.events) {
    const row = document.createElement("tr");
    row.className = event.id === state.selectedId ? "selected" : "";
    row.addEventListener("click", () => selectEvent(event.id));
    row.innerHTML = `
      <td>${formatTime(event.timestamp)}</td>
      <td><span class="tag ${event.ledger_type}">${event.ledger_type}</span></td>
      <td>${event.event_type}</td>
      <td>${event.symbol || "--"}</td>
      <td class="${numberClass(event.amount)}">${formatNumber(event.amount)}</td>
      <td>${formatNumber(event.quantity)}</td>
      <td>${formatNumber(event.price)}</td>
      <td>${event.reason || "--"}</td>
    `;
    body.appendChild(row);
  }
}

async function selectEvent(eventId) {
  state.selectedId = eventId;
  renderTable();
  const chain = await fetchJson(`/api/audit/chain/${encodeURIComponent(eventId)}`);
  $("chainSubtitle").textContent = eventId;
  renderChain(chain);
}

function renderChain(chain) {
  const content = $("chainContent");
  const steps = [
    ["策略信号", chain.signal],
    ["择时门控", chain.timing_decision],
    ["风控门控", chain.risk_decision],
    ["订单", chain.order_events?.length ? chain.order_events : chain.order],
    ["成交", chain.trade],
    ["现金变化", chain.cash_changes],
    ["持仓变化", chain.position_changes],
    ["净值快照", chain.portfolio_snapshot],
  ];

  content.className = "";
  content.innerHTML = steps
    .map(([title, value]) => {
      const events = Array.isArray(value) ? value : value ? [value] : [];
      if (!events.length) {
        return `
          <div class="chain-step">
            <h3>${title}</h3>
            <p>无记录</p>
          </div>
        `;
      }
      return events
        .map((event) => {
          const payload = {
            id: event.id,
            event_type: event.event_type,
            symbol: event.symbol,
            amount: event.amount,
            quantity: event.quantity,
            price: event.price,
            before_state: event.before_state,
            after_state: event.after_state,
            metadata: event.metadata,
          };
          return `
            <div class="chain-step">
              <h3>${title} · ${event.event_type}</h3>
              <p>${formatTime(event.timestamp)} · ${event.reason || "--"}</p>
              <code>${escapeHtml(JSON.stringify(payload, null, 2))}</code>
            </div>
          `;
        })
        .join("");
    })
    .join("");
}

function download(format) {
  const params = queryParams();
  params.set("format", format);
  window.location.href = `/api/audit/export?${params}`;
}

async function refreshAll(selectEventId = null) {
  await loadAccounts();
  await loadStrategies();
  await loadTiming();
  await loadScheduler();
  await loadRiskConfigs();
  await loadPortfolio();
  await loadOrders();
  await loadEvents();
  await loadWatchlist().catch(() => {});
  await loadRepoInstruments().catch(() => {});
  await loadReverseRepo().catch(() => {});
  await loadPerformance().catch(() => {});
  await loadBacktestRuns().catch(() => {});
  await loadStorageLocation().catch(() => {});
  if (!state.chart.bars.length) await loadChart();
  if (selectEventId) await selectEvent(selectEventId);
}

async function createAccount(event) {
  event.preventDefault();
  const data = await postJson("/api/accounts", {
    name: $("newAccountName").value.trim(),
    initial_cash: Number($("newInitialCash").value),
    commission_rate: Number($("newCommissionRate").value),
    stamp_duty_rate: Number($("newStampDutyRate").value),
    slippage_model: $("newSlippageModel").value,
    slippage_value: Number($("newSlippageValue").value),
    auto_reverse_repo_enabled: true,
    reverse_repo_annual_rate: Number($("newRepoRate").value),
  });
  $("accountFilter").value = data.account.id;
  showToast(`已创建账户 ${data.account.name}`);
  await refreshAll();
}

async function createSleeve(event) {
  event.preventDefault();
  const accountId = $("sleeveAccount").value;
  const data = await postJson(`/api/accounts/${encodeURIComponent(accountId)}/sleeves`, {
    name: $("newSleeveName").value.trim(),
    strategy_id: $("newSleeveStrategy").value.trim(),
    allocated_cash: Number($("newSleeveCash").value),
  });
  $("accountFilter").value = accountId;
  showToast(`已创建 Sleeve ${data.sleeve.name}`);
  await refreshAll();
}

async function submitOrder(event) {
  event.preventDefault();
  const { account, sleeve, symbol } = ticketContext();
  if (!sleeve) {
    showToast("请先选择账户与 sleeve");
    return;
  }
  const side = $("orderSide").value;
  const quantity = Number($("orderQuantity").value);
  if (!quantity || quantity < 100) {
    showToast("数量需为正，且 A 股最少 100 股（1 手）");
    return;
  }
  const payload = {
    account_id: account.id,
    sleeve_id: sleeve.id,
    strategy_id: sleeve.strategy_id || "strategy_manual_5m",
    symbol,
    side,
    quantity,
    data_source: ticketSource(),
    allow_open: true,
    timing_strategy_id: "timing_manual_gate",
    signal_reason: "manual order from trade ticket",
  };
  // 限价单带价；市价单留空,broker 按行情源最新 close 定价。
  if ($("orderPriceType").value === "limit") {
    const price = Number($("orderSignalPrice").value);
    if (!price) {
      showToast("限价单请填写价格");
      return;
    }
    payload.signal_price = price;
    payload.fill_price = price;
  }
  const data = await postJson("/api/broker/orders", payload);
  $("accountFilter").value = account.id;
  $("symbolFilter").value = symbol;
  $("chartSymbol").value = symbol;
  showToast(
    data.accepted
      ? `${side === "BUY" ? "买入" : "卖出"} ${data.order_status}，成交 ${formatNumber(data.filled_quantity)} 股`
      : `订单被拒绝：${data.reason}`,
  );
  await refreshAll(data.source_event_id || data.event_id);
}

async function handleOrderBookAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const row = button.closest(".order-row");
  const orderId = row?.dataset.orderId;
  if (!orderId) return;
  const order = state.orders.find((item) => item.id === orderId);
  if (button.dataset.action === "chain" && order?.source_event_id) {
    await selectEvent(order.source_event_id);
    showToast("已打开订单审计链路");
    return;
  }
  if (button.dataset.action === "cancel") {
    const data = await postJson(`/api/broker/orders/${encodeURIComponent(orderId)}/cancel`, {
      reason: "cancelled from order book",
    });
    $("eventTypeFilter").value = "order_cancelled";
    showToast(`订单已撤销：${data.order.id}`);
    await refreshAll(data.order.source_event_id);
  }
}

// 逆回购"投入现金"默认填该账户总闲置现金(未分配 + 各 sleeve 可用),与后端口径一致。
function accountIdleCash(account) {
  if (!account) return 0;
  const sleeveCash = (account.sleeves || []).reduce((s, sl) => s + Number(sl.available_cash || 0), 0);
  return Math.floor((Number(account.unallocated_cash || 0) + sleeveCash) * 100) / 100;
}

function updateRepoAmountDefault() {
  const account = state.accounts.find((a) => a.id === $("repoAccount").value);
  const idle = accountIdleCash(account);
  if (idle > 0) $("repoAmount").value = idle;
}

async function loadRepoInstruments() {
  if ($("repoSymbol").options.length) return;
  try {
    const data = await fetchJson("/api/repo/instruments");
    $("repoSymbol").innerHTML = (data.instruments || [])
      .map((it) => `<option value="${it.symbol}">${it.name} · ${it.desc}(${it.term_days}天)</option>`)
      .join("");
    $("repoSymbol").value = data.default || "204001.SH";
  } catch (error) {
    /* 离线忽略 */
  }
  applyRepoRateMode();
}

// 利率来源联动:实时行情→禁用手填、拉当前利率;自定义→可手填。
async function applyRepoRateMode() {
  const market = $("repoRateMode").value === "market";
  $("repoRate").disabled = market;
  if (market) {
    $("repoRateHint").className = "backfill-msg";
    $("repoRateHint").textContent = "拉取实时利率…";
    try {
      const q = await fetchJson(
        `/api/repo/rate?symbol=${encodeURIComponent($("repoSymbol").value)}&data_source=${encodeURIComponent(ticketSource())}`,
      );
      if (q.annual_rate) {
        $("repoRate").value = q.annual_rate;
        $("repoRateHint").className = "backfill-msg ok";
        $("repoRateHint").textContent = `实时:${$("repoSymbol").selectedOptions[0]?.text || ""} 年化 ${formatPercent(q.annual_rate)}`;
      } else {
        $("repoRateHint").className = "backfill-msg err";
        $("repoRateHint").textContent = "行情取不到该利率,执行时将回退到当前框内自定义值";
      }
    } catch (error) {
      $("repoRateHint").className = "backfill-msg err";
      $("repoRateHint").textContent = "行情取不到,回退自定义";
    }
  } else {
    $("repoRateHint").className = "backfill-msg";
    $("repoRateHint").textContent = "自定义年化利率(小数,如 0.018 = 1.8%)";
  }
}

async function runReverseRepo(event) {
  event.preventDefault();
  const accountId = $("repoAccount").value;
  const body = {
    amount: Number($("repoAmount").value),
    annual_rate: Number($("repoRate").value),
    rate_mode: $("repoRateMode").value,
    repo_symbol: $("repoSymbol").value,
    data_source: ticketSource(),
  };
  if ($("repoDate").value) body.trade_date = $("repoDate").value;
  const data = await postJson(`/api/accounts/${encodeURIComponent(accountId)}/reverse-repo`, body);
  const src = data.rate_source && data.rate_source.startsWith("market") ? data.rate_source : "自定义";
  showToast(`逆回购已记录(${data.trade_date} 14:30)·年化 ${formatPercent(data.annual_rate)}(${src})·利息 ${formatNumber(data.interest)}`);
  await Promise.all([loadPortfolio(), loadReverseRepo()]);
}

async function loadReverseRepo() {
  // 记录跟随当前活动账户(顶部选择器),并在面板上标明是哪个账户,避免歧义。
  const accountId = $("accountFilter").value.trim() || state.accounts[0]?.id;
  if (!accountId) return;
  const account = state.accounts.find((a) => a.id === accountId);
  $("repoAccountLabel").textContent = account ? account.name : accountId;
  // 自愈:每次查看记录前先幂等补全闲置现金的逐日逆回购,保证"列表=真相"(缺哪天补哪天,不重复)。
  await postJson(`/api/accounts/${encodeURIComponent(accountId)}/reverse-repo/reconcile`, { data_source: ticketSource() }).catch(() => {});
  let data;
  try {
    data = await fetchJson(`/api/accounts/${encodeURIComponent(accountId)}/reverse-repo`);
  } catch (error) {
    return;
  }
  const summary = data.summary || {};
  $("repoSummary").textContent = `自动补全 · 共 ${summary.days || 0} 日 · 截至 ${summary.last_date || "—"} · 累计利息 ${formatNumber(summary.total_interest || 0)}`;
  const records = data.records || [];
  $("repoRecords").innerHTML =
    records
      .map(
        (r) => `
        <div class="repo-row">
          <div><strong>${r.trade_date}</strong><span>${r.source === "auto" ? "自动" : "手动"} · ${formatTime(r.timestamp).slice(11, 16)}</span></div>
          <div><strong>${formatNumber(r.invest_amount)}</strong><span>年化 ${formatPercent(r.annual_rate)} · ${r.rate_source && r.rate_source.startsWith("market") ? "实时" : "自定义"}</span></div>
          <div class="positive"><strong>+${formatNumber(r.interest)}</strong><span>当日利息</span></div>
        </div>`,
      )
      .join("") || '<div class="repo-row"><span>暂无逆回购记录</span></div>';
}

async function deleteAccount() {
  const msg = $("deleteAccountMsg");
  const select = $("deleteAccountSelect");
  const accountId = select.value;
  if (!accountId) return;
  const label = select.options[select.selectedIndex]?.text || accountId;
  const force = $("deleteAccountForce").checked;
  if (!window.confirm(`确定删除账户「${label}」？\n将一并清除其 sleeve / 持仓 / 订单,不可撤销。`)) return;
  try {
    const data = await postJson(`/api/accounts/${encodeURIComponent(accountId)}/delete`, { force });
    msg.className = "backfill-msg ok";
    msg.textContent = `已删除 ${data.id}（清理 ${data.removed.sleeves} sleeve、${data.removed.positions} 持仓）`;
    showToast("账户已删除");
    if ($("accountFilter").value === accountId) $("accountFilter").value = "";
    await refreshAll();
  } catch (error) {
    msg.className = "backfill-msg err";
    msg.textContent = error.message;
  }
}

async function submitBackfill(event) {
  event.preventDefault();
  const msg = $("backfillMsg");
  const accountId = $("backfillAccount").value;
  const sleeveId = $("backfillSleeve").value;
  const symbol = $("backfillSymbol").value.trim().toUpperCase();
  const price = Number($("backfillPrice").value);
  const quantity = Number($("backfillQuantity").value);
  const tradeDate = $("backfillDate").value;
  // 前端先做"缺一不可"的硬校验,与后端一致。
  if (!symbol || !price || !quantity || !tradeDate) {
    msg.className = "backfill-msg err";
    msg.textContent = "代码、价格、数量、日期必填,缺一不可。";
    return;
  }
  try {
    const data = await postJson("/api/broker/backfill", {
      account_id: accountId,
      sleeve_id: sleeveId,
      symbol,
      side: $("backfillSide").value,
      quantity,
      price,
      trade_date: tradeDate,
      trade_time: $("backfillTime").value || undefined,
      apply_fees: $("backfillApplyFees").checked,
      note: $("backfillNote").value.trim(),
    });
    msg.className = "backfill-msg ok";
    msg.textContent = `已补录 ${data.side} ${data.symbol} ${data.quantity}@${data.price}（${data.timestamp.slice(0, 10)}）· 持仓 ${data.position_after}`;
    showToast("历史成交已补录");
    $("accountFilter").value = accountId;
    await refreshAll(data.source_event_id);
  } catch (error) {
    msg.className = "backfill-msg err";
    msg.textContent = error.message;
  }
}

async function importStrategy(event) {
  event.preventDefault();
  const data = await postJson("/api/strategies", {
    name: $("strategyName").value.trim(),
    code: $("strategyCode").value,
    source_filename: state.strategySourceFilename,
  });
  const adapter = data.strategy.adapter;
  const suffix = adapter && adapter.mode !== "native" ? `(已自动接入驱动: ${adapter.entry})` : "";
  showToast(`已导入策略 ${data.strategy.name}${suffix}`);
  await refreshAll();
}

async function runStrategy(event) {
  event.preventDefault();
  const strategyId = $("strategySelect").value;
  const accountId = $("strategyRunAccount").value;
  const sleeveId = $("strategyRunSleeve").value;
  const data = await postJson(`/api/strategies/${encodeURIComponent(strategyId)}/run`, {
    account_id: accountId,
    sleeve_id: sleeveId,
    data_source: $("strategyDataSource").value,
    symbols: $("strategySymbols").value,
    frequency: $("strategyFrequency").value,
    bar_limit: Number($("strategyBarLimit").value),
  });
  $("accountFilter").value = accountId;
  $("strategyFilter").value = strategyId;
  $("symbolFilter").value = $("strategySymbols").value.split(",")[0].trim().toUpperCase();
  $("chartSymbol").value = $("symbolFilter").value;
  $("chartDataSource").value = $("strategyDataSource").value;
  $("chartFrequency").value = $("strategyFrequency").value;
  $("eventTypeFilter").value = "";
  showToast(`策略运行 ${data.status}，生成 ${data.orders_submitted} 笔订单`);
  await refreshAll(data.source_event_ids?.[0] || null);
  await loadChart();
}

async function importTimingStrategy(event) {
  event.preventDefault();
  const data = await postJson("/api/timing-strategies", {
    name: $("timingName").value.trim(),
    code: $("timingCode").value,
    source_filename: state.timingSourceFilename,
  });
  const timingAdapter = data.timing_strategy.adapter;
  const timingSuffix = timingAdapter && timingAdapter.mode !== "native" ? `(已自动接入驱动: ${timingAdapter.entry})` : "";
  showToast(`已导入择时策略 ${data.timing_strategy.name}${timingSuffix}`);
  await refreshAll();
}

async function bindTimingStrategy(event) {
  event.preventDefault();
  const timingStrategyId = $("timingBindStrategy").value;
  const data = await postJson(`/api/timing-strategies/${encodeURIComponent(timingStrategyId)}/bind`, {
    strategy_id: $("timingBindStockStrategy").value,
    account_id: $("timingBindAccount").value,
    sleeve_id: $("timingBindSleeve").value,
    active: true,
  });
  $("accountFilter").value = data.binding.account_id;
  $("strategyFilter").value = timingStrategyId;
  $("eventTypeFilter").value = "timing_strategy_bound";
  showToast("择时 Gate 已绑定");
  await refreshAll();
}

async function runTimingStrategy(event) {
  event.preventDefault();
  const timingStrategyId = $("timingRunStrategy").value;
  const controlledStrategyId = $("timingRunControlledStrategy").value;
  const data = await postJson(`/api/timing-strategies/${encodeURIComponent(timingStrategyId)}/run`, {
    account_id: $("timingRunAccount").value,
    sleeve_id: $("timingRunSleeve").value,
    strategy_id: controlledStrategyId,
    data_source: $("timingRunDataSource").value,
    symbols: $("timingRunSymbols").value,
    frequency: $("timingRunFrequency").value,
    bar_limit: Number($("timingRunBarLimit").value),
  });
  $("accountFilter").value = $("timingRunAccount").value;
  $("strategyFilter").value = timingStrategyId;
  $("symbolFilter").value = $("timingRunSymbols").value.split(",")[0].trim().toUpperCase();
  $("eventTypeFilter").value = "timing_decision";
  $("chartSymbol").value = $("symbolFilter").value;
  $("chartDataSource").value = $("timingRunDataSource").value;
  $("chartFrequency").value = $("timingRunFrequency").value;
  showToast(`择时运行 ${data.status}，记录 ${data.decisions_recorded} 条决策`);
  await refreshAll(data.decision_event_ids?.[0] || null);
  await loadChart();
}

async function saveRiskConfig(event) {
  event.preventDefault();
  const payload = {
    account_id: $("riskAccount").value,
    sleeve_id: $("riskSleeve").value || null,
    enabled: $("riskEnabled").checked,
  };
  // 留空的限额不提交，表示该字段不限制(或回落到账户级配置)。
  const numberFields = [
    ["max_order_notional", "riskMaxOrderNotional"],
    ["max_sleeve_exposure", "riskMaxSleeveExposure"],
    ["max_symbol_position", "riskMaxSymbolPosition"],
    ["min_cash_buffer", "riskMinCashBuffer"],
    ["max_orders_per_tick", "riskMaxOrdersPerTick"],
    ["max_orders_per_day", "riskMaxOrdersPerDay"],
  ];
  for (const [field, inputId] of numberFields) {
    const value = $(inputId).value.trim();
    if (value !== "") payload[field] = Number(value);
  }
  const data = await postJson("/api/risk/configs", payload);
  $("accountFilter").value = payload.account_id;
  $("eventTypeFilter").value = "risk_config_updated";
  showToast(`风控配置已保存：${data.config.scope_type} ${data.config.scope_id}`);
  await refreshAll();
}

async function createSchedulerTask(event) {
  event.preventDefault();
  const data = await postJson("/api/scheduler/tasks", {
    name: $("schedulerName").value.trim(),
    account_id: $("schedulerAccount").value,
    sleeve_id: $("schedulerSleeve").value,
    strategy_id: $("schedulerStrategy").value,
    timing_strategy_id: $("schedulerTiming").value || null,
    data_source: $("schedulerDataSource").value,
    symbols: $("schedulerSymbols").value,
    frequency: $("schedulerFrequency").value,
    interval_seconds: Number($("schedulerInterval").value),
    bar_limit: Number($("schedulerBarLimit").value),
    calendar: $("schedulerCalendar").value,
    calendar_enabled: $("schedulerCalendarEnabled").checked,
    dedupe_bars: $("schedulerDedupeBars").checked,
  });
  $("accountFilter").value = data.task.account_id;
  $("strategyFilter").value = data.task.strategy_id;
  $("symbolFilter").value = data.task.symbols[0] || "";
  $("eventTypeFilter").value = "scheduler_task_created";
  showToast(`调度任务已创建：${data.task.name}`);
  await refreshAll();
}

async function handleSchedulerAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const row = button.closest(".task-row");
  const taskId = row?.dataset.taskId;
  if (!taskId) return;
  const action = button.dataset.action;
  const data = await postJson(`/api/scheduler/tasks/${encodeURIComponent(taskId)}/${action}`, action === "tick" ? { force: true } : {});
  if (action === "tick") {
    $("eventTypeFilter").value = data.tick.status === "skipped" ? "scheduler_tick_skipped" : "scheduler_tick_completed";
    showToast(`Tick ${data.tick.status}，orders ${data.tick.orders_submitted}${data.tick.skip_reason ? `，${data.tick.skip_reason}` : ""}`);
  } else {
    $("eventTypeFilter").value = action === "start" ? "scheduler_task_started" : "scheduler_task_stopped";
    showToast(`调度任务 ${action} 完成`);
  }
  await refreshAll();
  await loadChart();
}

async function loadChart() {
  const symbol = $("chartSymbol").value.trim().toUpperCase();
  if (!symbol) return;
  const params = new URLSearchParams({
    symbol,
    data_source: $("chartDataSource").value || "fixture",
    frequency: $("chartFrequency").value || "5m",
    limit: $("chartLimit").value || "120",
  });
  const markerParams = new URLSearchParams({
    symbol,
    account_id: $("accountFilter").value.trim() || "acct_a_share_alpha",
    strategy_id: $("strategyFilter").value.trim(),
  });
  for (const [key, value] of [...markerParams.entries()]) {
    if (!value) markerParams.delete(key);
  }
  const [barData, markerData] = await Promise.all([
    fetchJson(`/api/chart/bars?${params}`),
    fetchJson(`/api/chart/markers?${markerParams}`),
  ]);
  state.chart.bars = barData.bars;
  state.chart.markers = markerData.markers;
  state.chart.legendBase = `${barData.symbol} · ${barData.frequency} · ${barData.data_source} · ${barData.bars.length} bars · ${markerData.markers.length} trades`;
  $("chartLegend").textContent = state.chart.legendBase;
  renderChart();
}

// ---- TradingView lightweight-charts 图表(红涨绿跌) ----
const chartState = { chart: null, candle: null, volume: null, markers: null, markerByTime: new Map() };

function lwTime(value) {
  // 转 UNIX 秒(UTCTimestamp);分钟级 K 线必须用时间戳而非日期串。
  return Math.floor(new Date(String(value).replace(" ", "T")).getTime() / 1000);
}

function ensureChart() {
  if (chartState.chart || typeof LightweightCharts === "undefined") return;
  const container = $("klineChart");
  const chart = LightweightCharts.createChart(container, {
    autoSize: true,
    layout: {
      background: { type: "solid", color: "#0e1117" },
      textColor: "#7d8590",
      fontFamily: '"SF Mono", "JetBrains Mono", monospace',
      fontSize: 11,
    },
    grid: { vertLines: { color: "#161b22" }, horzLines: { color: "#161b22" } },
    rightPriceScale: { borderColor: "#21262d" },
    timeScale: { borderColor: "#21262d", timeVisible: true, secondsVisible: false },
  });
  const candle = chart.addSeries(LightweightCharts.CandlestickSeries, {
    upColor: "#f23645",
    wickUpColor: "#f23645",
    borderUpColor: "#f23645",
    downColor: "#089981",
    wickDownColor: "#089981",
    borderDownColor: "#089981",
  });
  const volume = chart.addSeries(LightweightCharts.HistogramSeries, {
    priceFormat: { type: "volume" },
    priceScaleId: "",
  });
  volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  chart.subscribeClick((param) => {
    if (param.time === undefined) return;
    const marker = chartState.markerByTime.get(Number(param.time));
    if (marker?.source_event_id) {
      selectEvent(marker.source_event_id).then(() => showToast("已打开该买卖点审计链路")).catch(() => {});
    }
  });
  chart.subscribeCrosshairMove((param) => {
    const data = param?.seriesData?.get(candle);
    if (!data) {
      $("chartLegend").textContent = state.chart.legendBase || "";
      return;
    }
    $("chartLegend").textContent =
      `O ${formatNumber(data.open)}  H ${formatNumber(data.high)}  L ${formatNumber(data.low)}  C ${formatNumber(data.close)}`;
  });
  chartState.chart = chart;
  chartState.candle = candle;
  chartState.volume = volume;
}

function renderChart() {
  ensureChart();
  if (!chartState.chart) return;
  // lightweight-charts 要求时间升序且唯一,先按时间去重排序。
  const byTime = new Map();
  for (const bar of state.chart.bars || []) byTime.set(lwTime(bar.timestamp), bar);
  const bars = [...byTime.entries()].sort((a, b) => a[0] - b[0]);

  chartState.candle.setData(
    bars.map(([time, bar]) => ({
      time,
      open: Number(bar.open),
      high: Number(bar.high),
      low: Number(bar.low),
      close: Number(bar.close),
    })),
  );
  chartState.volume.setData(
    bars.map(([time, bar]) => ({
      time,
      value: Number(bar.volume || 0),
      color: Number(bar.close) >= Number(bar.open) ? "rgba(242,54,69,0.5)" : "rgba(8,153,129,0.5)",
    })),
  );

  const markerByTime = new Map();
  const markers = (state.chart.markers || [])
    .map((marker) => {
      const time = lwTime(marker.time);
      markerByTime.set(time, marker);
      const isBuy = String(marker.side).toUpperCase() === "BUY";
      return {
        time,
        position: isBuy ? "belowBar" : "aboveBar",
        color: isBuy ? "#f23645" : "#089981",
        shape: isBuy ? "arrowUp" : "arrowDown",
        text: isBuy ? "B" : "S",
      };
    })
    .sort((a, b) => a.time - b.time);
  chartState.markerByTime = markerByTime;
  if (chartState.markers) {
    chartState.markers.setMarkers(markers);
  } else {
    chartState.markers = LightweightCharts.createSeriesMarkers(chartState.candle, markers);
  }
  chartState.chart.timeScale().fitContent();
}

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => {
    toast.hidden = true;
  }, 2600);
}

function resetFilters() {
  $("accountFilter").value = "acct_a_share_alpha";
  $("strategyFilter").value = "";
  $("symbolFilter").value = "";
  $("eventTypeFilter").value = "";
  state.selectedId = null;
  Promise.all([loadPortfolio(), loadOrders(), loadEvents()]).catch((error) => showToast(error.message));
}

function findStrategyName(strategyId) {
  return state.strategies.find((strategy) => strategy.id === strategyId)?.name || strategyId;
}

function findTimingName(timingStrategyId) {
  return state.timingStrategies.find((strategy) => strategy.id === timingStrategyId)?.name || timingStrategyId;
}

function formatTime(value) {
  if (!value) return "--";
  return value.replace("T", " ").replace("+08:00", "").replace("+00:00", "");
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "--";
  return Number(value).toLocaleString("en-US", { maximumFractionDigits: 2 });
}

function formatPercent(value) {
  if (value === null || value === undefined || value === "") return "--";
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function numberClass(value) {
  if (value === null || value === undefined) return "";
  return Number(value) < 0 ? "negative" : Number(value) > 0 ? "positive" : "";
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

$("applyFilters").addEventListener("click", () => {
  state.selectedId = null;
  Promise.all([loadPortfolio(), loadOrders(), loadEvents()]).catch((error) => showToast(error.message));
});
$("resetFilters").addEventListener("click", resetFilters);
$("exportCsv").addEventListener("click", () => download("csv"));
$("exportJson").addEventListener("click", () => download("json"));
$("orderAccount").addEventListener("change", () => {
  renderSleeveOptions();
  updateTicketEstimate();
});
$("orderSleeve").addEventListener("change", updateTicketEstimate);
$("orderQuantity").addEventListener("input", updateTicketEstimate);
$("orderPriceType").addEventListener("change", togglePriceType);
$("orderSymbol").addEventListener("change", () => refreshTicketQuote().catch(() => {}));
$("orderForm").addEventListener("click", (event) => {
  const sideButton = event.target.closest(".side-toggle button[data-side]");
  if (sideButton) {
    setTicketSide(sideButton.dataset.side);
    return;
  }
  const qtyButton = event.target.closest(".qty-quick button[data-qty]");
  if (qtyButton) applyQuickQty(Number(qtyButton.dataset.qty));
});
$("watchlistAddForm").addEventListener("submit", (event) => addWatchlist(event).catch((error) => showToast(error.message)));
$("watchlist").addEventListener("click", (event) => handleWatchlistClick(event).catch((error) => showToast(error.message)));
$("blotter").addEventListener("click", (event) => handleBlotterClick(event).catch((error) => showToast(error.message)));
$("recordSnapshot").addEventListener("click", () => recordSnapshot().catch((error) => showToast(error.message)));
$("perfBenchSource").addEventListener("change", () => loadPerformance().catch((error) => showToast(error.message)));
$("backtestForm").addEventListener("submit", (event) => runBacktest(event).catch((error) => showToast(error.message)));
$("btHistory").addEventListener("click", (event) => {
  const row = event.target.closest(".bt-history-row");
  if (row?.dataset.id) loadBacktest(row.dataset.id).catch((error) => showToast(error.message));
});
$("btExportCsv").addEventListener("click", () => exportBacktest("csv"));
$("btExportJson").addEventListener("click", () => exportBacktest("json"));
$("strategyRunAccount").addEventListener("change", renderStrategyRunSleeves);
$("timingBindAccount").addEventListener("change", renderTimingSleeves);
$("timingRunAccount").addEventListener("change", renderTimingSleeves);
$("schedulerAccount").addEventListener("change", renderSchedulerSleeves);
$("riskAccount").addEventListener("change", renderRiskSleeves);
$("riskForm").addEventListener("submit", (event) => saveRiskConfig(event).catch((error) => showToast(error.message)));
$("sidebarNav").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-view]");
  if (button) switchView(button.dataset.view);
});
$("refreshConnectors").addEventListener("click", () => loadStrategies().then(() => showToast("数据源状态已刷新")).catch((error) => showToast(error.message)));
$("dataConnectors").addEventListener("click", (event) => {
  if (event.target.closest("button[data-action='save-rq-key']")) {
    saveRiceQuantKey().catch((error) => showToast(error.message));
  }
  if (event.target.closest("button[data-action='save-wind']")) {
    saveWindConfig().catch((error) => showToast(error.message));
  }
});
$("defaultDataSource").addEventListener("change", (event) =>
  setDefaultDataSource(event.target.value).catch((error) => showToast(error.message)),
);

async function setDefaultDataSource(source) {
  // 持久化到服务端(后端各接口随之跟随),并一键把全站所有数据源下拉改成它。
  await postJson("/api/settings/data-source", { default_data_source: source });
  state.defaultDataSource = source;
  for (const id of DATA_SOURCE_SELECTS) {
    if (state.connectors.some((c) => c.name === source)) $(id).value = source;
  }
  showToast(`默认数据源已一键设为 ${source}（各板块仍可单独覆盖）`);
  // 用新数据源刷新依赖行情的视图。
  await refreshAll().catch(() => {});
}
$("strategyBoard").addEventListener("click", (event) => handleStrategyBoardAction(event).catch((error) => showToast(error.message)));
$("loadQuote").addEventListener("click", () => loadQuote().catch((error) => showToast(error.message)));
$("storagePick").addEventListener("click", () => handleStoragePick().catch((error) => showToast(error.message)));
$("storageReset").addEventListener("click", () => resetStorageLocation().catch((error) => showToast(error.message)));
$("accountForm").addEventListener("submit", (event) => createAccount(event).catch((error) => showToast(error.message)));
$("sleeveForm").addEventListener("submit", (event) => createSleeve(event).catch((error) => showToast(error.message)));
$("orderForm").addEventListener("submit", (event) => submitOrder(event).catch((error) => showToast(error.message)));
$("orderBook").addEventListener("click", (event) => handleOrderBookAction(event).catch((error) => showToast(error.message)));
$("repoForm").addEventListener("submit", (event) => runReverseRepo(event).catch((error) => showToast(error.message)));
$("repoRateMode").addEventListener("change", () => applyRepoRateMode().catch(() => {}));
$("repoSymbol").addEventListener("change", () => applyRepoRateMode().catch(() => {}));
$("repoAccount").addEventListener("change", (event) =>
  setActiveAccount(event.target.value).catch((error) => showToast(error.message)),
);
$("backfillForm").addEventListener("submit", (event) => submitBackfill(event).catch((error) => showToast(error.message)));
$("backfillAccount").addEventListener("change", renderBackfillSleeves);
$("deleteAccountBtn").addEventListener("click", () => deleteAccount().catch((error) => showToast(error.message)));
$("strategyFile").addEventListener("change", (event) =>
  loadPythonFile(event, {
    textareaId: "strategyCode",
    nameInputId: "strategyName",
    sourceKey: "strategySourceFilename",
  }).catch((error) => showToast(error.message)),
);
$("strategyList").addEventListener("click", (event) => handleManagedDelete(event, "strategy").catch((error) => showToast(error.message)));
$("timingList").addEventListener("click", (event) => handleManagedDelete(event, "timing").catch((error) => showToast(error.message)));
$("strategyImportForm").addEventListener("submit", (event) => importStrategy(event).catch((error) => showToast(error.message)));
$("strategyRunForm").addEventListener("submit", (event) => runStrategy(event).catch((error) => showToast(error.message)));
$("timingFile").addEventListener("change", (event) =>
  loadPythonFile(event, {
    textareaId: "timingCode",
    nameInputId: "timingName",
    sourceKey: "timingSourceFilename",
  }).catch((error) => showToast(error.message)),
);
$("timingImportForm").addEventListener("submit", (event) => importTimingStrategy(event).catch((error) => showToast(error.message)));
$("timingBindForm").addEventListener("submit", (event) => bindTimingStrategy(event).catch((error) => showToast(error.message)));
$("timingRunForm").addEventListener("submit", (event) => runTimingStrategy(event).catch((error) => showToast(error.message)));
$("schedulerForm").addEventListener("submit", (event) => createSchedulerTask(event).catch((error) => showToast(error.message)));
$("schedulerTasks").addEventListener("click", (event) => handleSchedulerAction(event).catch((error) => showToast(error.message)));
$("refreshPortfolio").addEventListener("click", () => loadPortfolio().catch((error) => showToast(error.message)));
$("loadChart").addEventListener("click", () => loadChart().catch((error) => showToast(error.message)));
// 统一切换活动账户:顶栏选择器与逆回购卡都走这里,保证全站(组合/订单/审计/逆回购记录/侧栏)一致。
function setActiveAccount(id) {
  if (!id) return Promise.resolve();
  $("accountFilter").value = id;
  $("topAccountSelect").value = id;
  if (state.accounts.some((a) => a.id === id)) $("repoAccount").value = id;
  state.selectedId = null;
  return Promise.all([loadPortfolio(), loadOrders(), loadEvents(), loadReverseRepo()]).then(updateRepoAmountDefault);
}

$("topAccountSelect").addEventListener("change", (event) =>
  setActiveAccount(event.target.value).catch((error) => showToast(error.message)),
);

// ----------------------- 全局刷新 + 自动刷新 ----------------------- //
let autoRefreshTimer = null;

async function manualRefresh(btnId) {
  const btn = btnId ? $(btnId) : null;
  if (btn) btn.classList.add("refreshing");
  try {
    await refreshAll();
    await refreshTicketQuote().catch(() => {});
  } finally {
    if (btn) btn.classList.remove("refreshing");
  }
}

function setupAutoRefresh() {
  state.autoRefresh = $("autoRefreshToggle")?.checked ?? true;
  if (autoRefreshTimer) clearInterval(autoRefreshTimer);
  // 每 20 秒拉一次,让 agent(或其他窗口)干完活后无需重开即可看到更新。
  autoRefreshTimer = setInterval(() => {
    if (!state.autoRefresh || document.hidden) return;
    const ae = document.activeElement;
    // 用户正在输入/选择时跳过,避免打断操作。
    if (ae && ["INPUT", "SELECT", "TEXTAREA"].includes(ae.tagName)) return;
    refreshAll().catch(() => {});
  }, 20000);
}

$("globalRefresh").addEventListener("click", () => manualRefresh("globalRefresh").catch((e) => showToast(e.message)));
$("perfRefresh").addEventListener("click", () => {
  const btn = $("perfRefresh");
  btn.classList.add("refreshing");
  loadPerformance().catch((e) => showToast(e.message)).finally(() => btn.classList.remove("refreshing"));
});
$("autoRefreshToggle").addEventListener("change", (event) => {
  state.autoRefresh = event.target.checked;
  showToast(state.autoRefresh ? "自动刷新已开(每 20 秒)" : "自动刷新已关");
});

window.paperApp = {
  state,
  loadChart,
  selectEvent,
  switchView,
};

switchView("overview");
setTicketSide("BUY");
refreshAll()
  .then(() => refreshTicketQuote())
  .catch((error) => {
    $("eventsTable").innerHTML = `<tr><td colspan="8">加载失败：${escapeHtml(error.message)}</td></tr>`;
  })
  .finally(() => setupAutoRefresh());
