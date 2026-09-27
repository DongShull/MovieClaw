# 播放引擎：FFmpeg 负责拆，Apple 负责播

> 状态：**阶段 0（实测验证）进行中**，分支 `feat/ios-player-engine`。开发期用启动参数
> `-movieclaw.player.engine native` 启用自研引擎；默认仍是现有的「系统播放器 + MPV」（见 [ios-app.md](ios-app.md) §4）。
> 起播链路与测量手段见 [playback-startup.md](playback-startup.md)。

## 1. 前提与原则

2026-09-27 定下的前提：**服务端（NAS）大多数时候性能弱，终端越来越强**，所以要用好终端；Apple TV 一定要做；
兜底链路不能丢。由此推出的规则：

- **终端做实时的重活**：解封装、解码、字幕、音频处理都在终端。服务端只提供文件字节与已探测的事实，
  空闲时可以做后台预计算，但客户端从不等它、不依赖它。
- **平台独占的能力交给平台**：让电视进入杜比视界模式、全景声以 MAT 2.0 透传、tvOS 画中画、帧率匹配、
  iOS 27 系统生成字幕，只有 AVPlayer 能做。所以主力通路必须以 AVPlayer 结尾，FFmpeg 只做它最擅长的事：拆容器。
- **只画平台画不了的**：AVPlayer 解不了的编码才走软件解码；mpv 只兜底；不写自己的渲染器和色调映射。
- **失败按常态设计**：兜底阶梯显式建模，每次回落带原因上报。

为什么不是「自己解码 + AVSampleBufferDisplayLayer」为主：杜比视界的显示握手、全景声透传、tvOS 画中画
（sample-buffer 内容源在 tvOS 上不被接受，Apple FB9751461）都拿不到。

## 2. 架构

```
PlayerScreen（控制层 UI、手势、文字字幕叠加）
  └─ PlaybackController（会话协议、选引擎、兜底阶梯、看门狗）
       └─ PlayerEngine 协议
            ├─ NativeEngine    自研引擎（App 侧适配层）
            │    └─ AetherCore.framework（动态框架，App 与 AetherEngine 之间唯一的边界）
            │         └─ AetherEngine（LGPL-3.0 + App Store 例外）+ AetherLib* FFmpeg 动态框架
            ├─ MPVEngine       兜底：libmpv 直出原文件（MPVCore.framework）
            └─ AVPlayerEngine  兜底：服务端 HLS（转码 / 换封装）
```

自研引擎内部有两条通路，对控制器透明：

- **主力通路（loopback）**：FFmpeg 读 NAS 原文件 → 就地换封装成 HLS-fMP4 → `127.0.0.1` 本机服务 → AVPlayer。
  杜比视界（P5 / P8.x，P7 实时转 8.1）、HDR10+ / HLG、全景声原样拷贝都在这里；TrueHD / DTS 在本机解码后重编。
- **软件通路（software）**：VP9、VC-1、MPEG-2、MPEG-4 ASP、隔行 H.264 等 AVPlayer 不收的编码，
  FFmpeg / dav1d 解码后交给 `AVSampleBufferDisplayLayer`。

### 为什么要 AetherCore 这层动态框架

同一个 App 里有两份 FFmpeg：MPVCore 里静态打着 MPVKit 的一份，AetherEngine 自带 `AetherLib*` 一份。
AetherEngine 调 `avcodec_*` 时绑到哪一份由链接顺序决定，绑错了的症状像引擎 bug（上游文档「One FFmpeg」）。
把它收进独立动态框架后，它的 FFmpeg 引用在这个框架链接时就绑定到 `AetherLib*`，与 MPVCore 互不干扰；
这也是 LGPL 组件的替换边界。**App 只 import AetherCore，不得直接依赖 AetherEngine。**
核对方法：`nm -m AetherCore.framework/AetherCore | grep _avcodec_open2` 应显示 `(from AetherLibavcodec)`。

## 3. 兜底阶梯

