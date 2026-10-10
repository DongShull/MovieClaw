# 小艺云 A2A 协议调研（三方 Remote Agent 接入）

> 调研对象：华为开发者联盟 · 小艺开放平台「A2A 协议接入方案」的**云 A2A** 分支
> 入口文档：`云A2A协议技术规范` — https://developer.huawei.com/consumer/cn/doc/doccenter-celia/agent2agent-comments-0000002500412353
> 抓取日期：2026-10-10（文档索引见 §13）
> 抓取方式：文档中心是 SPA，正文由
> `POST https://svc-drcn.developer.huawei.com/community/servlet/consumer/cn/documentPortal/getCenterDocument`
> 返回（body：`centerPrefix=doccenter` / `language=cn` / `level2NodeAlias=celia` / `fileName=<文档名>`）。
> 本文全部内容来自该接口返回的正文；文档没写的地方一律标注「文档未定义」，不做推测。
> 姊妹篇：端 A2A（应用内 Agent，§8）、AgentCard 规范（§7）。
> 对接判断见 §11，与 MovieClaw 现有实现逐条对照；上架门槛见 §12。

## 0. 一句话总览

小艺云 A2A 要求三方自建一个 **单一 Endpoint** 的 Remote Agent：仅 POST，
**Streamable HTTP + JSON-RPC 2.0**，`message/stream` 用 SSE 升级返回流式结果；
**没有长连接**，支持断线重连。核心考点只有五个：异步处理任务、SSE 推进度、
可发起追问澄清、首 Token 立刻流出、**按平台下发的 sessionId 缓存上下文**。
鉴权以 **AK/SK（HMAC-SHA256 签名）** 为首选，会话分「服务端分配 session-id（有状态）」
与「无状态」两种模式。

这套东西在传输层与 **MCP 的 Streamable HTTP** 高度同构（文档自己都写「类似于 MCP 的
mcp-session-id」），在事件层与 **A2A 标准**（status-update / artifact-update）同构。

## 1. 接入形态与平台侧配置（云A2A模式）

在小艺开放平台「新建项目 → 云A2A模式」创建智能体，A2A 基础配置项：

| 配置 | 说明 |
|---|---|
| API URL | 与智能体对话时的访问接口，协议按本文规范 |
| 客户端服务器间会话维持方式 | 「由服务器侧分配 Session；服务器间采用无状态通信，每次携带认证凭据」 |
| 认证信息 | AK/SK（预共享密钥）；OAuth（OAuth2.0，仅 Client 模式）；Header 传参；Query 传参（⚠ 平台自己标注有安全风险，建议别用） |
| 输出设置 | 绑定卡片，最多 20 条；输出参数必须含 `cardData`（Object 类型，必须加子项） |

其余约束（京东案例篇）：开场白 + 预置引导问题**最多 3 条**；卡片模板最多 20 条；
账号授权需在华为开发者联盟「管理中心 → 应用服务 → 账号」申请 **Client ID**（appId）
并在小艺开放平台注册绑定。

## 2. 传输与 RPC 方法

- 传输：统一一个 Endpoint（示例为 `https://xxx/agent/message`），**仅 POST**
- 协议：Streamable HTTP + JSON-RPC 2.0；`message/stream` 可用
  `Content-Type: text/event-stream` 升级为 SSE
- 服务端**不用维护长连接**，需支持断线重连
- 认证：参见 §4；会话：参见 §3

| # | 方法 | 调用方式 | 功能 |
|---|---|---|---|
| 1 | `initialize` | Request/Response 阻塞 | agent-client 发起初始化，服务端返回 sessionId |
| 2 | `notifications/initialized` | Request/Response 阻塞 | client 通知初始化完成（HTTP 200，无响应体） |
| 3 | `message/stream` | **非阻塞，SSE 升级** | 用户对话主入口：接任务、推进度、追问、流式输出正文、终答 |
| 4 | `tasks/cancel` | 阻塞 | 终止当前任务输出 |
| 5 | `clearContext` | 阻塞 | 清理本次多轮对话上下文 |
| 6 | `authorize` | 阻塞 | client 送宿主 APP 代取的华为账号授权信息 |
| 7 | `deauthorize` | 阻塞 | 通知用户取消授权登录 |
| 8 | `push` | 阻塞 | **方向相反**：agent-server 主动推 PUSH 通知给 agent-client（长耗时任务，如音/视频/文件生成） |

## 3. 会话两种模式（有状态 / 无状态）

| | 模式1（规范文档标「推荐」） | 模式2（简化版） |
|---|---|---|
| 状态 | 服务端按 session 维护，**服务器侧分配 `agent-session-id`** | 无状态，服务端不持久绑定会话 |
| 传递 | 客户端每次把 `agent-session-id` 放 **header** 带回（原文：类似于 MCP 的 `mcp-session-id`） | 每次请求 header 携带凭据（AK/SK、APIKey 等） |
| 必须实现 | `initialize` + `notifications/initialized` | 不需要这两个方法 |
| 额外要求 | **同一 AK/SK / APIKey / OAuth 客户端下要能同时分配多个 agent-session-id**，防凭据吊销间隙，建议 ≥5 个同时有效 | 可无限横向扩容 |

