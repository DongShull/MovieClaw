import AetherEngine
import AVFoundation
import Combine
import UIKit

/// AetherEngine 的封装，是 App 与这个 LGPL 组件之间**唯一**的边界（同 MPVCore 之于 libmpv）。
///
/// ## 为什么单独做成动态框架（AetherCore.framework）
/// 1. **一个进程里两份 FFmpeg**：mpv 兜底链路把 FFmpeg 静态打在 MPVCore 里，AetherEngine 自带另一份
///    （`AetherLib*` 动态框架）。AetherEngine 调 `avcodec_*` 时绑到哪一份由链接决定，绑错了症状像引擎 bug。
///    收进本框架后，它的 FFmpeg 引用在本框架链接时就绑定到 `AetherLib*`，与 MPVCore 互不相干。
/// 2. **LGPL 替换边界**：用户可以用自己编译的同名框架替换。
/// 因此 App 只 import AetherCore、从不直接 import AetherEngine，这层边界不能破。
///
/// ## 这个引擎做什么
/// 「FFmpeg 负责拆，Apple 负责播」：FFmpeg 读原文件、拆出音视频，就地换封装成 HLS 分片，经本机回环地址
/// 交给 AVPlayer；杜比视界、全景声、HDR、画中画都交给系统。AVPlayer 解不了的编码（VP9、VC-1、MPEG-2……）
/// 由引擎自己换到 FFmpeg 软解 + 系统显示层。设计见 docs/design/player-engine.md。
///
/// ## 本类的职责
/// - 把引擎的多路状态（`playbackPhase` / `errorInfo` / 轨道 / 首帧）归约成 App 关心的几个事件；
/// - 画内封字幕（PGS 等图形字幕、SRT/ASS 等文字字幕）：引擎只给出字幕数据，画在哪里、画成什么样由宿主决定，这里按画面矩形摆放；
/// - 汇总诊断读数（通路、解码器、画面格式、取流字节数）。
/// 时间一律是引擎的**源文件时间轴**（秒）：原文件直出时就是文件时间。
@MainActor
public final class AetherPlayback {
    /// 归约后的播放阶段
    public enum Phase: Equatable, Sendable {
        case loading, playing, paused, buffering, ended
    }

    /// 失败的归因：取流类（断线、令牌失效、服务端拒绝）原地重开就好；其余视为这条通路放不了
    public struct Failure: Sendable {
        public let message: String
        /// 引擎的稳定错误分类（`PlaybackErrorKind.rawValue`），日志与埋点用
        public let kind: String
        public let isNetwork: Bool
    }

    /// 一条音轨 / 字幕轨（id 是容器里的流序号，外挂轨从 100000 起）
    public struct Track: Sendable, Hashable {
        public let id: Int
        public let name: String
        public let codec: String
        public let language: String?
        /// 音轨声道数（2 = 立体声、6 = 5.1、8 = 7.1），字幕为 0
        public let channels: Int
        public let isExternal: Bool
        public let isDefault: Bool
    }

    /// 起播与音频的调校项（默认值即线上值；开发期可用启动参数逐项 A/B，见 NativeEngine）
    public struct Tuning: Sendable, Equatable {
        /// 点播起播时缓冲已够 1.5 秒就不再等 AVPlayer 的码率估计，一次性提前开播（内置引擎补丁 P2）
        public var startsImmediately = true
        /// 无损音轨（TrueHD / DTS-HD 等）本机解码后重编成 FLAC（无损，最多 7.1）而不是 EAC3 5.1
        public var losslessAudio = false
        /// 打开源时的探测预算（字节 / 微秒）；nil 用引擎默认（50 MB / 60 秒）
        public var probeBytes: Int64?
        public var probeMicroseconds: Int64?

        public init() {}
    }

    /// 诊断与看门狗用的读数快照（1 秒刷新一次的引擎遥测 + 当前状态）
    public struct Readouts: Sendable {
        /// 实际在跑的通路：loopback（换封装进 AVPlayer）/ software（软解）/ remoteBypass / none
        public let route: String
        /// 从源（NAS）累计拉到的字节，算加载速度与带宽用
        public let sourceBytesFetched: Int64?
        public let droppedFrames: Int?
        public let averageBitrateBps: Double?
        public let videoBitrateBps: Double?
        /// 给人看的细节行（解码器、画面格式、音频交付方式……）
        public let details: [String]
    }

    public let view: UIView
    public var onPhase: ((Phase) -> Void)?
    public var onFailure: ((Failure) -> Void)?
    /// 轨道列表变化（装载完成、外挂轨注册）
    public var onTracksChanged: (() -> Void)?
    /// 第一帧可以上屏
    public var onFirstFrame: (() -> Void)?

