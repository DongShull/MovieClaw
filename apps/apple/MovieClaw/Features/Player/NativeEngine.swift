import AetherCore
import AVKit
import UIKit

/// 自研引擎：「FFmpeg 负责拆，Apple 负责播」（内核是 AetherEngine，经 AetherCore 动态框架引入）。
///
/// 为什么要它（docs/design/player-engine.md）：服务端（NAS）大多性能弱，终端越来越强，所以原文件在本机处理——
/// FFmpeg 读 NAS 上的原文件、就地换封装成 HLS 分片，经本机回环地址交给 AVPlayer。
/// 解码、杜比视界、全景声、HDR 色调映射、画中画都由系统完成：MKV / 原盘也能拿到系统播放器的全部能力，
/// NAS 只吐字节、不起 ffmpeg；也不像 MPV 那样自己上屏（MoltenVK + 着色器，4K HDR 功耗高、发热掉帧）。
/// AVPlayer 解不了的编码（VP9、VC-1、MPEG-2……）由引擎内部换到 FFmpeg 软解 + 系统显示层，对这里透明。
///
/// 与控制器的分工：
/// - 只放原文件直出（服务端档 0 地址）；要服务端转码的情况仍交给系统播放器引擎放 HLS；
/// - 失败时如实上报，由控制器按兜底阶梯回落到 MPV → 服务端 HLS；
/// - 图形字幕（PGS 等）由引擎给出位图、在 AetherCore 里按画面摆放；文字字幕仍走 SwiftUI 叠加层。
@MainActor
final class NativeEngine: NSObject, PlayerEngine {
    let kind = EngineKind.native
    var onEvent: ((EngineEvent) -> Void)?

    /// 正在直出原文件（目前只用于这种情况，诊断面板显示用）
    let playsOriginalFile: Bool

    private let core: AetherPlayback
    private var lastReported: EngineEvent?
    /// 顶栏「↓」的实时加载速度：对引擎从 NAS 累计拉到的字节做差分（口径同 `LoadingSpeedMeter`）
    private var loadingMeter = LoadingSpeedMeter()
    /// 诊断面板「带宽」与 downlink_bps：没有逐请求计时，样本是每秒一个的加载速度读数（同 MPV）
    private var bandwidthMeter = BandwidthMeter()
    private var lastBandwidthSample: TimeInterval?

    /// 轨道列表出来之前就选定的轨：列表出来后补上（音轨要等首帧后再换，起播中途重载会拖慢出画）
    private var pendingAudio: Int?
    private var pendingSubtitle: SubtitleOption?
    private var hasPendingSubtitle = false
    private var tracksKnown = false
    private var firstFrameShown = false

    #if DEBUG
    /// 开发期：状态变化打到控制台并标上距装载的毫秒数（量起播、跳转、换轨耗时）
    private var loadedAt = ContinuousClock.now
    #endif

    private var pipController: AVPictureInPictureController?
    private weak var pipLayer: AVPlayerLayer?
    private(set) var isPictureInPictureActive = false

    init(playsOriginalFile: Bool) throws {
        self.playsOriginalFile = playsOriginalFile
        #if DEBUG
        // 开发期：-mcAetherLog YES 把引擎日志打到控制台（模拟器排查用）
        AetherPlayback.mirrorEngineLog(UserDefaults.standard.bool(forKey: "mcAetherLog"))
        #endif
        core = try AetherPlayback()
        super.init()
        core.onPhase = { [weak self] phase in self?.handle(phase) }
        core.onFailure = { [weak self] failure in self?.handle(failure) }
        core.onTracksChanged = { [weak self] in self?.tracksChanged() }
        core.onFirstFrame = { [weak self] in self?.firstFrameReady() }
    }

    var view: UIView { core.view }

    // MARK: - 播放控制