> ⚠ **两份文档推荐相反**：`云A2A协议技术规范` 说模式1 是推荐方案，而 `实践案例｜京东Agent`
> 说「无状态通信鉴权模式（推荐）」。平台配置项的名称又是「由服务器侧分配 Session」。
> 实机联调前需要确认走哪条（我们的适配层建议两条都实现，模式2 是模式1 的真子集）。

`initialize` 响应：

```json
{
  "jsonrpc": "2.0",
  "id": "{{请求里的同一序列号}}",
  "result": {
    "version": "1.0",
    "agentSessionId": "{{服务端分配，后续放 header agent-session-id}}",
    "agentSessionTtl": "{{有效期秒数，文档建议 7 天}}"
  },
  "error": { "code": "{{0 表示成功}}", "message": "{{描述}}" }
}
```

## 4. 鉴权（AK/SK 为首选）

### 4.1 AK/SK

| 参数 | 类型 | 必选 | 说明 |
|---|---|---|---|
| `accessKey` | String(64) | M | 开发者在小艺开放平台配置的接入码 |
| `sign` | String(256) | M | `Base64(HMAC-SHA256(secretKey, ts))` |
| `ts` | String(16) | M | 毫秒时间戳（Unix epoch ms） |

- 防重放：服务端校验 `|ts - 当前时间| < 15 分钟`（文档示例值）
- 参考实现（Java 原文换算）：

```python
# sign = base64(hmac_sha256(secret_key, ts))
sign = base64.b64encode(hmac.new(secret.encode(), ts.encode(), hashlib.sha256).digest()).decode()
```

  文档给出的校验用例：`secret = 2e3b71f5def64d95a727314f028bf5aa`、`ts = 1547630863716`
  → `sign = 6lFqepUu79KSxVCsrXyB/aLVFIdutsTLLx1cZjxDE4I=`
- **⚠ 凭据字段有 64 字符上限**：`accessKey` 规格写明 String(64)，实测 `secretKey` 超过
  64 也会被平台**静默截断**——69 字符贴进去只存前 64，截断后的值算签名与后端存的完整
  值永远对不上（实测现象：accessKey 对得上、sign 怎么都验不过）。生成侧须守在 64 以内
  （见 `settings/xiaoyi_a2a.py` 的 `_CREDENTIAL_MAX_LENGTH` 与 `_random_credential`）。
- OAuth：OAuth2.0，当前仅 Client 模式（`认证参考` 篇里另一处写的是 Implicit Grant，属老规范）
- APIKey：可放 header（较安全）或 query（明文泄露/日志抓取风险）
- **Header 名**:A2A 规范只列了参数名（`accessKey`/`sign`/`ts`），没写 header 名。
  `认证参考`（Ability Gallery 老规范）里的请求头域为 `accessKey` / `sign` / `ts`（＋OAuth 时
  `Authorization: Bearer`）；PUSH 侧用的是 `X-Access-Key` / `X-Sign` / `X-Ts` 前缀写法。
  → **实机抓包确认，适配层把 header 名做成可配置**。

### 4.2 出方向（PUSH，见 §5.5）

服务端 → 华为：`X-Access-Key`（小艺平台分配的 appKey）、`X-Ts`（毫秒时间戳）、
`X-Sign = Base64(HMAC-SHA256(secretKey, ts))`；`x-hag-trace-id` 为随机数，用于链路追踪。

## 5. 报文规格

### 5.1 `message/stream` 请求

```json
{
  "jsonrpc": "2.0",
  "id": "{{与 agent-server 通信的全局唯一序列号，字符串}}",
  "method": "message/stream",
  "params": {
    "id": "{{taskId：一次流式交互内保持不变}}",
    "sessionId": "{{Agent Client 侧分配的会话唯一标识，用它存上下文；清理上下文后会更新}}",
    "agentLoginSessionId": "{{用户登录后服务端分配的身份凭证 ID，账号绑定后每次请求携带}}",
    "message": {
      "role": "user",
      "parts": [
        { "kind": "text", "text": "{{用户输入或子任务 Query}}" },
        { "kind": "file", "file": { "name": "{{文件名}}", "mimeType": "{{MIME}}", "uri": "{{URI}}" } },
        { "kind": "data", "data": "{{结构化数据：用户参数、端插件执行结果等}}" }
      ]
    }
  }
}
```

### 5.2 响应事件一：`status-update`（进度 / 追问 / 终态）