    private let engine: AetherEngine
    private let playerView = AetherPlayerView()
    private let subtitleView = SubtitleLayerView()
    private var cancellables: Set<AnyCancellable> = []
    private var loadTask: Task<Void, Never>?
    private var lastPhase: Phase?
    private var destroyed = false
    /// 字幕的时间轴微调（秒，正数 = 延后），与 App 叠加层同一口径
    private var subtitleDelay: Double = 0

    public init() throws {
        engine = try AetherEngine()
        let container = PlaybackContainerView(playerView: playerView, subtitleView: subtitleView)
        view = container
        engine.bind(view: playerView)
        engine.videoGravity = .resizeAspect
        // 后台继续出声音（与系统播放器一致）；画中画时引擎保持管线不拆
        engine.backgroundPlaybackEnabled = true
        container.onLayout = { [weak self] in self?.refreshSubtitles() }
        observe()
    }

    /// 开发期：字幕列表变化时把文字字幕连同 ASS 定位打到控制台（排查字幕摆放用）
    nonisolated(unsafe) public static var logsCues = false

    /// 开发期把引擎日志同步打到控制台（`EngineLog` 默认只进系统日志，模拟器排查时看不到）。
    /// 每行前面带开机以来的秒数（与 App 侧 `[NativeEngine]` 行同一时钟），拆起播各段耗时用。
    /// 回调可能来自任意线程，只做线程安全的写入
    public static func mirrorEngineLog(_ enabled: Bool) {
        EngineLog.handler = enabled ? { line in
            let stamp = String(format: "%.3f", ProcessInfo.processInfo.systemUptime)
            FileHandle.standardError.write(Data("[Aether \(stamp)] \(line)\n".utf8))
        } : nil
    }

    /// 日志里要打码的秘密（取流令牌）
    public static func redact(_ secret: String) {
        EngineLog.registerSecret(secret)
    }

    // MARK: - 播放控制

    /// 原盘目录里的一个文件（相对原盘根目录的路径、字节数、按 Range 取字节的地址）
    public struct DiscFile: Sendable {
        public let path: String
        public let size: Int64
        public let url: URL

        public init(path: String, size: Int64, url: URL) {
            self.path = path
            self.size = size
            self.url = url
        }
    }

    /// 装载什么（docs/design/disc-direct-play.md）：光盘结构都在本机解析，服务端只按 Range 供字节
    public enum Source: Sendable {
        /// 普通媒体文件
        case file(URL)
        /// 光盘镜像（蓝光 UDF / DVD ISO9660）的原字节：地址没有 .iso 后缀，靠片段标记告诉引擎按镜像读
        case discImage(URL)
        /// 原盘目录（BDMV）：目录清单 + 服务端选中的主播放列表名，引擎按剪辑逐个文件取字节并拼接
        case discFolder(files: [DiscFile], playlist: String?)
    }

    /// 装载并（按需）起播。start 为源文件时间秒数；headers 附在每一次取源请求上
    /// （App 用它带上自己的 User-Agent，服务端的活动页据此认出「MovieClaw iOS」）
    public func load(url: URL, start: Double?, autoplay: Bool, headers: [String: String] = [:], tuning: Tuning = Tuning()) {
        load(source: .file(url), start: start, autoplay: autoplay, headers: headers, tuning: tuning)
    }

    /// 外挂字幕文件：装载时交给引擎，由引擎下载、解码、画（ASS 的定位照样生效），画中画时也能换成原生字幕轨
    public struct ExternalSubtitle: Sendable, Equatable {
        public let url: URL
        public let language: String?
        /// 文件格式（srt / ass / ssa / vtt）：取字幕的地址不带扩展名，要明说
        public let format: String

        public init(url: URL, language: String?, format: String) {
            self.url = url
            self.language = language
            self.format = format
        }
    }

