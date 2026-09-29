# iOS App 打包与上架

> 2026-09-28 开发者账号开通后整理。首发路线：TestFlight 内部测试 → TestFlight 对外公开链接 → App Store。
> 首版只上 iPhone；只有一个发行版本，App 里不提供资源站点 / 下载器 / 自动入库 / 订阅规则的配置（在网页端管理）。
> **要亲手做的事按顺序列在 [ios-release-checklist.md](ios-release-checklist.md)（含新机器搭环境）。**
> 相关：App 设计 [ios-app.md](ios-app.md)，Mac 转码器的签名公证见 `macos/MovieClawTranscoder/README.md`。

## 1. 只有一个发行版本

同一个构建既进内部 / 对外 TestFlight，也用于提审和上架；开发调试版与发布版功能相同（只差 `#if DEBUG` 的调试开关）。

App 里**不提供**「资源与下载」这组配置（订阅规则、资源站点、下载器、自动入库，见
`SettingsSection.availableInApp`）：设置首页不列出；其他页面里去这几项的跳转与深链落到
「请在网页端管理」页，给出直达网页对应分区的按钮。种子搜索、订阅操作、下载任务等其余功能照常。

为什么这样划：审核条款 5.2.3（不得便利非法文件共享）是这类 App 最常见的拒审点，
站点、Cookie、下载器接入是最显眼的一块。如果审核仍以 5.2.3 拒审，按审核意见扩大不提供的范围。

历史：2026-09-28 起曾分「完整版 / 商店版」两个编译版本（`MC_STORE` 与 `release.sh --store`），
2026-09-29 用户决定只维护一个版本，统一按原商店版的范围，编译开关与 App 内这几页配置代码一并删除
（需要时从 git 历史找回）。**不要**用远程开关在过审后再打开隐藏功能：违反审核条款 2.3.1，
处罚可到终止开发者账号。

## 2. 一次性准备（需要账号持有人在网页上操作）

1. **确认团队 ID**：developer.apple.com → Membership 里的 Team ID，写进本机
   `apps/apple/XcodeConfig/Signing.local.xcconfig` 的 `DEVELOPMENT_TEAM`（不入库）。
   付费团队和之前的个人免费团队 ID 不同，别混用。
2. **Xcode 登录账号**（Xcode → 设置 → 账户）：真机调试要用。打包导出靠下一条的 API 密钥——
   Xcode 27 的 xcodebuild 读不到 Xcode 里登录的账号，不给密钥时导出报 `No Accounts`。
3. **App Store Connect API 密钥**：App Store Connect → 用户和访问 → 集成 → App Store Connect API，
   新建一个**「管理」**角色的团队密钥（「App 管理」用不了云端托管的发布证书，导出报
   `Cloud signing permission error`），下载 `.p8`（只能下载一次）。
   放到 `~/.appstoreconnect/private_keys/AuthKey_<密钥 ID>.p8`，记下密钥 ID 与 Issuer ID。
   同一把密钥也用于 Mac 转码器的公证。**不要提交进仓库、不要贴进聊天或日志。**
4. **建 App 记录**：App Store Connect → App → 新建 App
   - 平台 iOS，名称 MovieClaw（被占用就换，例如「MovieClaw 影音」），主要语言简体中文；
   - 套装 ID 选 `io.movieclaw.app`（没有就先在 developer.apple.com → Identifiers 注册，或让
     Xcode 自动签名首次导出时自动注册）；SKU 随意（如 `movieclaw-ios`）。
5. **App 信息**：
   - 隐私政策网址：`https://github.com/movieclaw/movieclaw/blob/main/docs/privacy-policy.md`
     （该文件随 feat/ios-app 合入 main 后才可访问；合入前可临时用分支地址）；
   - 技术支持网址：`https://github.com/movieclaw/movieclaw/issues`；
   - 类别：娱乐（或摄影与录像）；价格：免费；
   - App 隐私问卷：**不收集任何数据**（数据都在用户自己的服务器上，见隐私政策）；
   - 年龄分级：内容来自用户自己的服务器，按问卷如实填写（「不受限制的网络访问」选是）。
6. **TestFlight 测试组**：内部测试组只能加团队成员（最多 100 人，免审）；朋友等外部测试员加进对外测试组
   （邮箱或公开链接，最多 1 万人），组里第一个构建要过一次 Beta 审核（见 §4）。iPhone 上装 TestFlight App。

## 3. 打包与上传

```bash
cd apps/apple
export MC_ASC_KEY_ID=… MC_ASC_ISSUER_ID=…   # 建议放本机 ~/.appstoreconnect/ 下的 env 文件里 source，不入库
scripts/release.sh --upload                 # 上传：同一个构建可用于内部 / 对外 TestFlight 与提审
```