```json
{
  "jsonrpc": "2.0",
  "id": "{{原样返回请求 id}}",
  "result": {
    "taskId": "{{原样返回请求 id 字段}}",
    "kind": "status-update",
    "final": "{{true 表示本任务 SSE 流结束并断开端云任务通道；任务结束必须置 true}}",
    "status": {
      "message": {
        "role": "agent",
        "parts": [{ "kind": "text", "text": "{{状态描述，如思考中、分析中，展示在状态栏}}" }]
      },
      "state": "{{submitted | working | input-required | completed | canceled | failed | unknown}}"
    }
  },
  "error": { "code": "{{0 成功，99911114 内容不合规，99911113 流控}}", "message": "{{描述}}" }
}
```

- `state` 语义：有 Artifact 输出时不必用 `completed` 收尾，否则可用它返回
- **追问澄清 = `input-required`**，没有单独的「question」方法

### 5.3 响应事件二：`artifact-update`（流式正文）

```json
{
  "jsonrpc": "2.0",
  "id": "{{原样返回请求 id}}",
  "result": {
    "taskId": "{{原样返回}}",
    "kind": "artifact-update",
    "append": "{{布尔，内容是否追加到前序片段，默认 false}}",
    "lastChunk": "{{布尔，是否本轮流式输出的最后一片，默认 true}}",
    "final": "{{布尔，同 5.2}}",
    "artifact": {
      "artifactId": "{{本条 Artifact 唯一 ID}}",
      "parts": [
        { "kind": "reasoningText", "reasoningText": "{{深度思考流式输出，支持 markdown，增量时 append=true}}" },
        { "kind": "text", "text": "{{正文流式输出，支持 markdown，增量时 append=true}}" },
        { "kind": "data", "data": "{{结构化数据：卡片、端指令、推荐问题、循证引用等，可扩展}}" }
      ]
    }
  },
  "error": { "code": "{{…}}", "message": "{{…}}" }
}
```

- 一次会话请求（`final=true` 结束前）允许**多次**流式输出，每次以 `lastChunk=true` 收尾
- 每轮流式输出可以没有「Start」，但必须以 `lastChunk=true` 结束
- **`final=true` 会断开端云任务通道**：置位后云侧不能再推消息，所以置位前必须把话说尽

### 5.4 `tasks/cancel` / `clearContext`

```json
{ "jsonrpc": "2.0", "id": "{{序列号}}", "sessionId": "{{会话 ID}}", "method": "tasks/cancel" }
→ { "result": { "id": "{{原样}}", "status": { "state": "{{canceled|failed|unknown}}" } }, "error": {...} }

{ "jsonrpc": "2.0", "id": "{{序列号}}", "sessionId": "{{会话 ID}}", "method": "clearContext" }
→ { "result": { "status": { "state": "{{cleared|failed|unknown}}" } }, "error": {...} }
```

> 注：两篇示例把 `sessionId` 放在顶层（不在 `params` 里），与 `message/stream` 不一致 → 以实测为准。

### 5.5 `push`（服务端 → 华为，反向调用）

```
POST https://hag.cloud.huawei.com/open-ability-agent/v1/agent-webhook
Content-Type: application/json
Accept: application/json
x-hag-trace-id: {{随机数}}
X-Access-Key: {{小艺开放平台触发器事件 WebHook 分配的 appKey}}
X-Sign: {{Base64(HMAC-SHA256(secretKey, ts))}}
X-Ts: {{毫秒时间戳}}
```

```json
{
  "jsonrpc": "2.0",
  "id": "{{序列号}}",
  "result": {
    "id": "{{原样返回请求 id}}",
    "apiId": "{{创建 api 时生成的 API ID}}",
    "pushId": "{{平台系统变量 push_id}}",
    "agentLoginSessionId": "{{用户授权身份 ID}}",
    "pushText": "{{Push 展示内容}}",
    "kind": "task",
    "artifacts": [
      { "artifactId": "{{唯一 ID，用于 CP 请求去重}}",
        "parts": [ { "kind": "text", "text": "{{…}}" }, { "kind": "data", "data": "{{…}}" } ] }
    ],
    "status": { "state": "{{completed|canceled|failed}}" }
  },
  "error": { "code": "{{整形错误码}}", "message": "{{描述}}" }
}
```

响应（华为 → 我们）：`{ "id": "{{请求 id}}", "resultId": "{{请求 result.id}}", "result": { "code": "...", "message": "..." } }`

### 5.6 `authorize` / `deauthorize`（华为账号授权）

```
authorize 请求：params.message.parts[0] = { "kind": "data", "data": { "authCode": "{{宿主 APP 代取的华为账号授权码}}" } }
authorize 响应：{ "result": { "version": "1.0", "agentLoginSessionId": "{{服务端分配的用户登录凭证 ID}}" } }

deauthorize 请求：params.message.parts[0].data = { "agentLoginSessionId": "...", "cpUserId": "{{支付场景的 CP 侧用户标识}}" }
deauthorize 响应：{ "result": { "version": "1.0" } }
```

流程要点（`云A2A协议技术规范` + 京东案例）：