    /// audioOrdinal：起播就放第几条音轨（容器里音轨的顺序，从 0 数；内置引擎补丁 P11）。nil = 引擎自己挑。
    /// externalSubtitles：外挂字幕按给出的顺序登记，`subtitleTracks` 里 isExternal 的轨按 id 排序与之一一对应
    public func load(source: Source, start: Double?, autoplay: Bool, headers: [String: String] = [:], tuning: Tuning = Tuning(),
                     audioOrdinal: Int? = nil, externalSubtitles: [ExternalSubtitle] = []) {
        loadTask?.cancel()
        lastPhase = nil
        subtitleView.cues = []
        var options = LoadOptions()
        options.autoplay = autoplay
        options.httpHeaders = headers
        options.vodStartsImmediately = tuning.startsImmediately
        options.audioBridgeMode = tuning.losslessAudio ? .lossless : .surroundCompat
        options.probesize = tuning.probeBytes
        options.maxAnalyzeDuration = tuning.probeMicroseconds
        options.audioTrackOrdinal = audioOrdinal
        options.externalSubtitles = externalSubtitles.map {
            ExternalSubtitleTrack(url: $0.url, language: $0.language, formatHint: $0.format)
        }
        // 内封文字字幕同时声明成原生字幕轨：平时不选（App 自己的字幕层画），画中画时换 AVPlayer 画（见 setPictureInPictureActive）。
        // 读字幕的旁路只在选中原生轨时才跑，平时不花读取与解码
        options.prepareNativeSubtitles = true
        let engine = self.engine
        let mediaSource: MediaSource
        switch source {
        case let .file(url):
            mediaSource = .url(url)
        case let .discImage(url):
            var components = URLComponents(url: url, resolvingAgainstBaseURL: false)
            components?.fragment = Demuxer.discImageFragment
            mediaSource = .url(components?.url ?? url)
        case let .discFolder(files, playlist):
            let reader = HTTPDiscDirectoryReader(
                files: files.map { HTTPDiscDirectoryReader.File(path: $0.path, size: $0.size, url: $0.url) },
                preferredPlaylist: playlist,
                httpHeaders: headers
            )
            mediaSource = .custom(reader, formatHint: nil)
        }
        loadTask = Task { [weak self] in
            do {
                try await engine.load(source: mediaSource, startPosition: start, options: options)
            } catch is CancellationError {
                // 被新的装载 / 停止取代：不是播放失败
            } catch {
                // 装载失败时引擎同时发布 .error 状态，由状态订阅统一上报；这里兜住没有发布状态的情况
                guard let self, !self.destroyed, !Task.isCancelled else { return }
                if case .error = engine.playbackPhase { return }
                self.report(Failure(message: error.localizedDescription, kind: "loadThrew", isNetwork: false))
            }
        }
    }

    public func play() { engine.play() }
    public func pause() { engine.pause() }

    public func seek(to seconds: Double) {
        let engine = self.engine
        Task { await engine.seek(to: max(0, seconds)) }
    }

    public func setRate(_ rate: Float) { engine.setRate(rate) }

    // MARK: - 读数

    public var currentTime: Double { engine.currentTime }
    public var duration: Double? { engine.duration > 0 ? engine.duration : nil }
    /// 已缓冲到的源文件时间
    public var bufferedPosition: Double { engine.bufferedPosition }
    public var isPaused: Bool {
        switch engine.state {
        case .paused, .idle, .ended: true
        default: false
        }
    }

    /// 画面显示尺寸（已计像素宽高比）；还不知道时为 .zero
    public var videoSize: CGSize {
        if let size = engine.softwareDisplaySize { return size }
        let width = Double(engine.sourceVideoWidth) * engine.sourceVideoPixelAspectRatio
        let height = Double(engine.sourceVideoHeight)
        return width > 0 && height > 0 ? CGSize(width: width, height: height) : .zero
    }

    public func readouts() -> Readouts {
        let telemetry = engine.liveTelemetry
        var details: [String] = []
        details.append("通路 \(Self.routeLabel(engine.videoRoute))")
        if let container = engine.sourceContainerFormat { details.append("容器 \(container)") }
        if let decoder = engine.activeVideoDecoder { details.append("视频 \(decoder)") }
        details.append("画面 \(formatLabel)")
        if let decoder = engine.activeAudioDecoder { details.append("音频 \(decoder)") }
        details.append("音频交付 \(Self.deliveryLabel(engine.audioDelivery))")
        return Readouts(
            route: engine.videoRoute.rawValue,
            sourceBytesFetched: telemetry?.demuxerBytesFetched,
            droppedFrames: telemetry?.droppedFrameCount,
            averageBitrateBps: telemetry?.averageBitrateMbps.map { $0 * 1_000_000 },
            videoBitrateBps: engine.sourceVideoBitrate > 0 ? Double(engine.sourceVideoBitrate) : nil,
            details: details
        )
    }

    private var formatLabel: String {
        var label = switch engine.videoFormat {
        case .sdr: "SDR"
        case .hdr10: "HDR10"
        case .hdr10Plus: "HDR10+"
        case .dolbyVision: "杜比视界"
        case .hlg: "HLG"
        }
        if let profile = engine.sourceDVProfile {
            label += " · 片源 DV P\(profile)"
            if engine.dolbyVisionConversion == .profile7ToProfile81 { label += "（已转 8.1）" }
        }
        return label
    }

