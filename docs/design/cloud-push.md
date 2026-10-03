# MovieClaw Cloud 与 App 推送（实例端）

> 状态：实施中（2026-10，分支 `feat/cloud-push`）。
>
> 依据：云端协议 `docs/design/cloud-protocol.md`、推送中继协议
> （公开仓库 movieclaw/MovieClaw-Push 的 `docs/protocol.md`）、推送密文格式
> `docs/design/push-payload.md`。这三份是实例、云端、App 三方的共同依据；本文只写
> 实例和 App 这一侧怎么落地，以及它们之间的接口。
>
> **本文的第 7 节即实例与网页、App 之间的接口契约**，字段以这里为准。

## 0. 关键决定

1. **云的连接和 App 推送分成两个入口。** 连接是一次性的、管理员的、整台服务器的
   动作，以后云端加能力（远程访问、控制……）都挂在它下面；App 推送是通道和内容，
   自建中继的用户可以完全不碰云。
2. **叫「连接」，不叫「登录」，也不叫「绑定设备」。**
   - 云端的数据模型以账号为主：实例挂在账号下（`instances.owner_id`），以后加好友、
     看好友在看什么，也是账号对账号——和 Plex 一样。
   - 但实例这边做的事不是「服务器登录了你的账号」：实例拿到的是它自己的凭证，只能做
     授予它的权限范围（本期只有 `push`），碰不到账号本身。说「登录」会让人以为服务器
     能以你的身份做任何事；以后每个家庭成员用自己的 MovieClaw 账号「登录」做社交功能
     时，两个「登录」还会撞在一起。
   - 「设备」在产品里已经指手机、浏览器、命令行（账号 → 设备），不能再拿来指服务器。
   - 所以：页面叫「MovieClaw Cloud」，动作叫「连接到 MovieClaw 账号」，状态写「已连接到
     y•••@gmail.com 的 MovieClaw 账号」。以后的社交身份是另一层：每个人在
     「账号」下各自登录自己的 MovieClaw 账号，与服务器连接互不依赖。
3. **未连接时，实例对云端不发任何请求。**
4. **推送只有一种发送器：推送中继协议客户端。** 不直连 APNs。自己打包 App 的用户
   自己跑一个 `movieclaw-push`。
5. **每个通道一个启用开关，按设备的 Bundle ID 自动选通道。** 连接云之后官方通道默认
   启用；未连接时官方通道显示为「未激活」。同一个 Bundle ID 有多个可用通道时按列表
   顺序，前一个连不上自动换下一个。
6. **推送按人发，每个人自己管收什么。** 规则见第 5 节。
7. **App 在登录服务器的第一时间请求通知权限**，不等第一次订阅：推送不止订阅，
   还有账号安全、管理员告警。
8. **能自己恢复的不打扰，需要人处理的告诉谁去哪做什么。** 见第 10 节。

## 1. 信息架构

| 位置 | 谁能看 | 内容 |
| --- | --- | --- |
| 系统 → MovieClaw Cloud（`/settings/cloud`） | 管理员 | 连接 / 断开、连接到哪个账号、权限和能力、最近同步、服务通知、上报内容与统计开关 |
| 通知与集成 → App 推送（`/settings/app-push`） | 管理员 | 推送通道（官方 + 自建中继，各一个启用开关）、添加自建中继 |
| 通知与集成 → IM 推送（`/settings/im-push`） | 管理员 | 原「消息推送」，只改名 |
| 账号 → 设备（`/settings/devices`，原有） | 所有人（管理员可看全部成员） | 设备列表；每台 App 设备收不到通知时写一行原因 |
| 账号 → 通知（`/settings/notifications`） | 所有人 | 自己收哪些通知、发测试通知；自己有设备收不到时提示一句 |

App 里：

- 「我的 → 服务器设置」里有同样的「通知」（所有人）和「MovieClaw Cloud」（管理员）。
  「App 推送」的中继管理只在网页做（表单多、低频），App 里不放。
- 「通知」页上，服务器还没有可用通道时：管理员看到「开启手机通知」，一步走完连接；
  成员看到「管理员还没有开启手机通知」。

## 2. 云端客户端

代码：`src/movieclaw_api/services/cloud/`。

### 2.1 地址

- 云端地址：环境变量 `MOVIECLAW_CLOUD_URL`，默认 `https://api.movieclaw.io`。
- 发现文档 `GET <云端地址>/.well-known/movieclaw-cloud`：连接时拉一次，之后每次
  续签顺带刷新；拉取失败用缓存。之后的认领、续签、解绑都用文档里的 `api`。