1. 开发者在华为开发者联盟账号服务申请 **appId（Client ID）**，并在小艺开放平台注册保存
2. 小艺 APP 加载三方智能体页面时从小艺开放平台取到该 appId
3. 用户在智能体内点「账号授权」：已授权 → 静默走 `authorize`，服务端返回**新的**
   `agentLoginSessionId`；未授权 → 弹框取授权码，服务端用授权码换**手机号**，再返回
   `agentLoginSessionId`
4. 小艺 APP 持久化 `agentLoginSessionId`，后续每轮对话自动携带
5. 超期/失效/不存在时，小艺 APP 自动重新发起授权
6. 需要提示登录时，正文里的超链接写
   `superlink://vassistant?hwIdAuth=phone&appId={{APP ID}}&agentId={{agentId}}`

## 6. 数据结构（message.parts 里的 data）

### 6.1 请求侧 data（`请求data数据结构定义`）

| 字段 | 类型 | 必选 | 说明 |
|---|---|---|---|
| `authCode` | string | 否 | 仅 `authorize` 方法必选 |
| `agentLoginSessionId` | string | 否 | 仅 `deauthorize` 方法必选 |
| `events` | array[EventObject] | 否 | 客户端上报事件时必选（如端侧执行结果回传） |
| `userInputInfo` | JSONObject | 否 | 底部快捷指令点击产生的输入 |
| `variables` | JSONObject | 否 | 小艺开放平台开关打开后才下发：`clientVariables` / `systemVariables`（如 `app_ver`、`foreground_apps`）/ `memoryVariables` |

`EventObject`：`{ header: { namespace, name }, payload: {...} }`（按业务场景填充）。

底部快捷指令（`userInputInfo.statusInfo[]`）：
`{ "isSelected": true, "statusKey": "{{平台定义的 Key}}", "statusValue": "{{平台定义的 Value}}" }`
（需先在小艺开放平台配置对应快捷指令）。

### 6.2 响应侧 data（`响应data数据结构定义`）

顶层：`kind: "data"` 固定 + `data`（object）+ 可选 `commands` / `cardsInfo` / `chipsInfo` / `reference`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `commands` | array[CommandObject] | 需要调用端侧工具时下发的指令（见 §7） |
| `cardsInfo` | array[CardDataObject] | 卡片模板填充数据 |
| `chipsInfo` | ChipDataObject | 接续追问气泡（推荐问题） |
| `reference` | ReferenceDataObject | 循证引用（来源卡片） |

`CardDataObject`：`cardName`（必须与小艺开放平台输出配置里的卡片名一致）、
`cardData`（多条记录放 `items.[*]`）、`displayType`（`EmbedMarkdown` 嵌入 MD /
`DisplayFaCard` 独立出卡；不传默认独立显示，卡片在文字下方）。

`ChipDataObject`：`displayChips` → `chipsList[]`，每项 `content`
（**≤64 字符**，`superlink://vassistant?text={{推荐问题}}&startmode=recognize`）、
`domain`（如 `AIGC`）、`icon`（气泡图标 URL）。

`ReferenceDataObject`：`items[]`（打点参数）+ `card`（固定模板
`leftPictureRightText`：`title` / `subTitle` / `link.webLink.{startMode,url}`
/ `imageInfo.small.url`）。`startMode`：0 小艺内部拉起、1 浏览器（默认 0）。

## 7. 端侧指令（下发给小艺 APP 或我们自己的 App）

1. **意图框架 Action 指令**（`header = { namespace: "Common", name: "Action" }`）
   `payload.executeParam`：`executeMode`（background/foreground）、`intentName`、
   `intentParam`、`bundleName`、`actionResponse`（执行结果是否回报云侧）、
   `actionResponseConfig`（`type`: WHITE/BLACK + `resultPath[]`）。
   ⚠ **约束**：必须先在小艺开放平台注册意图框架插件，且 `bundleName` / `intentName`
   与指令一致，否则**云侧直接拦截**。
   回传走请求侧 `data.events`，`header = { namespace: "Common", name: "UploadExeResult" }`，
   `payload = { toolName, resultCode, responseText, responseDataList[] }`。
2. **Deeplink 指令**（`header = { namespace: "Command", name: "Deeplink" }`）：
   `payload = { url, appName, packageName, appType（DeepLink/OpenHarmony） }`。**Deeplink 不需要白名单**。
3. **取经纬度等系统能力**：走 Action 指令（如 `intentName = GetCurrentLocation`，
   `bundleName = com.huawei.hmos.aidispatchservice`，需先在智能体里添加对应插件）。
   ⚠ 下发定位指令时 **`final` 不能填 true**，等小艺回传位置（`data.events`，
   WGS84 坐标系）后再继续。

## 8. 端 A2A（应用内 Agent，另一条路线）

同一份体系下还有「端 A2A」：小艺 ↔ **应用内 Agent**（不是云侧服务），文档版本 V0.6。
四个核心概念，与云 A2A 略有不同：