    private static func routeLabel(_ route: VideoRoute) -> String {
        switch route {
        case .loopback: "本机换封装 → AVPlayer"
        case .software: "本机软解 → 系统显示层"
        case .remoteBypass: "AVPlayer 直连"
        case .audio: "纯音频"
        case .none: "未装载"
        }
    }

    private static func deliveryLabel(_ delivery: AudioDelivery) -> String {
        switch delivery {
        case .streamCopy: "原样拷贝"
        case .bridged: "本机解码后重编"
        case .decoded: "本机解码"
        case .playerManaged: "系统播放器处理"
        case .noAudioInSource: "片源无音轨"
        case .droppedNoPipeline: "无法输出（静音）"
        case .none: "—"
        }
    }

    // MARK: - 轨道

    public var audioTracks: [Track] { engine.audioTracks.map(Self.track) }
    public var subtitleTracks: [Track] { engine.subtitleTracks.map(Self.track) }
    public var activeAudioTrackID: Int? { engine.activeAudioTrackIndex }

    /// 换音轨：引擎在当前位置重载一次（约 0.5～1 秒黑屏）
    public func selectAudioTrack(id: Int) { engine.selectAudioTrack(index: id) }

    public func selectSubtitleTrack(id: Int) { engine.selectSubtitleTrack(index: id) }

    public func clearSubtitle() {
        engine.clearSubtitle()
        subtitleView.cues = []
        refreshSubtitles()
    }

    public func setSubtitleDelay(_ seconds: Double) {
        subtitleDelay = seconds
        refreshSubtitles()
    }

    /// 文字字幕的样式（与 App 的字幕设置同一口径：字号、底边距都是画面高度的百分比）
    public struct TextStyle: Sendable, Equatable {
        public var fontScale: Double = 5.2
        public var bottomPercent: Double = 8
        public var outline = true
        public var background = false

        public init(fontScale: Double = 5.2, bottomPercent: Double = 8, outline: Bool = true, background: Bool = false) {
            self.fontScale = fontScale
            self.bottomPercent = bottomPercent
            self.outline = outline
            self.background = background
        }
    }

    public func setTextStyle(_ style: TextStyle) {
        subtitleView.textStyle = style
        refreshSubtitles()
    }

    private static func track(_ info: TrackInfo) -> Track {
        Track(id: info.id, name: info.name, codec: info.codec, language: info.language, channels: info.channels,
              isExternal: info.isExternal, isDefault: info.isDefault)
    }

    // MARK: - 画中画 / 生命周期

    /// 主力通路上 AVPlayer 的显示层：宿主用它建画中画控制器（软件通路为 nil）
    public var pictureInPictureLayer: AVPlayerLayer? {
        engine.videoRoute == .software ? nil : engine.nativePlayerLayer
    }

    /// 软件通路（VP9 / MPEG-2 / VC-1 / MPEG-4 由 FFmpeg 软解、系统显示层上屏）的画中画：宿主用它建
    /// 「采样缓冲」式画中画控制器。主力通路为 nil（那边用 `pictureInPictureLayer`）
    public var softwarePictureInPicture: SoftwarePictureInPicture? {
        guard engine.videoRoute == .software, let source = engine.softwarePiPSource else { return nil }
        return SoftwarePictureInPicture(source: source)
    }

    /// 软件通路画中画要的全部东西：显示层，加上小窗向播放方要的四个回答（可播范围、是否暂停、播放/暂停、快进快退）。
    /// 时间都在显示层所挂时钟的「源时间轴」上，这是引擎内部知识，所以由引擎回答
    @MainActor
    public struct SoftwarePictureInPicture {
        fileprivate let source: SoftwarePiPSource
        public var layer: AVSampleBufferDisplayLayer { source.layer }
        public func timeRange() -> CMTimeRange { source.timeRange() }
        public var isPaused: Bool { source.isPaused }
        public func setPlaying(_ playing: Bool) { source.setPlaying(playing) }
        public func skip(by seconds: Double) { source.skip(by: seconds) }
    }

    /// 清掉被杀掉的播放会话留在临时目录里的分片与包缓存（每次 App 启动调一次即可，放后台线程）
    public nonisolated static func sweepStaleCaches() {
        AetherEngine.sweepStaleSessionCaches()
    }

    /// 画中画进出要告诉引擎：画中画期间 App 进后台，引擎不能拆管线。
    /// 字幕也跟着换人画：画面进了小窗，App 自己的字幕层画不到那里——主力通路上把当前的内封文字字幕
    /// 换成 AVPlayer 自己渲染的原生字幕轨（装载时 `prepareNativeSubtitles` 声明好的），回到全屏再撤掉；
    /// 软件通路由引擎把字幕合成进小窗的画面（引擎在 `pictureInPictureActive` 里自己处理）
    public func setPictureInPictureActive(_ active: Bool) {
        engine.pictureInPictureActive = active
        if engine.videoRoute != .software {
            engine.setNativeSubtitleRendering(active)
        }
        // 画面在小窗里时，App 里原位置只剩系统的「此视频正以画中画播放」占位：自己的字幕层不能还画在上面
        subtitleView.isHidden = active
    }

