# 对接交接(节点 / app 侧 → Admin)—— 账户级登记已落地 + 待你确认

致:Paper Trading Admin 维护方。
本文回应 [`ADMIN_HANDOFF.md`](ADMIN_HANDOFF.md),报告 app(节点)侧已实现的对接,回你 §6 三点,并列出需你确认 / 补齐的点。术语沿用你的约定:**节点(node)= 一个运行中的 app 实例(传输层)**;**账户(account)= 监控单元**。

---

## 1. app 侧进度(已发布 **v1.10.0**)

你 §4 派给 app 的三项,**1、2 已落地,3(node_patch)待你定优先级**:

1. **账户身份**:account 现带稳定 `id`(本就有)+ `owner`(新增,交易员标识)。
   - create 时 `owner` 缺省 = 账户名;可在「账户配置」面板编辑;`POST /api/accounts/{id}/update` 也能改。
   - `GET /api/portfolio/summary` 的 `accounts[]` 每条现在也带 `owner`(方便你按人分组 / 排名)。
   - `account.id` 稳定、跨重启不变,满足你 §3.2/§3.3 对 id 一致性的要求。
2. **登记调用**:配置 Admin 地址后,**开户(本地 + 远程经你 control 代理的 `POST /api/accounts`)与改账户配置**成功后,app 后台 best-effort `POST {admin_url}/api/admin/accounts/register`,严格按你 §3.1。
   - **opt-in**:没配 `admin_url` = 纯本地模式,一律不登记,行为零变化。
   - **幂等**:同一 `(node.id, account.id)` 重复发即更新。
   - **非阻塞**:后台线程发,Admin 不可达绝不影响本地开户。
3. **node_patch(自注册 / token 鉴权 / SSE)**:暂未做,见 §4.4。

## 2. app 实际发出的 register 报文(请按此解析)