- **Context（会话）**：由 Agent 分配 `contextId`；首轮请求小艺不带，Agent 首帧返回，
  之后每次携带。Agent 可主动失效（返回 **99911222**，小艺不带 contextId 重放）
- **Task（任务）**：Agent 分配 `taskId`；状态 `TASK_STATE_SUBMITTED / WORKING /
  INPUT_REQUIRED / COMPLETED / CANCELLED / FAILED`（与云 A2A 的 state 取值同义）
- **Artifact / Part**：同云 A2A 的组合呈现单元与分片
- 场景：LUI 对话、长时任务伴随、界面控制伴随、chips 话题推荐、原生操控、动态开场白

配套还有「AgentCard 定义规范」（`name` / `description` / `agentId` / `version` /
`iconUrl` / `capabilities{streaming,pushNotifications,stateTransitionHistory}` /
`defaultInputModes` / `defaultOutputModes` / `skills[]` / `provider` /
`extension` / `appInfo` / `supportedInterfaces[{url, protocolBinding:"JSONRPC", protocolVersion}]`）
← 从字段看是**平台侧配置项**，不需要我们托管对外端点。

## 9. 错误码

| 错误码 | 分类 | 方向 | 说明 |
|---|---|---|---|
| -32700 | JSON-RPC 2.0 | 双向 | JSON 解析失败 |
| -32600 | JSON-RPC 2.0 | 双向 | 非法请求对象 |
| -32602 | JSON-RPC 2.0 | 双向 | 参数非法（类型错/缺必填） |
| -32603 | JSON-RPC 2.0 | 双向 | 服务端内部错误 |
| -32001 | JSON-RPC 2.0 | Agent → 小艺 | TaskNotFoundError（task 不存在/无权） |
| -32002 | JSON-RPC 2.0 | Agent → 小艺 | TaskNotCancelableError（已完成/当前阶段不支持） |
| -32004 | JSON-RPC 2.0 | Agent → 小艺 | UnsupportedOperationError（如向已终止 task 发消息） |
| 99911113 | 鸿蒙扩展 | 双向 | 流控失败 |
| 99911114 | 鸿蒙扩展 | 双向 | 风控失败（内容不合规） |
| 99911200 | 鸿蒙扩展 | Agent → 小艺 | task 已失效 |
| 99911222 | 鸿蒙扩展 | Agent → 小艺 | contextId 已失效，请重新请求 |

`认证参考` 篇里另有一套老规范（Ability Gallery 准入）的业务码：
HTTP 400 + errorCode `1` 无效参数 / `2` 签名错误 / `3` 参数过多 / `4` 不支持的签名方式 /
`5` 时间戳无效 / `6` 不支持的接口 / `7` AbilityId 不合法 …（可作为排障时对照）。

## 10. 准入与硬指标（来自 `认证参考`，Ability Gallery 老规范，作参考）

- 协议：HTTPS；**证书必须合法 CA 颁发，不允许自签名**
- 方法：POST；请求响应 JSON；响应应开启 **gz 压缩**
- 支持 `Connection: keep-alive`（华为侧用长连接调用）
- **单次响应总大小 ≤ 24KB**；Speech 响应 ≤ 8000 字节；图片/音频等资源链接必须 HTTPS
- 性能：TPS > 2000、P99 时延 < 150ms、成功率 > 99.99%、全年中断 < 20 分钟、支持水平扩容
  （这组数字是「快服务」准入口径，个人/小型智能体接入大概率不逐条强校验，**但 24KB
  与 HTTPS 证书两条是真会咬人的**，需要向平台确认）

## 11. 与 MovieClaw 的对接判断

### 11.1 同源的部分（可直接复用，不用从零搭）

| A2A 要求 | MovieClaw 里的对应物 |
|---|---|
| JSON-RPC + Streamable HTTP + SSE 升级 | 已经在跑官方 MCP SDK 的 `StreamableHTTPSessionManager`：`src/movieclaw_mcp/app.py`（`pyproject.toml`: `mcp>=2.1.0`） |
| `agent-session-id`（原文「类似 MCP 的 mcp-session-id」） | 同款语义，MCP 侧已有 session 管理经验 |
| 流式事件（status-update / artifact-update） | `AgentEvent`（`src/movieclaw_agent/events.py:31-43`）：`text_delta` / `thinking_delta` / `tool_call` / `tool_result` / `agent_done` / `agent_error` / `agent_cancelled`，与两类事件天然一一映射 |
| 追问澄清 | Agent 多步 loop 本来就会发问句，落成 `state=input-required` 即可，无需新机制 |
| 按 sessionId 缓存上下文 | `agent_session` 索引 + JSONL 转录（`src/movieclaw_api/services/channel_agent.py:6-7`、`agent_sessions.py`） |
| AK/SK = `Base64(HMAC-SHA256(secret, ts))` | 出站 webhook 投递已在做同算法签名：`src/movieclaw_api/services/webhook/formatter.py:45`；常量时间比较的写法见 `src/movieclaw_api/api/deps.py:41` |
| 出方向 PUSH（服务端反向调华为） | 形状同现有 webhook 投递器（签名 + 重试 + 投递记录） |
| AK/SK 落库加密 | `SecretBox`（`src/movieclaw_db/crypto.py:22`，Fernet/AES-128-CBC+HMAC） |
| 同一 sessionId 的并发串行 | `ChannelDispatcher` 的「per session_key 一条队列一个 worker」语义（`src/movieclaw_channel/dispatcher.py:190-212`） |