| 级 | 通路 | 什么时候走 |
|---|---|---|
| L0 | 自研引擎主力通路（换封装 → AVPlayer） | H.264 / HEVC / 有硬解的 AV1，任意容器 |
| L1 | 自研引擎软件通路 | AVPlayer 解不了的编码（引擎内部自动切换） |
| L2 | mpv 直出原文件 | 自研引擎放不了 |
| L3 | 服务端 HLS → AVPlayer | 本机全部失败；外网带宽不够时首选 |
| L4 | 中文报错 | 服务端也给不出 |

取流失败（断线、令牌过期）在同一级原地重开；解码类失败才降级。阶段 0 里 L0 → L2 的回落由
`PlaybackController.nativeFallback` 实现（`engine-fallback` 日志，`from=native`）。

## 4. 片库画像（2026-09-27，NAS 只读统计）

电影库 + 剧集库在位 11128 个文件、39.22 TB。按文件数和按字节看是两个完全不同的头部：

- 日常剧集：MKV/MP4 + HEVC/H.264 + SDR + AAC 系 + 文本或无字幕，占 75.8% 文件、27.8% 字节；
- 高规格电影：MKV/BDMV + HEVC 10bit 4K + HDR10/杜比视界 + TrueHD Atmos / DTS-HD MA，占 4.3% 文件、31.3% 字节。

文件少、字节多的几项决定了优先级：原盘与 ISO 25.1% 字节（UHD 原盘的杜比视界是 P7 FEL、增强层在单独 PID）；
主音轨 TrueHD / DTS 46.9%；位图字幕（PGS）48.5%，电影的中文字幕 74.8% 字节靠 PGS；
杜比视界约 82% 是 P5。AVPlayer 解不了的编码只占 1.1% 文件，AV1 为 0。

## 5. 与 AetherEngine 的关系

- **依赖，不 fork**：`project.yml` 钉死版本（`exactVersion`），包在 `PlayerEngine` 协议后面；需要的改动尽量提上游 PR。
  它的代码量大（最大的文件约 7400 行），长期自己维护一个分叉代价很高。
- **升级前跑语料回归**：上游几乎每天发版，升级版本号前要用第 6 节的语料在模拟器上过一遍。
- **许可**：AetherEngine 为 LGPL-3.0 加 App Store 例外，改了引擎本身要按 LGPL 公开；FFmpeg 以动态框架随包分发。
  MovieClaw（MIT）与之共存，关于页需列出组件与源码地址（上架前补）。
- **我们自己做的部分**：路线规划与兜底阶梯、服务端配合（直出会话、媒体事实、补探测盲区、原盘虚拟拼接）、
  字幕叠加层（引擎只给字幕数据）、iOS / tvOS 界面、语料回归，以及给引擎补的能力（原盘双 PID 杜比视界等）。

## 6. 阶段 0 验证清单

在专用模拟器 MC-Claude-Air 上连 NAS 验证（真机与 Apple TV 的杜比视界、全景声、能耗另测）。
启动参数：`-mcServer http://192.168.1.10:3000 -mcUser yee -movieclaw.player.engine native -mcAetherLog YES -mcRoute /play/<条目>[/sXXeYY]`。