- 官方中继地址来自发现文档的 `push_endpoints`（按 `priority` 依次尝试）。从没拉到过
  发现文档、且用的是默认云端地址时，用内置的 `https://push.movieclaw.io`。
- 所有请求走出网代理的服务标签 `movieclaw_cloud`（云端）和 `movieclaw_push`
  （官方中继）。自建中继在局域网地址时直连，其他地址也走 `movieclaw_push`。

### 2.2 连接（RFC 8628 设备授权）

1. 管理员点「连接」（可改服务器名称，默认取 Jellyfin 兼容层的服务器名）。
2. 实例拉发现文档 → `POST /v1/instance/device-code` → 拿到配对码，界面给出
   `verification_uri_complete` 按钮和二维码。
3. 实例在后台按 `interval` 轮询 `POST /v1/instance/token`：`slow_down` 间隔加 5 秒；
   `access_denied`、`expired_token`、`invalid_grant` 停止并给出原因；网络错误继续轮询
   直到配对码过期。
4. 拿到凭证后立刻持久化，状态变为「已连接」，并马上续签一次（把上报发上去）。

`device_code` 只在内存里，不落库、不写日志。服务重启后配对作废，重新点连接即可。
同一时间只有一个配对；重新获取配对码会作废上一个。

### 2.3 续签与上报

- 按 `renew_interval`（默认 1 小时）续签，间隔加 ±10% 随机抖动；启动后 5–30 秒内
  先续签一次（离线期间可能已在官网解绑）。
- 失败按指数退避重试：30 秒起、每次翻倍、最长 1 小时，同样加 ±10% 抖动。旧令牌在
  过期前照常使用。
- `access_token` 和 `instance_secret` 一起加密落库：重启时云端恰好连不上，令牌也还
  能用到过期为止。
- 上报：`instance_version`、`runtime_version`（`python x.y.z`）、`os`、`arch` 必报；
  统计开关打开时（默认）再报 `devices`（有推送登记的设备，按平台和 App 版本汇总）和
  `relay`（官方中继能否连通、最近一次成功推送的时间）。最近一次上报的原文保存下来，
  原样显示在设置页。

### 2.4 结果处理

| 情况 | 处理 |
| --- | --- |
| 200 | 更新令牌、权限、限额、能力、账号标识、服务通知；消退所有 `cloud:` 告警 |
| 401 `UNAUTHORIZED` / `INSTANCE_REVOKED` | 删除本地凭证，回到未连接；记下原因显示在云页面；管理员待处理事项里加一条「这台服务器已和 MovieClaw Cloud 断开」 |
| 403 `VERSION_UNSUPPORTED` | 保留凭证，照常续签；官方通道停用；云页面和待处理事项显示云端给的说明 |
| 网络错误、5xx、400 | 退避重试；令牌剩不到 6 小时还没续上，待处理事项里加一条「连不上 MovieClaw Cloud」 |

### 2.5 断开

`POST /v1/instance/unbind`，成功或云端说凭证已失效都删除本地凭证。云端连不上时先
问管理员「仍要断开吗」：确认后只删本地凭证，提示去官网把这台服务器也删掉。

## 3. 推送通道与路由

代码：`src/movieclaw_api/services/push/`。

- 通道 = 官方中继（来自云端）+ 管理员添加的自建中继（地址 + 令牌）。每个通道一个
  启用开关；官方通道连接云之后默认启用。
- 每个通道的 `GET /v1/info` 定期刷新（6 小时）并缓存进数据库，重启时不用等它就能
  路由。`topics` 决定它能推哪些 Bundle ID。官方通道没拉到过 `/v1/info` 时按
  `io.movieclaw.app` 处理。
- 官方通道可用 = 启用 + 已连接 + 有 `push` 权限 + 令牌未过期 + 版本受支持。
- 选通道：按设备的 Bundle ID，在可用通道里按「官方在前、自建按列表顺序」取第一个
  `topics` 包含它的；整批请求失败（连不上、5xx、401）时换下一个能推它的通道，官方
  通道先在自己的多个地址之间切换。全部失败的批次 30 秒、2 分钟后各重试一次。
- `rate_limited`：在 `retry_after` 之前不再往这个通道发；额度剩不到 5% 时只发 `alert`。
- 中继的 `/v1/info` 声明 `auth.mode: none` 时不带凭证。
- 实例凭证只发给官方通道或管理员加过的中继。

