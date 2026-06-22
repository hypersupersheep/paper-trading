# 对接交接 —— 账户级登记与监控

致:paper trading app 维护方。
本文定义 app 与 Admin 之间的账户级对接契约。术语与既有约定见 [`CONTRACTS.md`](CONTRACTS.md)。

---

## 1. Admin 侧进度(现状)

已上线并发布 v1.0.0(纯 Python 标准库 + SQLite + 原生 JS):

- **节点级监控**:Admin 主动轮询每个节点的 `/api/portfolio/summary` 与 `/api/audit/trades`,聚合成实时监控墙、排行榜、下钻、连通性告警。
- **节点登记**:手动添加,或节点自注册 `POST /api/admin/register`。
- **反向控制**:`POST /api/admin/nodes/{id}/control` 代理到节点(已用于远程 `POST /api/accounts` 开户)。
- **实时**:Admin 端 SSE 推送;节点装可选补丁后,成交事件触发秒级重拉。
- **隔离**:Admin 只读;唯一写操作是显式远程开户;Admin 宕机不影响节点;节点离线保留最后已知。

当前监控单元是**节点**。本次对接要把单元细化到**账户**。

## 2. 目标

> 每个账户在 Admin 登记后,受 Admin 监控。

- **节点 (node)** = 一个运行中的 app 实例,是**传输层**(`base_url`),负责连通。
- **账户 (account)** = **监控单元**。一个节点可含多个账户。
- Admin 对每个**已登记**账户单独成卡、单独排名;未登记的账户即使存在于节点也不展示(登记是纳管开关)。

## 3. 对接契约

### 3.1 账户登记(app → Admin)

app 在「开户 / 登记」动作发生时,调用 Admin 新增端点:

```
POST /api/admin/accounts/register
Content-Type: application/json
X-Admin-Token: <若 Admin 开启鉴权>
```
```json
{
  "node": {
    "id": "alice-mbp",
    "name": "Alice 的机器",
    "base_url": "http://192.168.1.23:8000",
    "token": "<节点 admin-token,可空>",
    "api_version": 1
  },
  "account": {
    "id": "acct_xxx",
    "owner": "Alice",
    "name": "主账户",
    "currency": "CNY",
    "market": "CN_A",
    "initial_cash": 10000000
  }
}
```

- `node` 段用于让 Admin 知道**从哪连**;Admin 内部 upsert 节点(复用现有节点登记逻辑)。
- `account` 段是账户身份。`id` 必须是节点内稳定标识(用节点 DB 里的 account id 即可)。
- **幂等**:主键 `(node.id, account.id)`,重复调用为更新。
- 返回 `201 { "account": { ... } }`。

可选批量:`POST /api/admin/register` 扩展一个 `accounts: [ {account 段}, ... ]` 字段,启动时一次性登记该节点全部账户。

### 3.2 监控数据(Admin → app,沿用现有读接口,app 无需新增)

Admin 仍按**节点**轮询一次,再按已登记账户拆分,**不逐账户发请求**:

- `GET /api/portfolio/summary?data_source=<ds>&frequency=5m`
  - Admin 从返回的 `accounts[]` 里按 `id` 匹配已登记账户,取每账户的
    `equity / pnl / pnl_pct / exposure / market_value / unrealized_pnl / day_pnl`。
  - **要求**:`accounts[].id` 稳定且与登记时一致(已满足)。
- `GET /api/audit/trades?limit=50`
  - 行内含 `account_id`,Admin 按其归属到对应账户。

即:**3.2 不需要 app 改动**,现有契约已够。app 只需保证账户 id 稳定。

### 3.3 账户在线判定

账户 `online` = 其所属节点在线(轮询成功)**且** 该 `account.id` 出现在最近一次 summary 的 `accounts[]` 中。
节点离线 → 其下所有账户标离线并显示最后已知。账户被删 → app 调 `POST /api/admin/accounts/{node_id}/{account_id}/delete` 注销(或随节点删除连带清理)。

## 4. 分工

### app 侧(你)需要做
1. **账户身份**:账户具备稳定 `id` 与 `owner`(交易员)字段。`id` 现已有;`owner` 建议新增(没有就先用账户 `name` 兜底)。
2. **登记调用**:在开户 / 登记成功后,`POST /api/admin/accounts/register`(§3.1)。
   - 远程开户(经 Admin `control` 代理)同样要触发登记 —— 简单做法:开户接口成功后无条件调一次登记,Admin 端幂等。
3. **(可选)** 沿用 [`node_patch`](node_patch/PATCH.md):自注册免填 IP、写鉴权、SSE 秒级。

### Admin 侧(我)负责做
1. 新增 `accounts` 注册表与 `POST /api/admin/accounts/register` / 注销端点。
2. 轮询后按已登记账户拆分,落账户级 state 与时序;监控墙/排行榜切到账户粒度(按 owner / node 分组)。
3. 远程开户成功后自动登记该账户(免去 app 端额外调用,二者择一即可)。

## 5. 时序

```
app 启动
  └─(可选)节点自注册 ──────────────► Admin: upsert node
开户(本地 或 Admin 远程 control)
  └─ POST /api/admin/accounts/register ─► Admin: upsert (node, account)  ← 纳入监控
Admin 轮询循环(每 2–5s,或 SSE 事件触发)
  └─ GET node /portfolio/summary, /audit/trades
       └─ 按已登记账户拆分 → 账户上墙 / 排名 / 告警
账户删除
  └─ POST /api/admin/accounts/{node}/{acct}/delete ─► Admin: 注销
```

## 6. 待你确认的点

1. **owner 字段**:app 的账户能否带交易员标识?没有的话先用 `name`,排行榜按账户名显示。
2. **一人一账户 还是 一人多账户**:若每位同事恒为单账户,node 与 account 可一一对应、模型可再简化;若要支持多账户(多策略分仓),按本文 node↔account 一对多。
3. **登记触发点**:由 app 主动调登记,还是由 Admin 在远程开户后自动登记?两条路都支持,确认主用哪条以免重复(幂等不会出错,只是省一次调用)。

确认后我即落地 Admin 侧的 §4 三项。