| 语料 | 条目 | 验什么 |
|---|---|---|
| 《疾速追杀4》UHD 原盘 | `/play/6512` | BDMV 单剪辑、P7 FEL 双 PID、TrueHD Atmos、PGS |
| 《变形金刚4》Remux | `/play/6859` | MKV 杜比视界 P8、TrueHD Atmos、PGS |
| 《梦华录》S01E18 | `/play/6997/s01e18` | MP4 杜比视界 P5（hev1 样本入口） |
| 《天盛长歌》S01E01 | `/play/7072/s01e01` | MP4 杜比视界 P5（dvh1）、DD+ Atmos |
| 《三体》S01E04 | `/play/7128/s01e04` | MKV 杜比视界 P5、DD+ Atmos、SRT |
| 《权力的游戏》S07E04 | `/play/6998/s07e04` | MKV 杜比视界、TrueHD Atmos、PGS |
| 《异星灾变》S01E02 | `/play/7038/s01e02` | H.264 1080p、DTS-HD MA、PGS |
| 《疯狂动物城2》UHD 原盘 | `/play/6517` | BDMV、9 条音轨 |
| 《不一样的天空》原盘 | `/play/6949` | BDMV 多剪辑（目前服务端给 HLS） |
| 《戴珍珠耳环的少女》原盘 | `/play/6681` | VC-1（软件通路） |
| 《公司的力量》S01E01 | `/play/7104/s01e01` | DVD ISO、MPEG-2、MP2 |
| 《他是谁》S01E15 | `/play/7114/s01e15` | 4K 60 帧 |
| 《觉醒年代》S01E12 | `/play/6552/s01e12` | 4K HLG TS |
| The Age of A.I. S01E05 | `/play/7134/s01e05` | VP9（软件通路） |
| 《小猪佩奇》S05E50 | `/play/7052/s05e50` | MPEG-4 ASP（软件通路） |
| 《睡衣小英雄》S01E21 | `/play/6864/s01e21` | 22 条音轨、切音轨耗时 |
| 《大空头》 | `/play/6805` | DTS:X、PGS |

结果记录见第 7 节。

## 7. 验证结果（2026-09-27，第一轮）

### 7.1 Mac 上跑主力通路

模拟器的 VideoToolbox 不完整（见 7.2），主力通路先在 Mac 上用上游自带的 `aetherctl play` 验：同一套
「换封装 → AVPlayer」代码，macOS 27、Apple 芯片硬件解码，直接拉 NAS 原文件（取流地址按「全解码直出」申报拿到）。

| 语料 | 通路 | 首帧 | 说明 |
|---|---|---|---|
| 《三体》S01E04 MKV | 主力 | 0.61 秒 | 杜比视界 P5 → `dvh1.05.06`；DD+ Atmos 原样拷贝（JOC） |
| 《梦华录》S01E18 MP4 | 主力 | 0.68 秒 | P5 的 `hev1` 样本入口被改写成 `dvh1` |
| 《天盛长歌》S01E01 MP4 | 主力 | 0.35 秒 | P5（dvh1）+ DD+ Atmos |
| 《变形金刚4》Remux | 主力 | 0.56 秒 | P8.1：`hvc1` + `dvh1.08.06/db1p`；TrueHD 本机解码后重编 EAC3 |
| 《权力的游戏》S07E04 | 主力 | 3.12 秒 | 同上；同一时段服务端连接池告急（7.3），首帧偏慢待重测 |
| 《赛车总动员2》 | 主力 | 1.48 秒 | HDR10 + TrueHD Atmos → EAC3 |
| 《异星灾变》S01E02 | 主力 | 1.33 秒 | H.264 + DTS-HD MA → EAC3 |
| 《疯狂动物城+》S01E05 | 主力 | 0.56 秒 | HDR10 + DD+ Atmos 原样拷贝 |
| 《疾速追杀4》UHD 原盘 | 主力 | 1.68 秒 | 按 HDR10 播放：原盘双 PID 杜比视界未识别（预期内，待补） |
| 《疯狂动物城2》UHD 原盘 | 主力 | 1.56 秒 | 9 条音轨 |
| 《戴珍珠耳环的少女》VC-1 原盘 | 软件 | 1.05 秒 | |
| 《他是谁》S01E15 4K 60 帧 MP4 | 主力 | 2.07 秒 | moov 在文件尾，起播多一次往返（上游 #281） |
| 《觉醒年代》S01E12 HLG TS | 主力 | 0.68 秒 | |
| The Age of A.I. VP9 / 《小猪佩奇》MPEG-4 ASP | 软件 | 0.67 / 0.54 秒 | |
| 《睡衣小英雄》22 条音轨 | 主力 | 0.50 秒 | |
| 《大空头》DTS:X | 主力 | 1.39 秒 | DTS → EAC3 |
| 《公司的力量》DVD ISO | — | — | 服务端对 ISO 直出返回 404（现有 MPV 路径同样失败，7.3） |