事件映射建议（AgentEvent → A2A）：

| AgentEvent | A2A 输出 |
|---|---|
| 收到请求 | `status-update` `state=working`（等价于通道侧「思考中💭」） |
| `thinking_delta` | `artifact-update`，`parts[{kind:"reasoningText"}]`，`append=true` |
| `text_delta` | `artifact-update`，`parts[{kind:"text"}]`，`append=true` |
| `tool_call` / `tool_result` | `status-update`（过程状态文案）或丢弃 |
| Agent 发问句 | `status-update` `state=input-required` |
| `agent_done` | 最后一次 `artifact-update`（`lastChunk=true`）+ `final=true` |
| `agent_error` | `state=failed` / `error.code` |
| `agent_cancelled` | `state=canceled` |

### 11.2 建议的实现形态

1. **独立适配模块**（如新增 `src/movieclaw_a2a/`）+ 一条 ASGI 路由
   `POST /api/v1/a2a/agent/message`，内部自己做 JSON-RPC 分发与 SSE 编码；
   `message/stream` 返回 `StreamingResponse(media_type="text/event-stream")`。
2. **不要**塞进 IM 通道的回调通路（`PLUGIN_CALLBACKS` + `ChannelDriver.webhook`）：
   那条路是「平台回调地址里带密钥 + 单一回调端点」，而 A2A 是固定 Endpoint +
   header 签名 + 8 个 RPC 方法 + SSE 会话管理，硬塞会把 RPC 端点掰成通道回调。
3. **认证**：AK/SK 三 header 校验 + `|Δts| < 15min` + `hmac.compare_digest`；
   header 名可配置（`accessKey/sign/ts` vs `X-Access-Key/X-Sign/X-Ts` 未定）；
   AK/SK 用 `SecretBox` 落库。
4. **会话映射表**（必须新加）：`platform_session_id → agent_session_id`，**每个
   sessionId 一条会话**（⚠ 现在的通道实现是「每账号一条会话」，A2A 多用户下会串号），
   模式1 还要允许同一凭证下多条 session 并存（≥5）。
5. **工具集**：先跟 IM 通道同档——只挂 mclaw 产品工具、不开 bash/read/write
   （`src/movieclaw_api/services/channel_agent.py:69-79`）。
6. **24KB 约束**：正文靠 `append` / `lastChunk` 分片，不能一条塞完。
7. **复用而非重写**：`AgentRunner` / `StepReplyPusher`（`src/movieclaw_channel/pusher.py`）
   / `AgentSessionRecorder` / 会话历史重建全部沿用；A2A 只提供「编解码 + 会话映射」。
8. **联调替身**：写一个模拟器（curl / 脚本）伪造 `initialize` + `message/stream` +
   `tasks/cancel` + 各类错误码，本地端到端跑通 `AgentRunner`；平台真联调时只需改
   header 名与字段名。

### 11.3 待确认（只有 4 项，我猜不出来）

1. ~~**走模式1 还是模式2**~~ **已实测：两条都要支持**。华为平台走模式1——`initialize`
   带 AK/SK 签名，之后只带 `agent-session-id` 头（值就是 initialize 返回的 `agentSessionId`）。
   端点两种都认：先验凭据（模式2），失败再验 `agent-session-id`（模式1，自签令牌 + 7 天 TTL）。
2. ~~**AK/SK 的 header 名**~~ **已实测：裸名 `accessKey/sign/ts`**（平台回带的正是这三个），
   `X-` 前缀写法未见；适配层两种命名都认，无需配置。会话维持方式见 `session_mode`
   （`assigned` / `stateless`），只作界面指引，端点两种都接受。
3. `tasks/cancel` / `clearContext` 的 `sessionId` 是在顶层还是 `params` 内（两篇示例不一致）
4. 是否真的被强校验：TPS/P99/可用性那组指标、24KB 响应上限、AgentCard 是否需自托管

### 11.4 分期

- 一期（最小闭环）：`initialize` + `notifications/initialized` + `message/stream` +
  `tasks/cancel` + `clearContext` + AK/SK 验签 + 会话映射表 + SSE 事件映射
- 二期：`authorize` / `deauthorize`（授权码换手机号；需 appId，合规最重但商业价值最高）、
  `cardsInfo` 卡片（需在小艺平台配置卡片模板）、`chipsInfo` 推荐问题
- 三期：`push` 出站长任务通知、端侧 Action/Deeplink 指令、`variables` 设备上下文

