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

---

# 对接回执 v2 —— Admin 回应 APP_HANDOFF §4(均已实现并实测)

Admin 侧已落地账户级登记接收端(单条 / 批量 / 注销),以下为**实测**确认(非纸面)。登记接收端已就绪,随时可联调。

## ① register 端点契约 —— 确认
- `POST /api/admin/accounts/register`,接受你 §2 原样报文,返回 `201 { "node_id": "...", "accounts": [ { ..., "_action": "created|updated" } ] }`。
- 幂等:主键 `(node.id, account.id)`,重复 = 更新;node 段先 upsert 节点(传输层),account 段写账户注册表。
- `owner` 缺省回退 `name`(与你一致)。
- **token 口径(重要,别混)**:Admin 设了 `ADMIN_TOKEN` 才校验请求头 `X-Admin-Token`,且校验的是 **Admin 的共享密钥**,不是 `node.token`。
  - `node.token`(报文 body 里那个)是「Admin 反向调用你节点」用的(对应你 §4.4 的节点鉴权),与登记调用的请求头 token 是**两个不同的 token**。
  - 你「配了就带、没配就不带」可以,但请把登记请求头 `X-Admin-Token` 设成 **Admin 共享密钥**;Admin 没开 `ADMIN_TOKEN` 时带不带都放行。

## ② 账户注销端点 —— 确认(可马上接)
- 路径就是它:`POST /api/admin/accounts/{node_id}/{account_id}/delete`
- **body**:不需要(忽略;可选 `{"reason":"..."}`,Admin 不强制)。
- **token**:与 register 同口径(Admin 开了 `ADMIN_TOKEN` 才需带 `X-Admin-Token` = 共享密钥)。
- 返回 `200 { "deregistered": true|false }`(false = 本就未登记)。
- 你在 `delete_account` 成功后调它即可。注:不接也能靠「summary 里不再出现该 account.id」判下线,但**显式注销更干净**——立即从墙上移除,不残留「最后已知」。

## ③ 启动批量登记 —— 倾向:同一端点的 `accounts:[]`
- register 端点已支持批量:`{"node":{...}, "accounts":[{...},{...}]}` 一次登记多账户(已实测)。
- **倾向**:app 启动(且配了 `admin_url`)时调一次 register-all,打到**同一个** `/api/admin/accounts/register`,body 用 `accounts:[]`。不必再用 `/api/admin/register` 的 accounts 字段——统一一个入口。
- 理由:Admin 可能重启 / 重置 DB,启动补登能自愈,不依赖逐个开户事件。建议**启动登一次 + Admin 不可达时重试**。

## ④ node_patch 优先级 —— 我的排序
同意你「绑 LAN 后无鉴权 = 同网段裸奔」,**安全优先**:
1. **节点 admin-token 校验(高,先上)** —— 绑 0.0.0.0 后没有它,同网段任何人都能拉数据 / 远程开户。必做。
2. **`/api/stream` SSE(中)** —— 接上后 Admin 从轮询切事件驱动、成交秒级上墙。值得做,但不阻塞。
3. **启动自注册(低,基本可省)** —— 你的 register 报文已自带 `node.base_url`(LAN 出口 IP),Admin 已知道怎么连你,自注册与之重复。除非你想要「节点起了但还没开任何账户时也出现在 Admin 上」,否则不必单做。

## ⑤ api_version=1 —— 收到
Admin 已读 `/api/meta` 的 `api_version` 做兼容判断;破坏性变更你 +1 即可。

## Admin 侧接下来(不阻塞你)
账户级监控墙:轮询节点 summary 后按**已登记**账户拆分,账户成卡、按 `owner` 分组排名;反向开户成功后我这边也会顺带登记(与你的主动登记幂等,不冲突)。

---

# 对接回执 v3 —— 账户级联调通过 + 监控墙上线(Admin 侧)

收到你 v1.10.1 回执。**端到端联调已通过**(打的是你真实 app,非 mock):
- 开户 → 单条登记 ✓
- 删账户 → 注销(`/api/admin/accounts/{node}/{acct}/delete`,空 body)✓
- 重启 → 批量补登(`accounts:[]`)✓
- node_id 跨重启稳定、与注销路径一致 ✓

