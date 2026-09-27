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
/// - 画图形字幕（PGS 等）：引擎只给出字幕位图与位置，画在哪里由宿主决定，这里按画面矩形摆放；
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
        public let isExternal: Bool
        public let isDefault: Bool
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
    private let subtitleView = BitmapSubtitleView()
    private var cancellables: Set<AnyCancellable> = []
    private var loadTask: Task<Void, Never>?
    private var lastPhase: Phase?
    private var destroyed = false
    /// 图形字幕的时间轴微调（秒，正数 = 延后），与文字字幕同一口径
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

    /// 开发期把引擎日志同步打到控制台（`EngineLog` 默认只进系统日志，模拟器排查时看不到）。
    /// 回调可能来自任意线程，只做线程安全的写入
    public static func mirrorEngineLog(_ enabled: Bool) {
        EngineLog.handler = enabled ? { line in
            FileHandle.standardError.write(Data("[Aether] \(line)\n".utf8))
        } : nil
    }

    /// 日志里要打码的秘密（取流令牌）
    public static func redact(_ secret: String) {
        EngineLog.registerSecret(secret)
    }

    // MARK: - 播放控制

    /// 装载并（按需）起播。start 为源文件时间秒数；headers 附在每一次取源请求上
    /// （App 用它带上自己的 User-Agent，服务端的活动页据此认出「MovieClaw iOS」）
    public func load(url: URL, start: Double?, autoplay: Bool, headers: [String: String] = [:]) {
        loadTask?.cancel()
        lastPhase = nil
        subtitleView.cues = []
        var options = LoadOptions()
        options.autoplay = autoplay
        options.httpHeaders = headers
        let engine = self.engine
        loadTask = Task { [weak self] in
            do {
                try await engine.load(url: url, startPosition: start, options: options)
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

    private static func track(_ info: TrackInfo) -> Track {
        Track(id: info.id, name: info.name, codec: info.codec, language: info.language, isExternal: info.isExternal, isDefault: info.isDefault)
    }

    // MARK: - 画中画 / 生命周期

    /// 主力通路上 AVPlayer 的显示层：宿主用它建画中画控制器（软件通路为 nil）
    public var pictureInPictureLayer: AVPlayerLayer? {
        engine.videoRoute == .software ? nil : engine.nativePlayerLayer
    }

    /// 画中画进出要告诉引擎：画中画期间 App 进后台，引擎不能拆管线
    public func setPictureInPictureActive(_ active: Bool) {
        engine.pictureInPictureActive = active
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
        engine.$playbackPhase
            .sink { [weak self] phase in self?.handle(phase) }
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
                subtitleView.cues = cues.compactMap(BitmapCue.init)
                refreshSubtitles()
            }
            .store(in: &cancellables)
        // 字幕跟着显示中的画面走（sourceTime 约 10 次 / 秒）
        engine.clock.$sourceTime
            .sink { [weak self] _ in self?.refreshSubtitles() }
            .store(in: &cancellables)
    }

    private func handle(_ phase: PlaybackPhase) {
        guard !destroyed else { return }
        switch phase {
        case .idle:
            return
        case .loading, .seeking, .rebuffering, .stalled:
            emit(lastPhase == nil ? .loading : .buffering)
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
        return [PlaybackErrorKind.sourceRateLimited, .vodSourceFailed].contains(info.kind)
    }

    // MARK: - 图形字幕

    private func refreshSubtitles() {
        guard !subtitleView.cues.isEmpty || subtitleView.hasVisibleCues else { return }
        subtitleView.update(time: engine.sourceTime - subtitleDelay, videoRect: videoRect())
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

/// 播放容器：底下是引擎的画面视图，上面叠图形字幕，两者都铺满
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

/// 一条图形字幕（只收位图；文字字幕由 App 的叠加层按用户样式画）
struct BitmapCue {
    let id: Int
    let start: Double
    let end: Double
    let image: CGImage
    /// 在字幕画布里的位置（0～1）
    let position: CGRect
    /// 字幕画布的像素尺寸（.zero = 与画面相同）
    let canvasSize: CGSize

    init?(_ cue: SubtitleCue) {
        guard case let .image(image) = cue.body else { return nil }
        id = cue.id
        start = cue.startTime
        end = cue.endTime
        self.image = image.cgImage
        position = image.position
        canvasSize = image.canvasSize
    }
}

/// 图形字幕层：按当前时间挑出该显示的位图，摆到画面矩形里。
///
/// 摆放规则（与引擎文档一致）：位置是相对字幕画布的 0～1 坐标；画布与画面宽度对齐、垂直居中——
/// 裁过黑边的片子画布比画面高，这样字幕仍落在原盘作者放的位置（包括下黑边里）。
final class BitmapSubtitleView: UIView {
    var cues: [BitmapCue] = []
    private var layers: [Int: CALayer] = [:]
    var hasVisibleCues: Bool { !layers.isEmpty }

    func update(time: Double, videoRect: CGRect) {
        let active = cues.filter { $0.start <= time && time < $0.end }
        let activeIDs = Set(active.map(\.id))
        for (id, layer) in layers where !activeIDs.contains(id) {
            layer.removeFromSuperlayer()
            layers[id] = nil
        }
        guard videoRect.width > 0, videoRect.height > 0 else { return }
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        for cue in active {
            let layer: CALayer
            if let existing = layers[cue.id] {
                layer = existing
            } else {
                layer = CALayer()
                layer.contents = cue.image
                layer.contentsGravity = .resize
                layer.magnificationFilter = .linear
                self.layer.addSublayer(layer)
                layers[cue.id] = layer
            }
            layer.frame = Self.frame(for: cue, in: videoRect)
        }
        CATransaction.commit()
    }

    static func frame(for cue: BitmapCue, in videoRect: CGRect) -> CGRect {
        var canvas = videoRect
        if cue.canvasSize.width > 0, cue.canvasSize.height > 0 {
            let height = videoRect.width * cue.canvasSize.height / cue.canvasSize.width
            canvas = CGRect(x: videoRect.minX, y: videoRect.midY - height / 2, width: videoRect.width, height: height)
        }
        return CGRect(
            x: canvas.minX + cue.position.minX * canvas.width,
            y: canvas.minY + cue.position.minY * canvas.height,
            width: cue.position.width * canvas.width,
            height: cue.position.height * canvas.height
        )
    }
}