    public func destroy() {
        destroyed = true
        onPhase = nil
        onFailure = nil
        onTracksChanged = nil
        onFirstFrame = nil
        loadTask?.cancel()
        cancellables.removeAll()
        engine.stop()
        engine.unbind(view: playerView)
    }

    // MARK: - 事件

    private func observe() {
        // 阶段之外还要看传输实况（isBuffering / state）：`.stalled` 只说明源连接在重连，播没播要看传输（见 handle）。
        // @Published 在写入前发出，combineLatest 拿到的是各自的新值
        engine.$playbackPhase.combineLatest(engine.$isBuffering, engine.$state)
            .sink { [weak self] phase, buffering, state in self?.handle(phase, transportStarved: buffering, transport: state) }
            .store(in: &cancellables)
        engine.$audioTracks.combineLatest(engine.$subtitleTracks)
            .dropFirst()
            .sink { [weak self] _ in
                // @Published 在属性真正写入之前发出：等这一轮写完再读
                Task { @MainActor [weak self] in self?.onTracksChanged?() }
            }
            .store(in: &cancellables)
        engine.$hasFirstFrameReadyForDisplay
            .removeDuplicates()
            .filter { $0 }
            .sink { [weak self] _ in self?.onFirstFrame?() }
            .store(in: &cancellables)
        engine.$subtitleCues
            .sink { [weak self] cues in
                guard let self else { return }
                subtitleView.cues = cues.compactMap(OverlayCue.init)
                if Self.logsCues {
                    for cue in cues where cue.text != nil {
                        let place = cue.placement.map { "an=\($0.alignment.map(String.init) ?? "-") pos=\($0.position.map { "\(String(format: "%.2f", $0.x)),\(String(format: "%.2f", $0.y))" } ?? "-")" } ?? "无定位"
                        FileHandle.standardError.write(Data("[AetherCue] \(String(format: "%.2f", cue.startTime))-\(String(format: "%.2f", cue.endTime)) \(place) \(cue.text ?? "")\n".utf8))
                    }
                    // 位图字幕（PGS / VobSub / DVB）没有文字：记下有几张图、画布多大，确认它确实上屏了
                    let bitmaps = cues.filter { $0.text == nil }
                    if let first = bitmaps.first {
                        FileHandle.standardError.write(Data("[AetherCue] \(String(format: "%.2f", first.startTime))-\(String(format: "%.2f", first.endTime)) 位图字幕 \(bitmaps.count) 张\n".utf8))
                    }
                }
                refreshSubtitles()
            }
            .store(in: &cancellables)
        // 字幕跟着显示中的画面走（sourceTime 约 10 次 / 秒）
        engine.clock.$sourceTime
            .sink { [weak self] _ in self?.refreshSubtitles() }
            .store(in: &cancellables)
    }

    private func handle(_ phase: PlaybackPhase, transportStarved: Bool, transport: PlaybackState) {
        guard !destroyed else { return }
        switch phase {
        case .idle:
            return
        case .loading, .seeking, .rebuffering:
            emit(lastPhase == nil ? .loading : .buffering)
        case .stalled:
            // 源连接断了、读取端在重连（或重连次数用完、等生产端重开）。本地已经切好的分片还够播时画面照常在走，
            // 这时报缓冲会让转圈一直盖着正在播的画面（2026-09-27 真机断流演练：服务端停机 21 秒，播放头一秒没停，
            // 转圈转了 21 秒）。只有传输真的吃光了缓冲、或还在起播 / 定位时才报缓冲，其余照传输实况报播放 / 暂停；
            // 缓冲吃光后迟迟不恢复，由控制器的卡顿看门狗判「断粮」原地重开
            if lastPhase == nil || transportStarved || transport == .loading || transport == .seeking {
                emit(lastPhase == nil ? .loading : .buffering)
            } else {
                emit(transport == .paused ? .paused : .playing)
            }
        case .playing:
            emit(.playing)
        case .paused:
            emit(.paused)
        case .ended:
            emit(.ended)
        case let .error(message):
            let info = engine.errorInfo
            report(Failure(message: message, kind: info?.kind.rawValue ?? "unknown", isNetwork: Self.isNetwork(info)))
        }
    }

    private func emit(_ phase: Phase) {
        guard phase != lastPhase else { return }
        lastPhase = phase
        onPhase?(phase)
    }

