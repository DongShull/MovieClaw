import AetherCore
import SwiftUI

/// 刷片的一条：一个自研引擎实例，从原片中间的 `segment.start_ms` 起播，放到 `end_ms` 停下
/// （docs/design/reels.md §6）。
///
/// 为什么不复用播放器页的 `PlaybackController` / `NativeEngine`：
/// - 刷片**不写观看记录**：进度上报、续播点、播放次数都在 PlaybackController 里，刷片一条都不能碰；
/// - 不需要会话心跳、画中画、锁屏信息、换画质建议这些正片才要的东西；
/// - 取流地址由刷片接口直接给出（按文件签发的令牌），不用每条开一次播放会话。
///
/// 片源字节缓存的键与播放器页同一口径（`PlaybackController.sourceCacheKey`，文件 id + 大小）：
/// 刷片预取和播放下过的字节，点「接着看」转到播放器页后直接复用，起播不用重下。
@MainActor
final class ReelPlayer {
    enum State: Equatable {
        case loading, playing, paused, ended
        case failed(String)

        var isFailed: Bool {
            if case .failed = self { return true }
            return false
        }
    }

    let item: API.ReelItemView
    let core: AetherPlayback
    private(set) var state: State = .loading {
        didSet { if state != oldValue { onStateChange?(state) } }
    }
    var onStateChange: ((State) -> Void)?
    var onFirstFrame: (() -> Void)?
    private(set) var hasFirstFrame = false
    /// 预起中：装载到起点停着，等 `play()`（见 `start(server:autoplay:)`）
    private var prerolling = false
    private var subtitleApplied = false
    private var monitor: Task<Void, Never>?

    var startSeconds: Double { Double(item.segment.startMs) / 1000 }
    var endSeconds: Double { Double(item.segment.endMs) / 1000 }
    /// 当前在原片上的位置（秒）；还没出画面时按起点算
    var position: Double { hasFirstFrame ? max(core.currentTime, startSeconds) : startSeconds }
    /// 片段内的进度 0～1
    var progress: Double {
        let span = endSeconds - startSeconds
        guard span > 0 else { return 0 }
        return min(1, max(0, (position - startSeconds) / span))
    }

    init(item: API.ReelItemView) throws {
        NativeEngine.prepareEngineEnvironment()
        core = try AetherPlayback()
        NativeEngine.sweepStaleCachesOnce()
        self.item = item
        setFullscreen(false)
        core.onPhase = { [weak self] phase in self?.handle(phase) }
        core.onFailure = { [weak self] failure in self?.state = .failed(failure.message) }
        core.onTracksChanged = { [weak self] in self?.applySubtitle() }
        core.onFirstFrame = { [weak self] in
            guard let self, !self.hasFirstFrame else { return }
            self.hasFirstFrame = true
            self.onFirstFrame?()
        }
    }

    /// 片源字节缓存的键；刷片预取与播放器页都用它认同一个文件
    static func cacheKey(for item: API.ReelItemView) -> String? {
        item.play.sizeBytes.map { PlaybackController.sourceCacheKey(fileId: item.segment.fileId, size: $0) }
    }

    /// - Parameter autoplay: false = 预起（装载到起点、停在第一帧，等 `play()`）
    func start(server: ServerAddress, autoplay: Bool = true) {
        guard let raw = item.play.streamUrl, let url = server.resolve(raw) else {
            state = .failed("这一条缺少取流地址")
            return
        }
        prerolling = !autoplay
        core.load(source: .file(url), start: startSeconds, autoplay: autoplay,
                  headers: ["User-Agent": APIClient.userAgent],
                  audioOrdinal: item.play.audioOrdinal,
                  sourceCacheKey: Self.cacheKey(for: item))
        // 到终点就停：引擎没有「放到某处停」的接口，四分之一秒看一次位置足够（片段 30～60 秒）
        monitor = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(250))
                guard let self else { return }
                if self.state == .playing, self.hasFirstFrame, self.core.currentTime >= self.endSeconds {
                    self.core.pause()
                    self.state = .ended
                }
            }
        }
    }

    /// 字幕字号：竖屏时画面只是一条横带，字幕按画面高度的比例算会很小，放大一些；
    /// 全屏（横屏）时画面铺满，回到播放器页的默认字号
    func setFullscreen(_ fullscreen: Bool) {
        core.setTextStyle(fullscreen ? .init() : .init(fontScale: 8, bottomPercent: 6, background: false))
    }

    func play() {
        if prerolling {
            prerolling = false
            core.setPrefetchSuspended(false)
        }
        if state == .ended { replay() } else { core.play() }
    }

    func pause() { core.pause() }

    func togglePause() {
        switch state {
        case .playing: pause()
        case .paused, .ended: play()
        default: break
        }
    }

    /// 从片段起点重放
    func replay() {
        core.seek(to: startSeconds)
        core.play()
        state = .playing
    }

    func destroy() {
        monitor?.cancel()
        monitor = nil
        onStateChange = nil
        onFirstFrame = nil
        core.destroy()
    }

    private func handle(_ phase: AetherPlayback.Phase) {
        switch phase {
        case .playing:
            if state != .ended { state = .playing }
        case .paused:
            if state == .playing { state = .paused }
            // 预起的那条装载好了就停止往前下：滑不滑过去还不知道，别占着当前这条的带宽
            if prerolling { core.setPrefetchSuspended(true) }
        case .ended:
            state = .ended
        case .loading, .buffering:
            if !hasFirstFrame { state = .loading }
        }
    }

    /// 内封字幕按同类型顺序对位（与播放器页 `NativeEngine.selectSubtitle` 同一口径）
    private func applySubtitle() {
        guard !subtitleApplied, let ordinal = item.play.subtitle?.ordinal else { return }
        let embedded = core.subtitleTracks.filter { !$0.isExternal }.sorted { $0.id < $1.id }
        guard ordinal < embedded.count else { return }
        subtitleApplied = true
        core.selectSubtitleTrack(id: embedded[ordinal].id)
    }
}