## 4. 设备登记

- App（`kind` 为 `ios`、`tvos`、`android` 的登录设备）用自己的设备令牌调
  `PUT /push/me/registration`，把 APNs 令牌、Bundle ID、环境、支持的推送类型、
  `key_id`、密钥和系统通知权限状态交给实例。
- 存在 `login_device` 行上（密钥加密存储），退出登录、注销设备时随行删除。
- 中继返回 `unregistered`：清掉这台设备的推送登记；`bad_token`：只标记，设置页显示。
- 系统通知权限为 `denied` 的设备不发（省额度），等 App 下次上报权限恢复。

## 5. 事件、推送对象与偏好

| 事件键 | 名称 | 推给谁 | 默认 | 跳转 |
| --- | --- | --- | --- | --- |
| `imported` | 入库完成 | 订阅的人（发起人 + 关注者）；手动下载的，推给点下载的人 | 开 | 媒体库里的这一部 / 这一集，找不到就订阅详情 |
| `download_started` | 开始下载 | 订阅的人 | 关 | 订阅详情 |
| `upgraded` | 洗版完成 | 订阅的人 | 关 | 订阅详情 |
| `library_new` | 媒体库有新片 | 打开了这项、勾选了这个库（或选了「全部」）的人 | 关 | 这部片 / 这一集；一批时到这个库 |
| `new_device` | 新设备登录 | 账号本人（不发给刚登录的那台） | 开 | 账号 → 设备 |
| `system_alert` | 需要处理的问题 | 管理员 | 开 | 能修它的设置页 |
| `new_version` | 有新版本 | 管理员（每个版本只推一次） | 开 | 设置 → 更新与维护 |

- 订阅的人 = 发起人（`created_by_member_id`，空为管理员）∪ 关注者。
- 手动搜索下载：提交时在手动下载锚上记下是谁点的（`submitted_by_member_id`，空为管理员），
  监听导入认领这个锚、入库成功时推「入库完成」给他。「开始下载」不推：是他自己刚点的。
  没进媒体库的下载（直接下到某个目录）不推。
- 自己订阅或手动下载的片，`library_new` 不再重复推给本人。
- 收件人看不到的条目一律不推：走 `assert_item_visible`（库可见范围 + 内容分级）。
- `new_device`：App、命令行、转码器的新凭证才算，网页登录不算；同一台设备重新登录
  （替换旧凭证）也不算。
- `system_alert`：待处理事项（`system_notice`）新出现或复发时推送，用户忽略过的不推。
- 同一事件、同一台手机（APNs 令牌相同）只推一条：一台 iPad 上登了家里两个人，不响两次。
- 偏好存在 `push_preference` 表，每人一行，没写过的事件用默认值。成员看不到、也改
  不了 `system_alert`。
- `library_new`（services/push/arrivals.py）：
  - **入口只有「账号 → 通知」**：打开开关后勾选关心的库，默认「全部」（我能看到的库，含以后
    新建的）。低频设定，不在媒体库页面放入口。
  - **怎么判断新片**：后台每两分钟看台账里新出现的行，不在各个入库路径上挂钩子。某部片（某一集）
    **第一次**出现在这个库里才算；同库同单元已有更早的行（洗版、多版本、改名）不算；扫描发现的
    文件在库建好后的头 24 小时内不算（新建库的首次全量扫描）。
  - **攒一攒再发**：一个库连续 5 分钟没有新行才发，最多等 30 分钟；不止一部就合成一条
    「『电影』新增 N 部：A、B、C 等」。同一部剧的多集合成一条。
  - 自己订阅了的片已经会收到「入库完成」，这里不重复；看不到的库、超出分级的片不推。
- `new_version`：每小时的更新检查发现新版本时推给管理员，每个版本只推一次；需要更新 Docker 镜像的版本会写明。

## 6. 密文、图片、collapse_id

- 加密：AES-256-GCM，格式 `v1.<key_id>.<nonce>.<密文>`，AAD 为 `v1.<key_id>`，
  用 `push-payload.md` 第 7 节的测试向量做单元测试（实例和 App 两边）。
- 明文按 2200 字节控制，超过先截短 `body`。
- 图片：`image` 是带签名的相对路径 `/api/v1/push/images/<签名>`，签名里只有 TMDB 图片
  地址和过期时间（7 天），不需要登录凭证——通知扩展直接取，不用共享 App 的登录令牌。
  签名在密文里，中继和苹果看不到；泄露出去也只是一张海报。