    func load(url: URL, start: Double, autoplay: Bool) {
        lastReported = nil
        tracksKnown = false
        firstFrameShown = false
        loadingMeter.reset()
        bandwidthMeter.reset()
        lastBandwidthSample = nil
        if let token = PlaybackAPI.token(in: url.absoluteString) { AetherPlayback.redact(token) }
        #if DEBUG
        loadedAt = .now
        #endif
        // 带上 App 的 User-Agent：服务端按它把这条流登记成「MovieClaw iOS」而不是浏览器
        core.load(url: url, start: start > 0.5 ? start : nil, autoplay: autoplay, headers: ["User-Agent": APIClient.userAgent])
        emit(.buffering)
    }

    func play() { core.play() }
    func pause() { core.pause() }

    /// 引擎的定位本身就是精确的（主力通路由 AVPlayer 按帧落点），exact 不区分
    func seek(to seconds: Double, exact: Bool) { core.seek(to: seconds) }

    func setRate(_ rate: Float) { core.setRate(rate) }

    // MARK: - 读数

    var currentTime: Double { core.currentTime }
    var duration: Double? { core.duration }
    var bufferedEnd: Double? { core.bufferedPosition > 0 ? core.bufferedPosition : nil }
    var isPaused: Bool { core.isPaused }
    var videoSize: CGSize { core.videoSize }

    func stats() -> EngineStats {
        let readouts = core.readouts()
        let now = ProcessInfo.processInfo.systemUptime
        let loading = loadingMeter.sample(bytes: readouts.sourceBytesFetched, transferSeconds: 0, at: now)
        if let loading, now - (lastBandwidthSample ?? -.infinity) >= 0.9 {
            lastBandwidthSample = now
            bandwidthMeter.push(bps: loading, at: now)
        }
        var details = readouts.details
        details.append(playsOriginalFile ? "直出原文件" : "播放服务端 HLS")
        if isPictureInPictureActive { details.append("画中画中") }
        let time = currentTime
        return EngineStats(
            engine: kind.rawValue,
            downlinkBps: bandwidthMeter.bps,
            loadingBps: loading,
            bitrateBps: readouts.videoBitrateBps ?? readouts.averageBitrateBps,
            droppedFrames: readouts.droppedFrames,
            // 系统播放器不给总帧数：掉帧比例看门狗在这个引擎上不启用，卡顿看门狗照常
            totalFrames: nil,
            bufferedSeconds: max(0, (bufferedEnd ?? time) - time),
            currentTimeSeconds: time,
            details: details
        )
    }

    // MARK: - 音轨

    /// 引擎在当前位置重载一次即可换轨，不用重开服务端会话
    var canSwitchAudioInPlace: Bool { true }

    func selectAudio(embeddedIndex: Int) {
        guard firstFrameShown else { pendingAudio = embeddedIndex; return }
        let audio = core.audioTracks.filter { !$0.isExternal }.sorted { $0.id < $1.id }
        guard embeddedIndex < audio.count else { return }
        let target = audio[embeddedIndex].id
        if core.activeAudioTrackID != target { core.selectAudioTrack(id: target) }
    }

    // MARK: - 字幕（引擎画图形字幕，文字字幕交给叠加层）

    func rendersSubtitle(kind: String) -> Bool { kind == "pgs" }

    func selectSubtitle(_ option: SubtitleOption?, url: URL?) {
        guard let option else {
            hasPendingSubtitle = false
            pendingSubtitle = nil
            core.clearSubtitle()
            return
        }
        guard tracksKnown else {
            hasPendingSubtitle = true
            pendingSubtitle = option
            return
        }
        // 内封轨按同类型顺序对位（embedded:N = 第 N 条内封字幕，与服务端探测顺序一致）
        if let index = option.embeddedIndex {
            let embedded = core.subtitleTracks.filter { !$0.isExternal }.sorted { $0.id < $1.id }
            if index < embedded.count {
                core.selectSubtitleTrack(id: embedded[index].id)
                return
            }
        }
        core.clearSubtitle()
    }