    private func report(_ failure: Failure) {
        onFailure?(failure)
    }

    /// 取流类失败：源拒绝（令牌过期、5xx）、限流、VOD 源中途断掉——换张新令牌原地重开可能就好了。
    /// 404 不算：文件不在或服务端不提供这种直出（例如 ISO），重开多少次都一样，该直接走兜底
    private static func isNetwork(_ info: PlaybackErrorInfo?) -> Bool {
        guard let info else { return false }
        if info.kind == .sourceRefused { return info.underlyingCode != 404 }
        // vodSourceFailed 带 -22（EINVAL）是「源音频封装不进 fMP4」，不是断线：重开多少次都一样
        if info.kind == .vodSourceFailed { return info.underlyingCode != -22 }
        return info.kind == .sourceRateLimited
    }

    // MARK: - 字幕

    private func refreshSubtitles() {
        guard !subtitleView.cues.isEmpty || subtitleView.hasVisibleCues else { return }
        subtitleView.update(time: subtitleClock - subtitleDelay, videoRect: videoRect())
    }

    /// 字幕对的是哪根时间轴：内封字幕的时间是容器的源时间轴（TS / 蓝光的 PTS 从几百几千秒起），
    /// 外挂字幕文件的时间从 0 起、对的是片内时间。TS 录像实测源时间轴从 19123 秒起，
    /// 外挂字幕按源时钟比永远对不上、一条都不出
    private var subtitleClock: Double {
        if let active = engine.activeSubtitleTrackIndex,
           engine.subtitleTracks.contains(where: { $0.id == active && $0.isExternal }) {
            return engine.currentTime
        }
        return engine.sourceTime
    }

    /// 画面在容器里的矩形：主力通路直接问 AVPlayerLayer，软件通路按显示尺寸 aspect-fit
    private func videoRect() -> CGRect {
        let bounds = view.bounds
        if engine.videoRoute != .software, let layer = engine.nativePlayerLayer {
            let rect = layer.videoRect
            if rect.width > 1, rect.height > 1 {
                return view.layer.convert(rect, from: layer)
            }
        }
        let size = videoSize
        guard size.width > 0, size.height > 0, bounds.width > 0, bounds.height > 0 else { return bounds }
        let scale = min(bounds.width / size.width, bounds.height / size.height)
        let fitted = CGSize(width: size.width * scale, height: size.height * scale)
        return CGRect(x: (bounds.width - fitted.width) / 2, y: (bounds.height - fitted.height) / 2, width: fitted.width, height: fitted.height)
    }
}

/// 播放容器：底下是引擎的画面视图，上面叠字幕层，两者都铺满
private final class PlaybackContainerView: UIView {
    var onLayout: (() -> Void)?

    init(playerView: UIView, subtitleView: UIView) {
        super.init(frame: .zero)
        backgroundColor = .black
        for child in [playerView, subtitleView] {
            child.frame = bounds
            child.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            addSubview(child)
        }
        subtitleView.isUserInteractionEnabled = false
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func layoutSubviews() {
        super.layoutSubviews()
        onLayout?()
    }
}

/// 一条要画的字幕：图形字幕是位图 + 位置，文字字幕是纯文本（ASS 样式先按纯文本画，字号、位置、描边随用户设置）
struct OverlayCue {
    enum Content {
        /// 位图、在字幕画布里的位置（0～1）、画布像素尺寸（.zero = 与画面相同）
        case image(CGImage, position: CGRect, canvas: CGSize)
        /// 文字与 ASS 指定的位置：alignment 是 `\an` 小键盘方位（1 左下 … 9 右上），anchor 是 `\pos`（0～1，y 从上往下）
        case text(String, alignment: Int?, anchor: CGPoint?)
    }

    let id: Int
    let start: Double
    let end: Double
    let content: Content

    init?(_ cue: SubtitleCue) {
        id = cue.id
        start = cue.startTime
        end = cue.endTime
        switch cue.body {
        case let .image(image):
            content = .image(image.cgImage, position: image.position, canvas: image.canvasSize)
        case let .text(text):
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { return nil }
            content = .text(trimmed, alignment: cue.placement?.alignment, anchor: cue.placement?.position)
        case let .richText(runs):
            let trimmed = runs.map(\.text).joined().trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { return nil }
            content = .text(trimmed, alignment: cue.placement?.alignment, anchor: cue.placement?.position)
        }
    }
}

/// 字幕层：按当前时间挑出该显示的字幕，摆到画面矩形里。
///
/// - 图形字幕：位置是相对字幕画布的 0～1 坐标；画布与画面宽度对齐、垂直居中——裁过黑边的片子画布比画面高，
///   这样字幕仍落在原盘作者放的位置（包括下黑边里）。
/// - 文字字幕：与 App 的 SwiftUI 叠加层（SubtitleOverlay）同一口径——字号是画面高度的百分比、
///   位置是距画面底边的百分比，白字加描边或半透明底框，横竖屏切换都不影响字幕相对画面的样子。
///   ASS 用 `\pos` 指定了位置的（招牌、注释、竖排说明这类特效字）画在它指定的位置，`\an7～9` 的画在顶部，
///   其余对白合成一块放在底部——不然特效字会叠进对白里（真机《如果历史是一群喵》实测）。
final class SubtitleLayerView: UIView {
    var cues: [OverlayCue] = []
    var textStyle = AetherPlayback.TextStyle()
    private var imageLayers: [Int: CALayer] = [:]
    private let bottomBlock = TextBlockView()
    private let topBlock = TextBlockView()
    private var anchoredBlocks: [Int: TextBlockView] = [:]