- `collapse_id`：`system_alert` 按 `dedupe_key` 生成，同一个问题的新通知替换旧的。
- `open` 是网页站内路径（如 `/subscriptions/42`），网页和 App 用同一套路由。

## 7. 接口

响应都是 `{success, code, message, data}`。时间是带时区的 ISO 8601（UTC，`+00:00`）。
全部接口在 OpenAPI 里标 `x-cli-hidden`（命令行不需要）。

### 7.1 MovieClaw Cloud（管理员）

`GET /api/v1/cloud` → `CloudStatusView`：

```json
{
  "state": "disconnected",
  "health": null,
  "health_message": null,
  "cloud_url": "https://api.movieclaw.io",
  "custom_cloud_url": false,
  "server_name": "客厅 NAS",
  "pairing": null,
  "connection": null,
  "last_disconnect": null,
  "report_stats": true,
  "last_report": null,
  "last_report_at": null,
  "notices": []
}
```

| 字段 | 说明 |
| --- | --- |
| `state` | `disconnected` / `pairing` / `connected` |
| `health` | 只在 `connected` 时有：`ok` / `unreachable`（续签失败但令牌还有效）/ `expired`（令牌过期，官方通道停用）/ `unsupported`（版本不受支持） |
| `health_message` | `health` 不是 `ok` 时给人看的说明 |
| `custom_cloud_url` | 设置了 `MOVIECLAW_CLOUD_URL` |
| `server_name` | 连接时默认的服务器名称 |
| `pairing` | `{user_code, verification_uri, verification_uri_complete, qrcode_image, expires_at, status, message, instance_name}`；`status` 为 `pending` / `denied` / `expired` / `error`；`qrcode_image` 是 `verification_uri_complete` 的二维码（SVG 的 data URL，与 IM 绑定同一套生成方式） |
| `connection` | `{instance_id, instance_name, account_display, connected_at, last_renew_at, token_expires_at, scopes, capabilities, limits}` |
| `last_disconnect` | 上次被动断开的原因 `{reason, message, at}`（`reason`: `revoked`），未连接时显示；重新连接后清空 |
| `last_report` | 最近一次上报的原文（对象） |
| `notices` | 云端发来的服务通知 `{id, level, message}`，已关闭的不再返回 |

| 接口 | 说明 |
| --- | --- |
| `POST /api/v1/cloud/pairing` `{instance_name?}` | 开始连接（已连接时 409）；返回 `CloudStatusView` |
| `DELETE /api/v1/cloud/pairing` | 取消配对 |
| `POST /api/v1/cloud/renew` | 立即同步一次（结果反映在 `health`） |
| `POST /api/v1/cloud/disconnect` `{force?}` | 断开；云端连不上且没带 `force` 时返回 409，`code` 为 `CLOUD_UNREACHABLE` |
| `PUT /api/v1/cloud/settings` `{report_stats}` | 统计开关 |
| `POST /api/v1/cloud/notices/{notice_id}/dismiss` | 关掉一条服务通知 |

以上都返回 `CloudStatusView`。

### 7.2 App 推送（管理员）

`GET /api/v1/push/channels` → `PushChannelsView`：

