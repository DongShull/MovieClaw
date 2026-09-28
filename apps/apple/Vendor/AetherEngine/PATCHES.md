# MovieClaw 对 AetherEngine 的补丁（我们自己维护的 fork）

基线：上游 [AetherEngine](https://github.com/superuser404notfound/AetherEngine) **7.19.0**（LGPL-3.0 + App Store 例外）。
这份源码就是 MovieClaw 自己维护的 fork（2026-09-27 起）：引擎问题我们自己修、自己完善，不以「等上游合并」为前提。
补丁在源码里都标了 `[MovieClaw P<n>]` 或 `[MovieClaw patch P<n>]`，动机与实测见 `docs/design/player-engine.md`
第 7、8 节与 `docs/design/disc-direct-play.md`。LGPL 义务：修改后的源码随仓库公开，关于页注明组件与源码地址。

| 补丁 | 位置 | 做什么 | 为什么 |
|---|---|---|---|
| P1 | `Video/HLSVideoEngine.swift` | MPEG-TS / M2TS 源不做「索引预热」（跳到片中间那一次寻址） | TS 没有容器索引，预热什么也读不到，分片规划照样退回均匀切分；从 NAS 读蓝光原盘时这一次寻址实测 1.9 秒，起播时换音轨重载还要再付一次 |
| P2 | `PlayerState.swift`、`Native/NativeAVPlayerHost.swift`、`AetherEngine+Loading.swift`、`AetherEngine+ReloadWithOptions.swift` | 新增 `LoadOptions.vodStartsImmediately`：点播起播时，AVPlayer 已缓冲 ≥1.5 秒却还停在「为减少卡顿而等待」，一次性 `playImmediately` | 机制、门槛与直播的 AE#440 完全相同，只是对点播也生效。本机回环的生产者远快于 1 倍速，AVPlayer 等码率估计只是白等（真机 0.2～0.6 秒） |
| P3 | `Video/HLSVideoEngine.swift`、`Video/HLSVideoEngine+SegmentPlanning.swift` | 第一个分片的切分目标从 4 秒改成 1 秒（其后仍按 4 秒：阈值 1、5、9……秒），均匀切分时第一段不短于实测关键帧间隔 | AVPlayer 必须等第一个分片完整产出才能开播，它的大小就是起播延迟：87 Mbit/s 的 UHD Remux 第一片 43 MB，真机从 NAS 读要 0.84 秒 |
| P4 | `Demuxer/Demuxer.swift` | URL 片段 `#aether-disc-image` 声明「这是光盘镜像」，HTTP 源直接走镜像读取器 | MovieClaw 的取流地址 `/playback/files/{id}/stream?token=` 没有 .iso 后缀，引擎按后缀判断镜像，ISO 被当成普通媒体探测失败。片段只在本机、不随请求发出，重新装载时随 URL 沿用 |
| P5 | `Disc/DiscDirectory.swift`（新）、`Disc/DiscReader.swift` | 原盘目录（BDMV 文件夹）经 HTTP 直推：`DiscDirectoryReader` 协议 + `HTTPDiscDirectoryReader`（每个文件一个 Range 读取器）+ 按文件首尾相接的拼接读取器；`DiscReader.wrap` 在识别缓存之前分支到目录版，复用 MPLS、选主片、多剪辑拼接与时间轴折叠；可按服务端指定的主播放列表名选主片（只读这一个播放列表） | 多剪辑原盘原来要 NAS 起 ffmpeg concat 换封装，还丢杜比视界增强层、TrueHD 退 AC-3 核心、字幕不进流。改后服务端只按文件供字节 |
| P7 | `Disc/DiscSeekTable.swift`（新）、`Disc/DiscMetadata.swift`、`Disc/DiscRecognitionCache.swift`、`Disc/BDTitleSelector.swift`、`Disc/DiscReader.swift`、`Disc/DiscDirectory.swift`、`Demuxer/Demuxer.swift` | 蓝光按 CLPI 的 EP map 定位：识别时读选中标题各剪辑的 CLPI，定位时按折叠后的时间找剪辑、换成剪辑内原始 PTS、查表得到关键帧字节偏移，一次按字节定位 | 原来按时间二分：各剪辑时间戳互相重叠时落到别的剪辑、跳转落不了地（《完美陌生人》）；经 HTTP 读机械盘时每一步都是一次请求加一次寻道，续播起播 14.8 秒 → 2.7 秒（《不一样的天空》） |
| P8 | `Video/SegmentCache.swift`、`Video/HLSVideoEngine+LiveReopen.swift` | 分片缓存目录因磁盘已满建不出来时记下来，VOD 泵因此失败时报「存储已满」而不是「音频无法封装」 | 手机可用空间 197 MB 时 4K60 片子起播失败，报错却是「Source audio cannot be muxed (-22)」，真因是 NSCocoaErrorDomain 640 |
| P9 | `Demuxer/Demuxer.swift` | UHD 原盘双 PID 杜比视界：探测后把增强层（PID 0x1015）与基础层（PID 0x1011）配对，基础层补上 P7 记录（bl_present_flag=1；增强层带 DOVI 描述符就照抄，没有就按基础层分辨率/帧率合成、兼容 ID 6），增强层每个访问单元的 RPU 按 PTS 追加到同一时间戳的基础层包尾，增强层视频不送下游；定位时清空暂存 | 引擎只看基础层，UHD 原盘一律按 HDR10 播放；增强层还被「只保留选中流」丢掉。实测《疾速追杀4》原盘的节目映射表根本没有 DOVI 描述符（两路只有 HDMV 注册描述符，杜比视界只记在 CLPI/MPLS），所以按蓝光规格固定的 PID 配对。合并后下游现成的 P7 → 8.1 转换原样接手，真机画面「杜比视界 · 片源 DV P7（已转 8.1）」 |

| P10 | `Demuxer/Demuxer.swift` | 起播默认音轨：没有一条音轨标了默认（蓝光 / DVD / TS 都不标）时，语言以第一条音轨为准，同语言里优先 AVPlayer 能原样拷贝的编码（AC-3 / E-AC-3 / AAC） | av_find_best_stream 按帧数、码率挑，真机《怦然心动》蓝光镜像起播放的是西班牙语 AC-3（第一条是英语 DTS）。服务端的默认音轨是「标了默认的，否则第一条」，两边一致后 App 起播后不必为换语言再重载一次 |

| P11 | `PlayerState.swift`、`AetherEngine.swift`、`AetherEngine+ReloadWithOptions.swift` | 新增 `LoadOptions.audioTrackOrdinal`：起播音轨按「第几条音轨」指定，探测完换成流下标（显式 `audioSourceStreamIndex` 仍优先） | App 记着的是与服务端同口径的 embedded:N，装载前不知道流下标，原来只能首帧后 `selectAudioTrack` 重载一次：真机蓝光镜像续播记着的中文音轨，起播 2.5 → 4.2 秒 |

| P12 | `Demuxer/Demuxer.swift` | find_stream_info 期间把没有画布尺寸的 PGS 流、认不出编码的数据流（广播录像的 DSM-CC 数据轮播，编码号临时给 BIN_DATA 占位）、MP4 / MKV 里认不出编码的音轨（国产 4K 剧的菁彩声 Audio Vivid「av3a」）暂当附件（同 `parkUnresolvableAudio`），探测完放回 | libavformat 认定 PGS「参数不全」直到知道画布尺寸，字幕包又稀疏，探测一直读到 50 MB 预算：带 PGS 的片子起播里「探测流」0.6～1.1 秒，改后 0.2 秒以内（《权力的游戏》3.85 → 1.79 秒、《乱世佳人》1.22 → 0.46 秒）；日本电视台录像两路数据流同样拖满预算，《再见我们的幼儿园》1.12 → 0.40 秒；菁彩声音轨同理，《交锋》探测 1.51 → 0.14 秒、起播 3.48 → 1.77 秒（TS / PS 的未知流可能靠嗅探认出来，不动）。引擎画 PGS 的画布尺寸取自画面，用不上探出来的尺寸 |
| P13 | `Disc/DVDIFOParser.swift`、`Disc/DiscReader.swift`、`Disc/DiscMetadata.swift`、`Disc/DiscRecognitionCache.swift`、`Demuxer/Demuxer.swift` | DVD 主 PGC 的每个 cell 当成一个剪辑交给现成的时间轴折叠（`ClipSpan`），cell 的时间戳基准由导航包（PCI 的 VOBU 起始 PTS − cell 内已播时间）当场算出；有时间表（VTS_TMAPT）就「标题时间 → VOBU 字节偏移」一次定位。只处理主 PGC 从标题 VOB 开头起、cell 首尾相接、没有多角度块的盘；定位表也接进 `seekBounded`（软件通路与字幕旁路走它）；`SWClockAnchorPolicy` 只在首个样本晚于起播点时才按它重锚（粗粒度定位落在前面时时钟仍锚在起播点） | 整个 VTS 的 VOB 当一条 MPEG-PS 读，很多盘每个 cell 的 PTS 从头开始：《公司的力量》三个 cell 各自从 0.37 秒起，标题时间 2670 秒处归零，播放头与按时间二分的定位从那里起全乱；《聪明的一休》时间表 16 秒一格，续播落在 12 秒前，原来时钟锚在落点、画面等 8.9 秒才出，改后 0.68 秒 |
| P14 | `Decoder/HardwareVideoDecoder.swift`、`Native/SoftwarePlaybackHost.swift`、`AetherEngine.swift`、`AetherEngine+Loading.swift` | VP9（4:2:0 的 8/10 bit）走 VideoToolbox 硬解：iOS 26.2 起登记补充解码器（`VTRegisterSupplementalVideoDecoderIfAvailable`），格式描述用由流参数拼出的 vpcC；解码器标签按实际硬解/软解写 | 4K VP9 软解本进程 CPU 约 51%，连播 7 分钟温度到「偏热」；硬解后 18～25%（《The Age of A.I.》） |

| P15 | `IO/HTTPDiscIOReader.swift`、`Demuxer/Demuxer.swift` | 光盘镜像读取器按 LRU 留最近 8 个小块（≤ 1 MB）；光盘标题有 MPLS / IFO 时长时不再让 libavformat 从文件尾倒读估时长 | 识别盘内结构时目录项与文件数据两处按扇区来回跳，单缓冲每跳一次整块重取（26 次请求里 20 次）；估时长又从几十 GB 镜像的尾部倒着读十来次。蓝光镜像《怦然心动》起播 2.02 → 0.97 秒（开盘 0.63 → 0.14、探测 0.55 → 0.01） |

| P16 | `Video/SegmentCache.swift`、`Native/SoftwarePacketDiskFIFO.swift`、`AetherEngine+Prewarm.swift` | 死会话留下的缓存立刻清：分片目录与软件通路包缓存都带存活锁（flock，进程一死内核就放），锁没人拿着且建了 10 秒以上就删，不再等 1 小时 / 24 小时；新增 `sweepStaleSessionCaches()` 供宿主启动时清一遍（两种缓存原来只在建同类新会话时顺手清） | 被杀掉的播放会话每个留下最多 2 GB 分片、1 GB 上下包缓存：真机一夜测试 App 占用长到 16 GB（分片 8.9 GB + 包缓存 6.6 GB），此前 4K 片子因手机写满起播失败；改后临时目录 0.08 GB |

| P17 | `Native/SoftwarePlaybackHost.swift` | 软件通路跳转落地与起播锚时钟时，时钟先停在落点（速率 0），落点之后第 5 次送帧（渲染器重排缓冲 4 帧，这一次落点那一帧才交到显示层）再按当前速率走；兜底 0.6 秒，暂停取消等待 | 原来落地就走时钟，解码器还在从前一个关键帧往目标解，前十几帧一出来就迟到被丢，跳转后画面停半秒再跳：DVD 7 → 0、AVI 11 → 0、VP9 4 → 0 帧，起播时间不变 |
| P18 | `Demuxer/MatroskaCuesProbe.swift`（新）、`Demuxer/Demuxer.swift`、`Video/HLSVideoEngine.swift`、`Video/HLSVideoEngine+SegmentPlanning.swift` | 没有可用 Cues 的 MKV：打开时读文件头解析 SeekHead（跟不到的链、读不全的头都不下结论），判定没有或指向文件尾之外就跳过起播前的索引预热；按时间定位前按字节比例估位置、往后找最近的 Cluster 读时间码（至多校正两次）得到目标前最近的一个，再按平均码率估到目标之后探一次得到目标后最近的一个，都登记成 libavformat 的索引项（反向定位落前一个，生产端「不早于目标」的正向定位落后一个；码率按探到的点现算，截断文件的片长不可信）；分片计划的可信判定加「尾部覆盖」：最后一个关键帧离片尾超过 60 秒就退回均匀切分 | 片库 6675 个 MKV 里 8 个没有可用索引（《饥饿站台》下载不完整、Cues 指针在 18.6 GB 而文件只有 11.8 GB；7 个《哆啦A梦》番外是没下完的空壳），边下边播的文件也是这个状态。原来预热线性读 10 秒（上限）放弃，扫到的关键帧只覆盖开头 212 秒却被判可信，最后一段从 205 秒拉到片尾 5933 秒，永远不出画；改后续播到 900 秒 1.2 秒出画，往后跳 600 秒 0.93 秒、往回跳到 300 秒 1.49 秒且落点准确 |

（P6 已并入 P5：按主播放列表名选主片是目录读取协议的一个字段。）

升级上游版本时：先把上游新版原样覆盖 `Sources/AetherEngine`，再逐个重打仍需要的补丁，然后跑语料回归。