    func applySubtitleStyle(_ style: SubtitleStyle) {
        core.setSubtitleDelay(style.offsetSeconds)
    }

    // MARK: - 画中画 / 前后台

    var supportsPictureInPicture: Bool {
        AVPictureInPictureController.isPictureInPictureSupported() && core.pictureInPictureLayer != nil
    }

    func togglePictureInPicture() {
        preparePictureInPicture()
        guard let pipController else { return }
        if pipController.isPictureInPictureActive {
            pipController.stopPictureInPicture()
        } else {
            pipController.startPictureInPicture()
        }
    }

    /// 画中画控制器绑在引擎的 AVPlayerLayer 上：换引擎、换会话都不需要，原地进出小窗
    private func preparePictureInPicture() {
        guard AVPictureInPictureController.isPictureInPictureSupported(), let layer = core.pictureInPictureLayer else { return }
        if pipController != nil, pipLayer === layer { return }
        pipController?.delegate = nil
        let controller = AVPictureInPictureController(playerLayer: layer)
        controller?.canStartPictureInPictureAutomaticallyFromInline = true
        controller?.delegate = self
        pipController = controller
        pipLayer = layer
    }

    /// 前后台由引擎自己跟随 App 生命周期处理（后台只留声音、画中画时保持管线），这里无事可做
    func setBackgrounded(_ background: Bool) {}

    func destroy() {
        onEvent = nil
        pipController?.delegate = nil
        pipController = nil
        core.destroy()
    }

    // MARK: - 事件

    private func handle(_ phase: AetherPlayback.Phase) {
        switch phase {
        case .loading, .buffering: emit(.buffering)
        case .playing: emit(.playing)
        case .paused: emit(.paused)
        case .ended: emit(.ended)
        }
    }

    private func handle(_ failure: AetherPlayback.Failure) {
        emit(.failed(reason: "自研引擎无法播放（\(failure.message)）", cause: failure.isNetwork ? .network : .decode))
    }

    private func tracksChanged() {
        tracksKnown = !core.audioTracks.isEmpty || !core.subtitleTracks.isEmpty
        guard tracksKnown, hasPendingSubtitle else { return }
        hasPendingSubtitle = false
        selectSubtitle(pendingSubtitle, url: nil)
    }

    private func firstFrameReady() {
        firstFrameShown = true
        preparePictureInPicture()
        if let pendingAudio {
            self.pendingAudio = nil
            selectAudio(embeddedIndex: pendingAudio)
        }
    }

    private func emit(_ event: EngineEvent) {
        switch (event, lastReported) {
        case (.playing, .playing?), (.paused, .paused?), (.buffering, .buffering?), (.ended, .ended?):
            return
        default:
            lastReported = event
            #if DEBUG
            let elapsed = Int((ContinuousClock.now - loadedAt) / .milliseconds(1))
            FileHandle.standardError.write(Data("[NativeEngine] \(event) 距装载 \(elapsed) 毫秒 t=\(String(format: "%.2f", currentTime))\n".utf8))
            #endif
            onEvent?(event)
        }
    }
}

extension NativeEngine: AVPictureInPictureControllerDelegate {
    nonisolated func pictureInPictureControllerWillStartPictureInPicture(_ controller: AVPictureInPictureController) {
        MainActor.assumeIsolated {
            isPictureInPictureActive = true
            core.setPictureInPictureActive(true)
            onEvent?(.pictureInPicture(true))
        }
    }

    nonisolated func pictureInPictureControllerDidStopPictureInPicture(_ controller: AVPictureInPictureController) {
        MainActor.assumeIsolated {
            isPictureInPictureActive = false
            core.setPictureInPictureActive(false)
            onEvent?(.pictureInPicture(false))
        }
    }

    nonisolated func pictureInPictureController(_ controller: AVPictureInPictureController, failedToStartPictureInPictureWithError error: any Error) {
        MainActor.assumeIsolated {
            isPictureInPictureActive = false
            core.setPictureInPictureActive(false)
        }
    }
}