```json
{
  "cloud_state": "connected",
  "channels": [
    {
      "id": "official",
      "kind": "official",
      "name": "MovieClaw 官方推送",
      "enabled": true,
      "state": "ok",
      "status_text": "正常",
      "url": "https://push.movieclaw.io",
      "auth_mode": "issuer",
      "token_hint": null,
      "software": "movieclaw-push/0.1.3",
      "topics": ["io.movieclaw.app"],
      "quota": {"limit": 5000, "used": 132, "remaining": 4868, "reset_at": "2026-10-04T00:00:00Z"},
      "last_success_at": "2026-10-03T08:12:40Z",
      "last_error": null,
      "device_count": 5
    }
  ],
  "uncovered": [
    {"topic": "com.friend.mc", "device_count": 1}
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `channels[].kind` | `official` / `custom` |
| `channels[].state` | `inactive`（官方：未连接云，或已停用）/ `ok` / `warning`（限额用完、版本即将不受支持、最近失败过）/ `error`（连不上、令牌无效、版本不受支持） |
| `channels[].status_text` | 给人看的一句话状态 |
| `channels[].auth_mode` | `issuer` / `static` / `none` / `null`（没拉到过 `/v1/info`） |
| `channels[].token_hint` | 自建中继令牌的打码形式（`mcpush_a1b2…`） |
| `channels[].quota` | 中继最近一次返回的当日额度，没有为 `null` |
| `channels[].device_count` | 走这个通道的设备数（设备明细在「设备」页） |
| `uncovered` | 登记了推送、但没有任何可用通道的 App 版本；空 = 没有缺口，页面不提示 |

| 接口 | 说明 |
| --- | --- |
| `PUT /api/v1/push/channels/official` `{enabled}` | 启用 / 停用官方通道 |
| `POST /api/v1/push/relays/probe` `{url}` | 检测中继，返回 `RelayProbeView` |
| `POST /api/v1/push/relays` `{name, url, token?}` | 添加（服务端再检测一次；`static` 必须带令牌） |
| `PATCH /api/v1/push/relays/{relay_id}` `{name?, url?, token?, enabled?}` | 修改；改了地址会重新检测 |
| `DELETE /api/v1/push/relays/{relay_id}` | 删除 |
| `POST /api/v1/push/relays/{relay_id}/refresh` | 重新拉取 `/v1/info` |

除 probe 外都返回 `PushChannelsView`。`RelayProbeView`：

```json
{
  "url": "https://push.home.example",
  "reachable": true,
  "error": null,
  "software": "movieclaw-push/0.1.3",
  "protocol": 1,
  "auth_mode": "static",
  "topics": ["com.yi.movieclaw"],
  "types": ["alert", "background"],
  "matched_devices": 1,
  "warnings": ["地址是 http:// 而且不在局域网：令牌会明文经过公网"]
}
```

### 7.3 我的通知（所有登录的人）

`GET /api/v1/push/me` → `MyPushView`：

```json
{
  "instance_ready": true,
  "is_admin": false,
  "events": [
    {"key": "imported", "title": "入库完成", "description": "你订阅的电影、剧集整理进媒体库时", "group": "我的订阅", "enabled": true, "default": true}
  ],
  "ready_devices": 2,
  "libraries": [{"id": 1, "name": "电影", "kind": "movie"}, {"id": 2, "name": "剧集", "kind": "tv"}],
  "library_ids": null,
  "attention": [
    {"device_id": "ld-15", "device_name": "iPad", "status": "permission_denied", "status_text": "系统通知已关闭，在这台设备的设置里打开"}
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `instance_ready` | 服务器有没有任何可用通道（没有时成员看到「管理员还没有开启手机通知」） |
| `ready_devices` | 我能收到通知的设备数 |
| `libraries` | 我能看到的媒体库，「媒体库有新片」的选项 |
| `library_ids` | 「媒体库有新片」关心的库；`null` = 能看到的全部（含以后新建的） |
| `attention` | 我收不到通知、需要处理的设备（`permission_denied` / `no_channel` / `bad_token`）；空 = 没问题，页面不提示 |

设备的推送状态挂在设备上：`GET /api/v1/auth/devices` 每台 App 类设备多一个
`push: {status, status_text}`（`ok` / `permission_denied` / `no_channel` / `bad_token` /
`not_registered`），其他设备为 `null`。界面只在不是 `ok` 时在设备下面写一行原因。

| 接口 | 说明 |
| --- | --- |
| `PUT /api/v1/push/me/preferences` `{events?: {<key>: bool}, library_ids?: [int] \| null}` | 改开关和关心的库（`library_ids`：`null` = 全部，不传 = 不改），返回 `MyPushView` |
| `POST /api/v1/push/me/test` | 给自己的设备发一条测试通知，返回 `{sent, results: [{device_id, device_name, result, message}]}`；10 秒内只能发一次 |
| `PUT /api/v1/push/me/registration` | App 登记（只接受 App 类设备凭证），见下 |
| `DELETE /api/v1/push/me/registration` | 清掉这台设备的推送登记 |

登记请求：

```json
{
  "token": "a1b2c3…",
  "topic": "io.movieclaw.app",
  "environment": "production",
  "types": ["alert"],
  "key_id": "k7Qm2xP9Hn4",
  "key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
  "permission": "authorized"
}
```

- `permission`：`authorized` / `provisional` / `ephemeral` / `denied` / `not_determined`。
- `token`、`key_id`、`key` 三个要么都有，要么都没有（只上报权限状态，比如用户拒绝了
  通知、App 拿不到 APNs 令牌）。
- `key` 是 32 字节密钥的 base64url（无填充）；`key_id` 是 11 个 base64url 字符。

响应 `PushRegistrationView`：`{registered, status, status_text, channel_name}`，
`status` 同设备的 `push.status`。

### 7.4 推送配图（公开）

`GET /api/v1/push/images/{token}`：验签通过返回图片，否则 404。

## 8. 网页

- 「MovieClaw Cloud」：未连接 / 配对中 / 已连接 / 异常四种状态；配对弹窗每 2 秒轮询
  `GET /cloud`，批准后自动变成已连接。
- 设备只在「账号 → 设备」里列：推送状态是设备的属性，没问题时什么都不显示，收不到通知的
  设备下面写一行原因。管理员切到「全部成员」就能看到全家谁收不到。
- 「App 推送」只管通道：每个通道一行，带启用开关和「N 台设备」；官方通道未连接 MovieClaw
  Cloud 时开关关着且按不动。有没有可用通道的 App 版本时，顶部才出一条提示。
- 「通知」只管收什么：事件开关按 `group` 分组、测试按钮；自己有设备收不到时顶部出一条提示，
  点进「设备」页。
- 「消息推送」改名「IM 推送」。

## 9. iOS App

- **权限**：登录服务器（密码登录、创建管理员、配对）成功后马上请求通知权限
  （提醒、声音、角标）。升级前就已登录、从没被问过的人，启动时问一次。系统只会弹一次，
  之后只读取当前状态；没带通知扩展的侧载版不问。
- **登记**：拿到 APNs 令牌后，给本机保存的**每一个**账号（每台服务器 × 每个账号）各
  登记一次：每个账号一把自己的密钥，用那个账号的设备令牌调 `PUT /push/me/registration`。
  每次启动、每次登录、令牌变化时都重新登记。
- **密钥**：每个登录（服务器 + 账号）一把 256 位随机密钥和一个 `key_id`，放在 App Group
  共享的钥匙串（访问组用 App Group，`AfterFirstUnlockThisDeviceOnly`），通知扩展读得到；
  `key_id → 服务器地址、服务器名、账号名` 的对照放在 App Group 的 UserDefaults。
  退出登录、令牌失效时删掉这把密钥和对照。
- **环境**：调试版报 `development`，TestFlight 和 App Store 报 `production`。
- **通知扩展**（`MovieClawNotificationService`）：按 `key_id` 取密钥解密、校验 `type`；
  设置标题、正文、分组（按服务器分开）、声音；来源标注按明文的 `source`：内容类（入库、
  下载、洗版）不标，点开时自动切过去；管理员告警在手机连了多台服务器时标服务器名；
  新设备登录的正文里已写明服务器，同一台服务器登了多个账号时再标账号名；有 `image` 时从「服务器地址 + 路径」下载，5 秒超时，失败不带图（服务器给的
  是 WebP，通知附件不支持，扩展里转成 JPEG 再附上）；解不开就保留中继的通用文案。
- **调试**：模拟器能拿到真实的 APNs 令牌，走官方中继能收到真实推送；`xcrun simctl push` 不会经过
  通知扩展（只显示通用文案），测解密要走真实推送。
- **点开**：按 `key_id` 找到对应的服务器和账号，不是当前账号就先切过去，再按 `open`
  用现有的网页路径路由打开。
- **设置**：「服务器设置」里加「通知」（所有人）和「MovieClaw Cloud」（管理员）。

## 10. 高可用与安全

- 云端宕机：令牌 24 小时有效、每小时续签，一天内推送不受影响；重启也不受影响（令牌
  加密落库）。
- 官方中继：多个地址按优先级切换；自建中继：同一 Bundle ID 的下一个通道接替。
- 推送失败不影响任何业务链路：推送全部在后台任务里，异常只记日志。
- `instance_secret` 只发给云端 api，`access_token` 只发给官方中继或管理员加过的中继。
- `device_code`、凭证、设备令牌、密钥不写日志；日志只记推送的追踪 `id`、设备名和结果。
- 自建中继：`http://` 且不在局域网时提示令牌会明文经过公网；`auth.mode: none` 且不在
  局域网时提示任何人都能用你的推送密钥。只提示，不拦。

## 11. 不在本期

- tvOS App 的推送（角标、刷新顶栏）：实例按设备上报的推送类型发，tvOS 以后只报
  `background` 即可，实例不用改。
- 实时活动、小组件推送。
- 检测同一份凭证被两台实例同时使用（从备份恢复到另一台机器时）。