    var hasVisibleCues: Bool { !imageLayers.isEmpty || !bottomBlock.isHidden || !topBlock.isHidden || !anchoredBlocks.isEmpty }

    override init(frame: CGRect) {
        super.init(frame: frame)
        addSubview(bottomBlock)
        addSubview(topBlock)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    /// 开发期（-mcAetherCues YES）：上一次记下的上屏字幕，变了才记一笔
    private var loggedActiveIDs: [Int] = []

    func update(time: Double, videoRect: CGRect) {
        let active = cues.filter { $0.start <= time && time < $0.end }
        if AetherPlayback.logsCues, active.map(\.id) != loggedActiveIDs {
            // 真机无人值守验证时确认字幕确实画上了屏（[AetherCue] 只说明拿到了字幕）
            loggedActiveIDs = active.map(\.id)
            let shown = active.isEmpty ? "清空" : active.map { cue -> String in
                if case let .text(text, _, _) = cue.content { return "「\(text.prefix(24))」" }
                return "位图"
            }.joined(separator: " ")
            FileHandle.standardError.write(Data("[AetherShow] \(String(format: "%.2f", time)) \(shown)\n".utf8))
        }
        updateImages(active, videoRect: videoRect)
        var bottom: [String] = [], top: [String] = []
        var anchored: [(id: Int, text: String, alignment: Int, anchor: CGPoint)] = []
        for cue in active {
            guard case let .text(text, alignment, anchor) = cue.content else { continue }
            if let anchor, anchor.x.isFinite, anchor.y.isFinite {
                anchored.append((cue.id, text, alignment ?? 2, anchor))
            } else if let alignment, (7 ... 9).contains(alignment) {
                top.append(text)
            } else {
                bottom.append(text)
            }
        }
        guard videoRect.width > 0, videoRect.height > 0 else {
            bottomBlock.isHidden = true
            topBlock.isHidden = true
            anchoredBlocks.values.forEach { $0.removeFromSuperview() }
            anchoredBlocks = [:]
            return
        }
        let fontSize = max(12, videoRect.height * textStyle.fontScale / 100)
        let margin = videoRect.height * textStyle.bottomPercent / 100 + fontSize
        // 与 SwiftUI 叠加层一致：底部块的中心在「画面底边往上 bottomPercent，再上移一个字号」处
        bottomBlock.show(Array(bottom.prefix(3)).joined(separator: "\n"), style: textStyle, fontSize: fontSize,
                         maxWidth: videoRect.width * 0.9, anchor: CGPoint(x: videoRect.midX, y: videoRect.maxY - margin), alignment: 5)
        topBlock.show(Array(top.prefix(3)).joined(separator: "\n"), style: textStyle, fontSize: fontSize,
                      maxWidth: videoRect.width * 0.9, anchor: CGPoint(x: videoRect.midX, y: videoRect.minY + margin), alignment: 5)
        let anchoredIDs = Set(anchored.map(\.id))
        for (id, block) in anchoredBlocks where !anchoredIDs.contains(id) {
            block.removeFromSuperview()
            anchoredBlocks[id] = nil
        }
        for cue in anchored {
            let block = anchoredBlocks[cue.id] ?? {
                let created = TextBlockView()
                addSubview(created)
                anchoredBlocks[cue.id] = created
                return created
            }()
            // 特效字比对白小一号：它们多是画面上的注释，不该和对白抢
            let point = CGPoint(x: videoRect.minX + min(max(cue.anchor.x, 0), 1) * videoRect.width,
                                y: videoRect.minY + min(max(cue.anchor.y, 0), 1) * videoRect.height)
            block.show(cue.text, style: textStyle, fontSize: fontSize * 0.8, maxWidth: videoRect.width * 0.6,
                       anchor: point, alignment: cue.alignment)
        }
    }

    private func updateImages(_ active: [OverlayCue], videoRect: CGRect) {
        let activeIDs = Set(active.compactMap { cue -> Int? in
            if case .image = cue.content { return cue.id }
            return nil
        })
        for (id, layer) in imageLayers where !activeIDs.contains(id) {
            layer.removeFromSuperlayer()
            imageLayers[id] = nil
        }
        guard videoRect.width > 0, videoRect.height > 0 else { return }
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        for cue in active {
            guard case let .image(image, position, canvas) = cue.content else { continue }
            let layer: CALayer
            if let existing = imageLayers[cue.id] {
                layer = existing
            } else {
                layer = CALayer()
                layer.contents = image
                layer.contentsGravity = .resize
                layer.magnificationFilter = .linear
                self.layer.addSublayer(layer)
                imageLayers[cue.id] = layer
            }
            layer.frame = Self.frame(position: position, canvas: canvas, in: videoRect)
        }
        CATransaction.commit()
    }

    static func frame(position: CGRect, canvas: CGSize, in videoRect: CGRect) -> CGRect {
        var rect = videoRect
        if canvas.width > 0, canvas.height > 0 {
            let height = videoRect.width * canvas.height / canvas.width
            rect = CGRect(x: videoRect.minX, y: videoRect.midY - height / 2, width: videoRect.width, height: height)
        }
        return CGRect(
            x: rect.minX + position.minX * rect.width,
            y: rect.minY + position.minY * rect.height,
            width: position.width * rect.width,
            height: position.height * rect.height
        )
    }
}

/// 一块字幕文字：白字（描边或半透明底框），按 ASS 小键盘方位把自己对齐到锚点（5 = 锚点在中心）。
/// 文字、样式、位置都没变时不重排（字幕层约每秒刷新 10 次）
final class TextBlockView: UIView {
    private let label = UILabel()
    private var last: (String, AetherPlayback.TextStyle, CGFloat, CGFloat, CGPoint, Int)?