## 12. 上架合规与资质（决定能不能真的上线）

⚠ 这一节是**非技术门槛**，比协议本身更能决定项目成败。来源：`智能体资质`、
`内容合规信息中人工智能生成合成内容标识和大模型备案FAQ`、`不收录类型`、
`Agent上架审核规范`（目录页）。

### 12.1 大模型备案（最硬的一道门）

`大模型备案信息怎么填`：

- 接入的是**小艺开放平台提供的三方大模型** → 填「否」
- 接入的是**非小艺开放平台提供的三方大模型** → 填「是」，并给出
  **生成式人工智能服务上线备案号 + 算法备案号**

MovieClaw 的 LLM 是用户自己在设置里配的（DeepSeek / OpenAI / 本地模型…），
几乎不可能落在前一种 → **上架申报时要提供备案号**。备案主体是提供生成式 AI 服务的
公司/组织，个人或小主体通常拿不到。三条出路：

1. 主体去办备案（有周期、有主体资质要求）；
2. 智能体侧改走小艺平台提供的大模型（若平台允许且够用）；
3. 不做公开发布，只做「真机测试」自用/内测（**测试态是否豁免申报需向平台确认**）。

### 12.2 AI 生成内容标识（必做，开发量小）

按《人工智能生成合成内容标识办法》：

- **显式标识**：输出正文与交互界面里要有用户明显可感知的标识（文字/声音/图形）
- **隐式标识**：要在生成内容的文件数据里写入技术标识
- 自查：AI 标识服务检测平台（上传文件 → 开始检测 → 看结果）

`不收录类型`里明确把「恶意删除、篡改、伪造、隐匿法律规定的生成合成内容标识」
列为不收录。

### 12.3 业务资质（与 MovieClaw 相关的那一行）

`智能体资质`里最相关的是**「影视频、电台、微短剧」**：

> 《信息网络传播视听节目许可证》或「全国网络视听平台信息管理系统」备案；
> 涉及技术服务/合作的分支需合作方资质与双方合作协议；
> **（若仅涉及 AIGC 问答类则无需提供）**

→ 这条豁免是我们要落的边界。定位必须停在「媒体库问答 / 订阅与下载管理助手」，
**不提供视听节目服务**：播放动作由 **DeepLink 跳回用户自己的 MovieClaw 客户端/网页**
完成，智能体本身不下发、不播放视频。同时避免出现「影视下载 / 磁力 / 种子」等
容易被判为侵权或不收录的表述（`不收录类型`对侵权与违法内容的口径很严）。

另外两条：从事经营性互联网信息服务的需《增值电信业务经营许可证》；
拟人化智能体暂不支持上架（与本项目无关，记录备查）。

### 12.4 技术侧硬指标（复述 §10 里真会咬人的几条）

- HTTPS + **合法 CA 证书（不允许自签名）** → 必须有公网域名与正式证书，
  与 MovieClaw 现有 `external_url` 回调地址是同一套要求
- 单次响应 ≤ 24KB → 正文靠 `append` / `lastChunk` 分片
- 平台侧还要求填写「人工智能生成合成内容标识」「权限用途描述」等信息

## 13. 抓取的文档索引

| 文档 | URL |
|---|---|
| A2A协议接入方案 | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/agent2agent-0000002498656261 |
| 云A2A协议技术规范 | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/agent2agent-comments-0000002500412353 |
| 云A2A模式（平台配置） | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/cloud-a2a-0000002640266052 |
| 实践案例｜云A2A智能体协同——京东Agent | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/cloud-a2a-jingdong-0000002640047776 |
| 云A2A协议消息指令定义（仅目录页） | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/agent2agent-define-0000002467293060 |
| 初始化/初始化完成 | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/initialize-initialized-0000002537681161 |
| 发起会话（message/stream） | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/message-stream-0000002505761434 |
| 终止会话（tasks/cancel） | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/tasks-cancel-0000002537561193 |
| 清理上下文（clearContext） | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/clear-context-0000002537681163 |
| 授权登录/解授权 | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/authorize-deauthorize-0000002505921274 |
| PUSH通知 | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/pushmessage-0000002505761436 |
| 消息参数说明（仅目录页） | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/agent2agent-parameter-desc-0000002467479802 |
| 请求data数据结构定义 | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/query-data-0000002537691281 |
| 响应data数据结构定义 | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/response-data-0000002505931382 |
| 端侧插件工具指令说明 | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/agent2agent-action-0000002467539682 |
| 底部快捷指令说明 | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/agent2agent-command-0000002467900460 |
| 设备上下文参数指令说明 | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/agent2agent-context-0000002501019701 |
| 错误码总览 | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/agent2agent-errorcode-0000002660465493 |
| 认证参考（实现接口定义，Ability Gallery 老规范） | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/interface-0000001195110098 |
| 端A2A协议技术规范 | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/agent2agent-device-0000002624952279 |
| AgentCard定义规范 | https://developer.huawei.com/consumer/cn/doc/doccenter-celia/agentcard-0000002678424557 |