结论：主力通路在 Apple 硬件解码下覆盖了片库的头部组合，杜比视界 P5 / P8.1 的信令、全景声原样拷贝、
TrueHD / DTS 重编都工作；首帧 0.35～1.7 秒，其中无损音轨要重编、原盘要多读索引的偏慢，需到 iPhone 上再量。

### 7.2 模拟器（MC-Claude-Air，iOS 27）接入 App 后

- **模拟器的限制**：VideoToolbox 对 H.264 报「找不到解码器」（-12906），引擎按设计把 H.264 改走软件通路；
  HEVC 能走主力通路；杜比视界 P5 在模拟器上 AVPlayer 解不了，按兜底阶梯回落到 MPV。
  所以**杜比视界、全景声、画中画、能耗只能真机验**。
- 起播（点击 → 开始播放）：《异星灾变》0.58 秒（软件通路，PGS 字幕位置正确）、《睡衣小英雄》0.74 秒、
  《大空头》0.97 秒、《他是谁》1.07 秒、《变形金刚4》1.06 秒、《权力的游戏》1.21 秒、《疾速追杀4》原盘 2.5 秒。
  其中「开会话」一段 0.3～0.6 秒，比起播专项量到的慢，属于服务端负载，与引擎无关。
- 软件通路上换音轨约 0.2 秒、跳转到 10 分钟处 0.06 秒恢复播放（《睡衣小英雄》，`-mcAutoAudio` / `-mcAutoSeek`）。
- 兜底阶梯按设计工作：P5 在模拟器失败 → MPV；多剪辑原盘服务端给 HLS → MPV 放；ISO 404 → 直接回落不重试。
- 两份 FFmpeg 核对：`AetherCore` 的 `_avcodec_open2` 绑定到 `AetherLibavcodec`；MPVCore 确实导出了自己那份 FFmpeg 符号，
  若把 AetherEngine 直接链进 App 就有绑错的风险——独立动态框架是必要的。

### 7.3 发现的问题

1. **服务端取流接口在响应期间一直占着数据库连接（已修，待部署）**：FastAPI 的 yield 依赖要等响应发完才收尾，
   直出长连接因此各占一个连接；本机换封装的引擎一次会开主读取、尾部预读、字幕预读几路，两三个播放器就能耗尽
   连接池（5 + 溢出 10），全站接口 30 秒超时——第一轮里《他是谁》《觉醒年代》失败、模拟器卡在「正在判断播放方式」都是它。
   修法：读完台账立即 `session.close()`；测试 `test_direct_play_releases_the_db_connection_before_streaming`
   （去掉修复时复现为占用 1 个连接）。
2. **服务端不提供 ISO 原字节直出**：`is_disc()` 把 ISO 当原盘，找不到单剪辑就 404。引擎能读解密的 BD / DVD ISO，
   要放行 ISO 原字节，并在 AetherCore 里给经 HTTP 的 ISO 提供按 Range 读取的字节源。
3. **原盘双 PID 杜比视界未识别**：UHD 原盘按 HDR10 播放。要从增强层 PID 取 RPU、注入基础层再转 8.1，引擎侧补（适合提上游）。
4. **起播时换非默认音轨要在首帧后重载一次**（主力通路约 0.5～1 秒黑屏）：原盘默认挑到的是 AC-3 核心，服务端决策要 TrueHD。
   改进：服务端媒体事实带上音轨的流序号，装载时直接指定（`audioSourceStreamIndex`）。
5. **MP4 的 moov 在文件尾时起播多一次往返**：服务端媒体事实可以带 moov 偏移。
6. 服务端取流接口不支持 HEAD（405），引擎会退回 Range GET，影响很小。