Admin 侧账户级监控墙已上线:按已登记账户成卡(按 owner 分组排序、显示所属节点)、按账户收益率排行、下钻按 account_id 过滤持仓/成交、账户级 sparkline。账户实时指标从你节点 `/api/portfolio/summary` 的 `accounts[]` 里按 id 取(单一真相,不双写)。

## 对你这边:无新增契约要求,现有 v1.10.1 已完整对接

## 下一步(沿用你我排的优先级)
1. **节点 admin-token 鉴权(高,你说下一轮做)** —— 做完把生成的 token 作为登记报文里的 `node.token` 传上来;我反控你(远程开户)时会带 `X-Admin-Token = node.token`,Admin 侧 control 代理已支持,你那边校验即可。
2. **`/api/stream` SSE(中)** —— 接上后我从轮询切事件驱动、成交秒级上墙。
3. 自注册(低,可省,register 报文已带 base_url)。

## 一个提醒(不阻塞)
监控墙要显示实时盈亏,需节点数据源可用。你节点默认 `tongdaxin` 要行情网络;联调时我用 `fixture` 才出数。生产没问题;若某节点数据源不通,我这边可对该节点单独指定 data_source 兜底。

握手:`/api/meta`,api_version=1。有进展我会继续更新本文件。

---

# 对接回执 v4 —— 节点鉴权已对接并实测(Admin 侧)

收到你 v1.11.0「节点 admin-token 鉴权」。**已确认 Admin 的轮询与反控都带 `X-Admin-Token = node.token`**:

- `node_client` 对每个出站请求(`/api/meta`、`/api/portfolio/summary`、`/api/audit/trades`、反控 `POST /api/accounts`)统一加 `X-Admin-Token`,值取该节点登记报文里的 `node.token`(已落 Admin 注册表)。header 名与你一致。
- 我没法在本机复现你的 401(本机连节点 LAN IP 被你按 loopback 豁免、一律 200),所以改用**强制校验 token 的 mock 节点**确证:带对的 token → 在线;把 token 改错 → 节点 401 → Admin 判离线。**poller 与 control 确实在带 token**,非纸面。
- node.token 已从你最近一次登记 / 批量补登里收到(非空),Admin 自动使用,无需手配。

**运维提醒(已写进运行手册 FAQ)**:某节点开了鉴权但 Admin 还没拿到它的 `node.token`(比如鉴权上线前就登记过)→ 会 401 变离线;**让该节点重启一次**(register-all 带上最新 token),Admin 拿到即恢复。

下一步等你的 **`/api/stream` SSE**(端点走入站鉴权,我消费时会带 node.token)。接上后我从轮询切事件驱动。

---

# 对接回执 v5 —— /api/stream SSE 已对接(Admin 侧)

收到 v1.12.0 SSE。**Admin 已切到事件驱动 + 轮询兜底**:

- 每个节点起一条 SSE 长连消费线程,连 `GET /api/stream`,带 `X-Admin-Token = node.token`(远程鉴权)。
- 解析:`data:` 行视为变更触发器 → 立即重拉该节点 `summary` + `trades`(单一真相不变);`:` 开头(`: connected` / `: ping` 心跳)忽略,不触发。
- 断连 / 节点重启 → 自动重连;期间低频轮询兜底。`capabilities.event_stream` 为探测位(不支持的节点走 404 退避,纯轮询)。

**修了一处对接 bug**:我早期消费者连的是 `/api/events/stream`(旧草案路径),已改成你的 `/api/stream`。否则会一直 404、SSE 连不上。

**实测**:
- 抓真实节点 v1.12.0 的 `/api/stream`:`: connected` + `data:{"type":"account_created",...}`,格式与契约逐字节一致;消费者对 `data:` 触发、对 `:` 忽略 ✓。
- 强制 token 的 mock:消费者带对 `node.token` 才连上并触发,不带 → 401 连不上 ✓。

至此账户级登记 / 节点鉴权 / SSE 三步全部双向打通。后续加事件类型(风控 / 逆回购)随时说,我收到即重拉。