未抓取（与本项目无关，需要时再补）：端A2A 的应用内 Agent 开发接入、端A2A 消息指令定义
（对话交互 / 长时任务伴随 / 界面操控伴随 / 原生操控 / chips 推荐 / 动态开场白 / 异常处理
与任务取消）、扩展数据结构定义、数字商品支付服务三篇、Skill 体系若干。

## 14. 实现对照与差异清单（2026-10-10 二次精读核对）

> 联调排障过程中二次精读了官方文档正文（非目录页），逐条与 MovieClaw 落地代码
> 核对。结论：**协议层实现与官方文档一致，无阻塞性错误**；已知差异见 §14.3。

### 14.1 本次二次精读的文档

| 文档 | 内容 |
|---|---|
| 云A2A协议技术规范 | 全文：传输原语、两种模式、8 个 RPC 方法、时序 |
| 初始化/初始化完成 | `initialize` 请求/响应报文、`notifications/initialized`（HTTP 200 无体） |
| 发起会话（message/stream） | 请求体、`status-update` / `artifact-update` 两类 SSE 事件报文 |
| 请求data数据结构定义 | `message.parts[].data`（events / variables / userInputInfo 等） |
| 响应data数据结构定义 | `commands` / `cardsInfo` / `chipsInfo` / `reference` 四类结构化返回 |

### 14.2 逐项对照表

| 官方要求 | 落地位置 | 结论 |
|---|---|---|
| initialize 返回 `version` + `agentSessionId` + `agentSessionTtl`（秒，建议 7 天） | `routes/xiaoyi_a2a.py` `_handle_initialize` | ✅ 一致（实测返回 `version=1.0` / `agentSessionId` / `agentSessionTtl=604800`） |
| message/stream 请求 `params.id`（taskId）/ `params.sessionId` / `params.message.parts[]` | `_extract_user_text` + `_handle_message_stream` | ✅ 一致（`kind=="text"` 拼接为用户输入；file/data 暂忽略，见 §14.3-2） |
| `status-update`：`{taskId, kind, final, status:{message:{role:"agent",parts:[{kind:"text"}]}, state}}` | `_status_event` | ✅ 一致 |
| `artifact-update`：`{taskId, kind, append, lastChunk, final, artifact:{artifactId, parts[]}}`，**`append`/`lastChunk` 是 result 顶层字段**（与 taskId/kind/final 平级） | `_artifact_event` | ✅ 一致（append/lastChunk 放顶层，未误塞进 artifact） |
| `final=true` 断开端云任务通道，任务结束必须置 true | `_handle_message_stream.event_source` 的 `sent_final` 守卫 | ✅ 一致（终态后绝不重复发 final） |
| 8 个 RPC 方法分发 | `agent_message` | ✅ 一致（authorize/deauthorize/push 二期占位，见 §14.3-3） |
| 模式1：initialize + `agent-session-id` header + 同凭证多 session（≥5） | `xiaoyi_a2a_session` 映射表 + `ensure_agent_session`（**每 platform sessionId 一条**，非每账号） | ✅ 支持；模式2 无状态是模式1 真子集，验签天然不依赖 session |
| AK/SK：`sign=Base64(HMAC-SHA256(secret, ts))`、`|Δts|<15min`、`hmac.compare_digest` | `verify_signature` | ✅ 一致（官方向量实测匹配） |
| 错误码：JSON-RPC 标准码 + 99911113（流控）/ 99911114（风控） | 模块常量 + `_error_response` | ✅ 一致 |

### 14.3 差异清单（已知、不阻塞、待二期）

1. **reasoningText 流的 `lastChunk` 收尾**：官方要求「每轮流式输出以 `lastChunk=true` 结束」。
   当前 `thinking_delta` 只发 `append=true` 的 reasoningText，切换到 `text` 流之前没有单独发一帧
   `lastChunk=true` 收尾。整体 `final=true` 收尾正确，但思考态/正文态在端侧的切分可能不够干净。
   → 联调后如需，在 thinking 流结束时补一帧 `reasoningText` + `lastChunk=true`。
2. **message.parts 的 `file` / `data` kind 未解析**：只取 `text`。文件/图片、端侧事件
   （events/variables/userInputInfo）二期做文件与卡片时再补。
3. **`authorize` / `deauthorize` / `push` 未实现**：当前返回 JSON-RPC `-32601` 占位。
   文档定义已收录 §5.5 / §5.6，二期实现（授权码换手机号 + PUSH 出站通知）。

### 14.4 联调结论（2026-10-10）

协议层实现与官方文档对齐、无错误；**实际卡点不在协议，在网络可达性**——华为云出站只放行
80/443，反代非标端口 99 无法被华为访问（证据：反代日志无任何华为来源请求；手机流量可访问
`:99/api/v1/health` 证明本侧对外是通的）。解法：入口挪到 443（国内云轻量中转 / Cloudflare）。