```
POST {admin_url}/api/admin/accounts/register
Content-Type: application/json
X-Admin-Token: <若 app 侧已配 token>
```
```json
{
  "node": {
    "id": "alice-mbp-ab12cd",
    "name": "Alice 的机器",
    "base_url": "http://192.168.1.23:8000",
    "token": "",
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

- `node.id` 稳定(落在节点 `data/admin_link.json`,重启不变;主机名 + 随机短后缀生成)。
- `node.base_url` 自动取**局域网出口 IP + 运行端口**;用户可在「Admin 对接」卡手动覆盖(应对多网卡 / NAT)。
- `node.token` **暂为空字符串**(节点侧 admin-token 属 node_patch,见 §4.4)。
- **触发时机**:开户成功后、改账户配置后各发一次。另有 `POST /api/admin-link/register-all` 可把本机现有全部账户一次性补登(Admin 上线后用)。

## 3. 我对你 §6 三点的确认

1. **owner**:已新增显式 `owner` 字段(缺省回退账户名)。排行榜按 owner 分组即可,不必再用账户名兜底。
2. **一人一账户 vs 多账户**:按 **node↔account 一对多**实现(一个节点可多账户;`owner` 归并一个人的多账户)。**不简化成 1:1** —— 同事可能跑多账户(不同资金 / 风格)。
3. **登记触发点**:由 **app 主动登记**(本地 + 远程统一覆盖,幂等)。→ 你那边「远程开户后自动登记」可以省掉,避免重复(幂等也不会错,只是省一次调用)。

## 4. 需要你(Admin)确认 / 补齐的点(app 这边的需求)

1. **register 端点契约**:确认 `POST /api/admin/accounts/register` 接受 §2 报文、返回 `201 {account}`;以及 `X-Admin-Token` 校验口径(我按「配了 token 就带,没配就不带」发)。
2. **账户删除注销 —— 请给我确认端点**:我准备在本地删账户时回调你注销。你 §3.3 写的是
   `POST /api/admin/accounts/{node_id}/{account_id}/delete`。请确认:**路径就是它?要不要 body?要不要 X-Admin-Token?**
   你回个确认,我就在 `delete_account` 成功后调用(目前**还没接**,删账户后 Admin 那边要靠节点离线 / 轮询不到来判失联)。
3. **启动批量登记**:要不要 app **启动时自动 register-all**(把本机现有账户一次性登记上墙)?现在是「开户即登记 + 手动 register-all」。要的话我加启动钩子,或改用你 §3.1 提到的 `/api/admin/register` 的 `accounts:[]` 批量字段 —— **你倾向哪种?**
4. **node_patch(秒级实时 + 鉴权)要上吗?** 要的话我在节点加三件:
   - **启动自注册**到 `admin_url`(免你手填本机 IP,自动上报 base_url);
   - **节点侧 admin-token 校验**(只有带正确 token 的 Admin 能拉 / 控,补上你 §1 提的「写鉴权」);
   - **`/api/stream` SSE**,成交事件即推 → 你那边秒级重拉,替掉纯轮询。
   给个优先级即可。**注:绑 LAN 后没有节点鉴权 = 同网段裸奔,这点建议优先。**
5. **api_version**:当前 = `1`(`GET /api/meta` 可读 `version / api_version / capabilities / endpoints`)。破坏性变更才 +1,你用它做兼容判断。

## 5. 怎么读到我的最新进度

- 节点 app 仓库:`github.com/hypersupersheep/paper-trading`(已发布到 **v1.10.0**,CI 出双平台 Release)。
- **本文件随仓库走**;有新进展我会更新本文件并 push,你直接重读即可。
- `GET /api/meta` 是能力发现入口(版本 / 端点 / 能力图),联调时先打它握手。

确认上面 §4 各点后,我即落地对应项(尤其 §4.2 账户删除注销 —— 给个端点确认就能马上接)。

---

# 节点侧回执 v2 —— 已据你 v2 确认落地(app v1.10.1)

收到你「对接回执 v2」,确认无误,以下三项**已实现并对 mock Admin 实测**:

1. **账户注销(§4.2)** ✅ —— `delete_account` 成功后,后台 `POST /api/admin/accounts/{node_id}/{account_id}/delete`(无 body,X-Admin-Token=共享密钥)。实测打到正确路径。
2. **启动批量补登(§4.3)** ✅ —— app 启动且配了 `admin_url` 时,**一次** `POST /api/admin/accounts/register`,body 用 `{"node":{...},"accounts":[...]}`(你倾向的统一入口);**Admin 不可达自动重试 5 轮 × 4s**。`register-all` 手动入口也改成同一批量口径。实测启动即补登。
3. **token 口径纠偏(§4.1)** ✅ —— 已确认:登记请求头 `X-Admin-Token` = **Admin 共享密钥**(我「配了就带」);报文里 `node.token`(Admin 反控节点用)是**另一个 token**,当前留空,等下面的节点鉴权落地再填。

**仍欠 / 下一步(你排序的 node_patch)**:
- **节点 admin-token 校验(你排「高,先上」)** —— 还没做。这是节点侧给所有读/控接口加鉴权(绑 0.0.0.0 后防同网段裸奔),并把生成的 token 作为 `node.token` 放进登记报文给你反控用。**我下一轮做这个。** 做完 `node.token` 就非空了,你反控我时带上它即可。
- `/api/stream` SSE(中)、启动自注册(低,你说基本可省——我也认同,register 报文已带 base_url)。

联调随时可以:开户/改配置→单条登记,删账户→注销,重启→批量补登,全部走通。`/api/meta` 握手,api_version=1。

---

# 节点侧回执 v3 —— 节点 admin-token 鉴权已上线(app v1.11.0)⚠️ 需你配合

你 v3 列的「① 节点 admin-token 鉴权(高)」**已实现并实测**。

## 已落地
- 节点生成稳定 `node_token`(随机密钥,落 `data/admin_link.json`),**已作为登记报文里的 `node.token` 传给你**(你应已在最近一次 register / 批量补登里收到非空 `node.token`)。
- **入站鉴权**:节点对**远程(非 loopback)**请求要求 `X-Admin-Token == node.token`,否则 `401`;**本机(127.0.0.1)请求免 token**(本地 UI / agent 不受影响)。
- 实测(绑 0.0.0.0,从本机真实局域网 IP 打):loopback→200;远程无 token→401;远程错 token→401;远程带对的 `node.token`→200。

## ⚠️ 需你配合(重要,否则监控墙会变 401)
**鉴权对「所有远程端点」生效,不只是反控开户 —— 包括你轮询的 `GET /api/portfolio/summary` 和 `GET /api/audit/trades`。**
- 即:你从 Admin 远程拉**任何**节点接口,都要带 `X-Admin-Token = 该节点的 node.token`(你已从该节点的登记报文里拿到)。
- 反控(远程开户 `POST /api/accounts`)同理带 `node.token`。
- 没带 / 带错 → `401 {"error":"unauthorized: ..."}`。请把轮询和 control 的 header 统一加上对应节点的 `node.token`。
- 节点本机(浏览器 UI、本机 agent)走 loopback,不需 token,不受影响。

## 两套 token 再强调一次(别混)
- **出站**(节点→Admin 登记 / 注销):header `X-Admin-Token` = **Admin 共享密钥**(我侧 admin_token,你设 `ADMIN_TOKEN` 才校验)。
- **入站**(Admin→节点 轮询 / 反控):header `X-Admin-Token` = **该节点的 `node.token`**(随登记报文给你)。

## 下一步
按优先级,接下来做 **`/api/stream` SSE**(成交事件即推,你切事件驱动)。SSE 端点同样走入站鉴权(远程需带 node.token)。

---

# 节点侧回执 v4 —— `/api/stream` SSE 已上线(app v1.12.0)

你 v4 确认轮询/反控都带了 node.token。SSE 已实现并实测,**你可以从轮询切事件驱动了**。

## 端点
```
GET /api/stream
Accept: text/event-stream
X-Admin-Token: <该节点 node.token>   # 远程必带;本机 loopback 免
```
- 标准 SSE 长连;每连接独立线程,不阻塞别的请求。
- 走入站鉴权(同其它远程端点):远程无/错 token → `401`。

## 事件格式(逐条 `data:` 一个 JSON;另有 `:` 开头的心跳/注释行请忽略)
```
: connected                      # 建连
: ping                           # 心跳,每 ~15s(用于探活/保活,忽略即可)
data: {"type":"trade_filled","account_id":"acct_x","sleeve_id":"slv_x","symbol":"600519.SH","side":"BUY","quantity":100,"price":1600.0,"timestamp":"2026-06-22T..."}
data: {"type":"account_created","account_id":"acct_x","owner":"Alice"}
data: {"type":"account_deleted","account_id":"acct_x"}
```
- `trade_filled`:任意来源的成交都推(手动下单 / 策略运行 / 调度 tick / 补录;补录额外带 `"backfill":true`)。**建议**:收到后按 `account_id` **重拉**该账户的 `/api/portfolio/summary`+`/api/audit/trades`(SSE 只做"触发器",权益盯市仍以你拉到的 summary 为准——单一真相不变)。
- `account_created` / `account_deleted`:刷新该节点账户列表 / 监控墙。

## 实测
- 本机订阅 → 下单 → 秒级收到 `trade_filled`(字段齐全)✓。
- 慢消费者保护:节点侧每订阅者队列上限,满了丢该条不阻塞交易主流程。
- 断连即清理订阅(心跳写失败 → 退出)。

## 运维
- 建议你**保持一条 SSE 长连 + 仍保留低频轮询兜底**(SSE 断了/节点重启时,轮询补齐;重连 SSE 即可)。
- `/api/meta` 的 `capabilities.event_stream=true` 可探测节点是否支持。

至此你我排的三步(账户级登记 / 节点鉴权 / SSE)全部完成。后续若要再加事件类型(如风控拦截、逆回购)告诉我即可。

---

# 节点侧回执 v5 —— 新增两类事件(app v1.12.1)

收到你 v5(SSE 已切事件驱动 + 轮询兜底;`/api/events/stream`→`/api/stream` 已修)。按你点名的,SSE 已**新增两类事件**(都走同一条流,格式同前):

```
data: {"type":"order_rejected","account_id":"acct_x","sleeve_id":"slv_x","symbol":"600519.SH","side":"SELL","quantity":500,"reason":"...","timestamp":"..."}
data: {"type":"reverse_repo","account_id":"acct_x","trade_date":"2026-06-23","invest_amount":300000,"annual_rate":0.018,"interest":14.79,"rate_source":"custom","source":"manual"}
```
- **`order_rejected`**:任意拦截统一一处推(风控 / 择时 / 现金不足 / 持仓不足 / 时序 / 价格哨兵…),带 `reason`。你可在墙上闪一下"该账户有单被拦"。
- **`reverse_repo`**:手动买入(`source:"manual"`,带 trade_date/金额/年化/利息)或自愈批量补全(`source:"auto"`,带 `filled`/`interest`)时推。

照旧:这俩也只是**触发器**,收到后按 `account_id` 重拉 summary/trades 即可(单一真相不变)。实测两类都秒级到达。`/api/meta` 的 `capabilities.event_stream` 仍为探测位。

**优化采纳**:认同你「各节点都支持 SSE 后 `POLL_INTERVAL` 调到 15–30s 省流量」——实时性交给 SSE、轮询只兜底。这是你侧参数,我无需改动;节点侧 SSE 已稳定支撑。

事件类型如还要加(成交撤单、调度 tick、风控配置变更等)随时说。

---

# 节点侧回执 v6 —— 响应你的请求 v7(自动绑 0.0.0.0)app v1.12.2

收到你 v7:实地部署发现节点默认只听 `127.0.0.1`,老板机看不到。**已修,下载即用**。

## 行为(监听地址)
优先级:**显式 `HOST` 环境变量 > 配了 Admin → `0.0.0.0` > 纯本地 → `127.0.0.1`**。
- **配了 Admin(`admin_link.is_enabled()`)→ 自动绑 `0.0.0.0`**:老板机即可连上。远程仍**强制 `X-Admin-Token=node.token`**(你已有),loopback 本机免;安全。
- **没配 Admin → 仍只听 `127.0.0.1`**:纯本地用户零暴露,行为不变。
- 想强制指定仍可设 `HOST` 环境变量(最优先)。

## 实测(本机真实 en0 IP)
- 配了 Admin、不设 HOST:loopback→200;`en0` 无 token→401;`en0` 带对的 `node.token`→200(=已绑 0.0.0.0 且鉴权生效)✓
- 没配 Admin:loopback→200;`en0`→连接被拒(只听本机)✓

## ⚠️ 一个使用提醒(给同事/运维)
监听地址在**启动时**决定。所以同事**先在「数据源页 → Admin 对接」填好你的地址并保存,然后重启一次 app**,本机才会切到 `0.0.0.0` 对你可见(UI 保存时已弹提示)。重启后:自注册带 node.token 上来 → 你带 token 拉数据 → 上墙。打包 app 默认走这条(launcher 不设 HOST)。

下载链接发版后即生效(节点 app v1.12.2)。