- 构建号默认取 UTC 时间 `yyyyMMddHHmm`，天然递增；营销版本号在 `project.yml` 的
  `MARKETING_VERSION`（首发 0.1.0，之后每次提审递增）。
- 产物与日志在 `apps/apple/build-release/`（已被 git 忽略），归档约 5 分钟，DerivedData 约 0.7 GB，
  磁盘紧时打包完可删。
- 不带 `--upload` 只在本机导出 `.ipa`，用于验证签名；首次导出时自动签名会在账号下创建
  「Apple Distribution」证书与 App Store 描述文件，属正常流程。
- 上传后 5～30 分钟处理完才出现在 TestFlight；处理期间 Apple 会发邮件报告问题，
  **首次上传务必看邮件**（见 §5）。

## 4. 提交审核（对外测试与上架）

对外 TestFlight 首个构建要过一次 Beta 审核，上架要过正式审核，两者都要：

- **演示服务器与账号**：App 离开服务器无法使用，审核员必须能登录。准备一台公网可达、
  HTTPS 的演示服务器（不要用自己的真实 NAS），只放开放授权的片源（Big Buck Bunny、
  Sintel、Tears of Steel 等），不接入任何资源站点和下载器；建一个普通成员账号给审核员。
  服务器地址与账号密码填在「App 审核信息 → 登录信息」与备注里。
- **审核备注模板**：

  > MovieClaw is a client for a self-hosted media server (open source: github.com/movieclaw/movieclaw),
  > similar to Jellyfin / Plex / Infuse clients. Users connect to a server they deploy themselves on
  > their NAS; the app itself hosts or provides no content.
  > Demo server: https://… — account: … / …
  > NSAllowsArbitraryLoads / local networking: most users run the server on their home LAN over
  > plain HTTP (e.g. http://192.168.1.10:3000), so the app must reach LAN addresses without TLS.
  > Background audio: continues playback and Picture in Picture.

- **截图**：6.9 英寸 iPhone（1320×2868 或 1290×2796）至少 3 张，用演示服务器的开放授权内容截，
  不要出现真实影片海报以外的版权敏感画面、种子名或站点名。
- **出口合规**：Info.plist 已声明 `ITSAppUsesNonExemptEncryption = NO`，上传不再追问。

## 5. 已知风险与首次上传要核对的

- **隐私清单**：App（`MovieClaw/PrivacyInfo.xcprivacy`）与引擎框架（`AetherCore/PrivacyInfo.xcprivacy`）
  已声明用到的「需声明原因的 API」（偏好设置、系统运行时长、磁盘空间、文件时间戳）。
  FFmpeg 的 `AetherLib*` 二进制框架不带清单；若上传后收到 ITMS-91053 邮件点名缺少某类声明，
  按邮件补到对应清单里。新增代码用到这几类 API 时同步更新清单。
- **开源许可**：「我的 → 关于 MovieClaw」列出随包分发的组件、许可与源码地址，全文随包
  （`Features/About/Licenses/`）。升级 AetherEngine / FFmpegBuild / Nuke 等依赖时同步核对。
  AetherEngine 是 LGPL-3.0 且带 App Store 例外；FFmpeg 为 LGPL-2.1（未启用 GPL 组件），
  以动态框架随包，满足可替换要求。
- **播放质量记录**：App 会把每次播放的起播耗时、跳转、卡顿、失败原因（失败时附最近的播放器日志，
  引擎自带脱敏）上报给**用户自己的服务器**，供管理员排查（设计见 [playback-qoe.md](playback-qoe.md)）。
  开发者不接收任何数据，隐私问卷仍填「不收集数据」；隐私政策已写明这一点。旧版服务器没有这个接口时
  回 404，App 直接丢弃记录，不重试、不积压。
- **调试开关只在调试版**：`-mc…` 启动参数（真机实验台、故障注入、强制通路）都在 `#if DEBUG` 里，
  发布构建不含；提审前不必额外清理。
- **TMDB 署名**：关于页已注明「本产品使用 TMDB API，但未经 TMDB 认可或认证」。
- **5.2.3**：见 §1。若被拒，审核意见会点名具体功能，据此扩大 App 内不提供的范围。
- **最低系统 iOS 26**：只有 iOS 26 及以上的 iPhone 能在商店里看到它。

## 6. Mac 转码器

与 iOS 共用同一个开发者账号，但证书不同：需要在 developer.apple.com → Certificates 新建
**Developer ID Application** 证书（只有账号持有人能建），导出成 `.p12` 后按
`.github/workflows/release.yml` worker-macos 作业的注释配置仓库密钥；公证复用 §2 的 API 密钥。
配好后每次发版的 `MovieClawTranscoder-macos-arm64.zip` 自动签名、公证、钉票据，用户双击即可打开。