    override init(frame: CGRect) {
        super.init(frame: frame)
        isHidden = true
        isUserInteractionEnabled = false
        layer.cornerCurve = .continuous
        label.numberOfLines = 0
        label.textAlignment = .center
        addSubview(label)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func show(_ text: String, style: AetherPlayback.TextStyle, fontSize: CGFloat, maxWidth: CGFloat, anchor: CGPoint, alignment: Int) {
        guard !text.isEmpty else {
            isHidden = true
            last = nil
            return
        }
        if let last, last.0 == text, last.1 == style, last.2 == fontSize, last.3 == maxWidth, last.4 == anchor, last.5 == alignment { return }
        last = (text, style, fontSize, maxWidth, anchor, alignment)
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = .center
        paragraph.lineSpacing = fontSize * 0.1
        var attributes: [NSAttributedString.Key: Any] = [
            .font: UIFont.systemFont(ofSize: fontSize, weight: .medium),
            .foregroundColor: UIColor.white,
            .paragraphStyle: paragraph,
        ]
        if style.outline, !style.background {
            // 描边：负的描边宽度 = 描边同时保留填充；再加一层柔和阴影压住亮背景
            attributes[.strokeColor] = UIColor.black
            attributes[.strokeWidth] = -3.0
            let shadow = NSShadow()
            shadow.shadowColor = UIColor.black.withAlphaComponent(0.6)
            shadow.shadowBlurRadius = 3
            shadow.shadowOffset = .zero
            attributes[.shadow] = shadow
        }
        label.attributedText = NSAttributedString(string: text, attributes: attributes)
        let padH = style.background ? fontSize * 0.35 : 0
        let padV = style.background ? fontSize * 0.12 : 0
        let fitted = label.sizeThatFits(CGSize(width: maxWidth - padH * 2, height: .greatestFiniteMagnitude))
        let size = CGSize(width: min(maxWidth, ceil(fitted.width) + padH * 2), height: ceil(fitted.height) + padV * 2)
        // 小键盘方位：列 1/4/7 左、2/5/8 中、3/6/9 右；行 1-3 底、4-6 中、7-9 顶
        let column = (alignment - 1) % 3, row = (alignment - 1) / 3
        let x = anchor.x - size.width * [0, 0.5, 1][max(0, min(2, column))]
        let y = anchor.y - size.height * [1, 0.5, 0][max(0, min(2, row))]
        frame = CGRect(x: x, y: y, width: size.width, height: size.height)
        label.frame = bounds.insetBy(dx: padH, dy: padV)
        backgroundColor = style.background ? UIColor.black.withAlphaComponent(0.6) : .clear
        layer.cornerRadius = style.background ? fontSize * 0.2 : 0
        isHidden = false
    }
}
