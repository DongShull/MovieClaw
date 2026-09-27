import AVFoundation
import Foundation
import UIKit

/// 通知：一场播放的「停止」已被服务端收下（续播点、已看标记已更新）。userInfo["mediaItemId"] 是条目 id。
/// 详情页据此重拉续播点——关掉播放器回来，播放键从「播放」变「继续」、时间跟上刚才看到的位置。
/// 在服务端确认之后才发，避免抢在写入前读到旧进度
nonisolated extension Notification.Name {
    static let playbackStopReported = Notification.Name("MovieClaw.playbackStopReported")
}

/// 播放单元：电影用 (0, 0) 哨兵，与后端台账、playback_state 的约定一致。
struct PlaybackUnit: Hashable {
    var mediaItemId: Int
    var season: Int
    var episode: Int

    var isEpisode: Bool { season > 0 || episode > 0 }

    /// SxxExx（季集号补零是媒体库的通用写法）
    var code: String { String(format: "S%02dE%02d", season, episode) }
}

/// 播放接口的作用域（对应 Web `PlaybackApiScope`，docs/design/media-share.md §5.3）。
///
/// 登录成员走 `/playback/…`；分享访客走 `/share/{slug}/playback/…` 这条按 slug 收窄的公开通道：
/// 访客没有成员身份，进度只记本机、遥测（metrics / client-log）不上报。
/// 播放器其余代码只认这一层，不关心自己在哪个作用域里——这样分享播放与登录播放共用同一套界面与状态机。
struct PlaybackAPI {
    let api: APIClient
    let shareSlug: String?

    var isShare: Bool { shareSlug != nil }
    var telemetry: Bool { !isShare }
    let deviceId = PlayerPreferences.deviceId

    // MARK: 条目与分集

    func item(_ mediaItemId: Int) async throws -> API.PlaybackItemView {
        if let shareSlug { return try await api.sharePlaybackItem(mediaItemId: mediaItemId, slug: shareSlug) }
        return try await api.playbackItemInfo(mediaItemId: mediaItemId)
    }

    func episodes(_ mediaItemId: Int, season: Int) async throws -> [API.EpisodeView] {
        if let shareSlug {
            return try await api.sharePlaybackItemEpisodes(mediaItemId: mediaItemId, slug: shareSlug, seasonNumber: season).episodes
        }
        return try await api.playbackItemEpisodes(mediaItemId: mediaItemId, seasonNumber: season).episodes
    }

    // MARK: 决策与会话
    //
    // 这两个接口标成 nonisolated：起播协商（`negotiate`）要在后台一口气跑完，不能每一步都回主线程排队。

    /// 只问「该怎么放」，不起会话（自动引擎选择用它先探一次 AVPlayer 的档位）
    nonisolated func decide(_ body: API.PlaybackSessionRequest) async throws -> API.PlaybackDecisionView {
        let request = API.PlaybackDecideRequest(
            fileId: body.fileId, mediaItemId: body.mediaItemId, seasonNumber: body.seasonNumber,
            episodeNumber: body.episodeNumber, capability: body.capability, failedTiers: body.failedTiers,
            audioTrack: body.audioTrack, subtitleTrack: body.subtitleTrack, maxHeight: body.maxHeight,
            downlinkBps: body.downlinkBps
        )
        if let shareSlug { return try await api.sharePlaybackDecide(slug: shareSlug, body: request) }
        return try await api.playbackDecide(body: request)
    }

    nonisolated func startSession(_ body: API.PlaybackSessionRequest) async throws -> API.PlaybackSessionView {
        var body = body
        body.deviceId = deviceId
        if let shareSlug { return try await api.sharePlaybackSessionStart(slug: shareSlug, body: body) }
        return try await api.playbackSessionStart(body: body)
    }

    /// 起播协商用的引擎模式（由控制器按强制引擎、MPV 可用性、图形字幕偏好算好）
    enum EngineMode: Sendable {
        /// 先用 AVPlayer 的能力问一次决策，再按结果选引擎（规则见 `PlaybackController` 的类注释）
        case auto
        case system
        case mpv
    }

    /// 起播协商的全部输入：控制器在主线程一次算好（能力快照要读屏幕与机型），之后整段在后台跑
    struct NegotiationInputs: Sendable {
        var mode: EngineMode
        /// 按 AVPlayer 能力的请求体（也用于自动模式的决策）
        var system: API.PlaybackSessionRequest
        /// 按 MPV 能力的请求体
        var mpv: API.PlaybackSessionRequest
        /// 按「移动端原生 HLS」口径的 MPV 请求体：服务端拒绝 / 要同意时拿它换一张取流凭据
        var mpvLimited: API.PlaybackSessionRequest
        /// MPV 这次打算直接拉原文件（请求体里申报了全解码）
        var mpvOriginal: Bool
        /// 本来就要服务端压码率（用户限了画质 / 线路不够）：这时交给 AVPlayer 放 HLS
        var needsServerTranscode: Bool
    }

    /// 起播协商的结果与各段完成时刻（起播分段计时用）
    struct Negotiation: Sendable {
        var useMPV: Bool
        var askedOriginal: Bool
        var session: API.PlaybackSessionView
        var startedAt: ContinuousClock.Instant
        var decidedAt: ContinuousClock.Instant?
        var sessionAt: ContinuousClock.Instant
        var retriedAt: ContinuousClock.Instant?
        /// 给系统播放器预先建好、已经在加载的资源（见 `negotiate` 末尾）；MPV 或没给出计划时为 nil
        var preparedAsset: AVURLAsset?
    }

    /// 起播协商：（自动模式先决策、选引擎）→ 开会话 →（MPV 被拒时按移动端口径再要一次计划）。
    ///
    /// 整段在后台执行器上跑完、中途不回主线程：播放器刚弹出时主线程忙着排版和转场，
    /// 原来每个网络往返都要回主线程续跑，起播请求光是排队就要一两百毫秒（模拟器实测）。
    @concurrent
    nonisolated func negotiate(_ inputs: NegotiationInputs) async throws -> Negotiation {
        let clock = ContinuousClock()
        let startedAt = clock.now
        var decidedAt: ContinuousClock.Instant?
        let useMPV: Bool
        switch inputs.mode {
        case .system:
            useMPV = false
        case .mpv:
            useMPV = true
        case .auto:
            let probe = try await decide(inputs.system)
            decidedAt = clock.now
            if probe.outcome == "plan", probe.video?.action != "transcode" {
                // 直出或只换封装/转音频：系统播放器（画中画、隔空播放、系统字体字幕）
                useMPV = false
            } else if probe.outcome == "plan", inputs.needsServerTranscode {
                // 用户自己限了画质 / 线路不够：本来就要服务端转码，交给 AVPlayer 放 HLS
                useMPV = false
            } else {
                // 要为系统播放器重新编码画面（含图形字幕压制）、或被拒绝/要同意：MPV 在本机直接放原文件
                useMPV = true
            }
        }
        var session = try await startSession(useMPV ? inputs.mpv : inputs.system)
        let sessionAt = clock.now
        var retriedAt: ContinuousClock.Instant?
        if useMPV, session.decision.outcome != "plan", !inputs.needsServerTranscode {
            // 服务端按「浏览器口径」拒绝了（例如 4K 杜比视界没有显卡做色调映射）或要求同意软转，
            // 但 MPV 自己就能解原片。服务端只在给出计划时签发取流 token，于是按移动端口径再要一次计划
            let retry = try await startSession(inputs.mpvLimited)
            if retry.decision.outcome == "plan" { session = retry }
            retriedAt = clock.now
        }
        // 系统播放器要放的地址此刻已经确定：马上建好资源、开始读文件头 / 播放列表。
        // 主线程这时多半还在忙播放器弹出的转场，挂引擎要再等几十毫秒——AVFoundation 先干起来
        var preparedAsset: AVURLAsset?
        if !useMPV, session.decision.outcome == "plan", let url = Self.systemPlayerURL(session, server: api.server) {
            let asset = AVURLAsset(url: url)
            Task { _ = try? await asset.load(.isPlayable) }
            preparedAsset = asset
        }
        return Negotiation(
            useMPV: useMPV, askedOriginal: useMPV && inputs.mpvOriginal, session: session,
            startedAt: startedAt, decidedAt: decidedAt, sessionAt: sessionAt, retriedAt: retriedAt,
            preparedAsset: preparedAsset
        )
    }

    /// 系统播放器该吃的地址：VOD 会话吃 master 列表（里面的 WEBVTT 字幕组让画中画 / 隔空播放时由系统渲染字幕），
    /// 其余（原文件直出、旧式会话列表）用 stream_url。与 `PlaybackController.handleSession` 的取址规则一致
    nonisolated static func systemPlayerURL(_ session: API.PlaybackSessionView, server: ServerAddress) -> URL? {
        if session.timeline == "file", let master = session.masterUrl { return server.resolve(master) }
        return server.resolve(session.streamUrl)
    }

    /// 会话续命兼探活。三态必须区分（同 Web `pingPlaybackSession`）：
    /// true = 还在；false = 服务端明确说没了（404），要原地重开；nil = 这次请求本身失败，不能当成没了。
    func ping(_ sessionId: String) async -> Bool? {
        do {
            if let shareSlug {
                _ = try await api.sharePlaybackSessionPing(sessionId: sessionId, slug: shareSlug)
            } else {
                _ = try await api.playbackSessionPing(sessionId: sessionId)
            }
            return true
        } catch let error as APIError where error.status == 404 {
            return false
        } catch {
            return nil
        }
    }

    /// 结束会话（掐断 ffmpeg、清临时分片）。失败无所谓：服务端有心跳超时回收兜底。
    func stop(_ sessionId: String) async {
        if let shareSlug {
            _ = try? await api.sharePlaybackSessionStop(sessionId: sessionId, slug: shareSlug)
        } else {
            _ = try? await api.playbackSessionStop(sessionId: sessionId)
        }
    }

    func diagnostics(_ sessionId: String, token: String) async throws -> API.PlaybackDiagnosticsView {
        if let shareSlug {
            return try await api.sharePlaybackSessionDiagnostics(sessionId: sessionId, slug: shareSlug, token: token)
        }
        return try await api.playbackSessionDiagnostics(sessionId: sessionId, token: token)
    }

    // MARK: 进度

    /// 上报观看进度（start / progress / stop 同一入口）。
    /// 分享访客：位置记本机；服务端只收一份「谁在播」的心跳给活动页，靠响应里的 ended_by_admin 退出。
    /// 请求包在后台任务里：切后台、暂停、退出时发出的上报不会因为 App 被挂起而丢在半路。
    @discardableResult
    func progress(_ unit: PlaybackUnit, event: String, positionMs: Int?, durationMs: Int? = nil, paused: Bool? = nil,
                  audio: String?, subtitle: String?) async -> API.PlaybackStateView? {
        let body = progressBody(unit, event: event, positionMs: positionMs, paused: paused, audio: audio, subtitle: subtitle)
        let background = UIApplication.shared.beginBackgroundTask(withName: "playback-progress")
        defer { if background != .invalid { UIApplication.shared.endBackgroundTask(background) } }
        if let shareSlug {
            ShareLocalProgress.write(shareSlug, unit, positionMs: Self.localResume(positionMs, durationMs: durationMs), audio: audio, subtitle: subtitle)
            return try? await api.sharePlaybackProgress(slug: shareSlug, body: body)
        }
        return try? await api.playbackProgress(body: body)
    }

    /// App 即将被结束：同步补发一次 stop，最多等 1.5 秒（异步任务在进程退出前跑不完）
    func stopBeforeTermination(_ unit: PlaybackUnit, positionMs: Int, durationMs: Int?, audio: String?, subtitle: String?) {
        let body = progressBody(unit, event: "stop", positionMs: positionMs, paused: nil, audio: audio, subtitle: subtitle)
        let path: String
        if let shareSlug {
            ShareLocalProgress.write(shareSlug, unit, positionMs: Self.localResume(positionMs, durationMs: durationMs), audio: audio, subtitle: subtitle)
            path = "/share/\(shareSlug)/playback/progress"
        } else {
            path = "/playback/progress"
        }
        var request = URLRequest(url: api.url(path))
        request.httpMethod = "POST"
        request.timeoutInterval = 1.5
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? APIClient.encoder.encode(body)
        let done = DispatchSemaphore(value: 0)
        api.session.dataTask(with: api.authorized(request)) { _, _, _ in done.signal() }.resume()
        _ = done.wait(timeout: .now() + 1.5)
    }

    private func progressBody(_ unit: PlaybackUnit, event: String, positionMs: Int?, paused: Bool?,
                              audio: String?, subtitle: String?) -> API.PlaybackProgressRequest {
        API.PlaybackProgressRequest(
            mediaItemId: unit.mediaItemId, seasonNumber: unit.season, episodeNumber: unit.episode,
            event: event, positionMs: positionMs, audioTrack: audio, subtitleTrack: subtitle,
            deviceId: deviceId, paused: paused
        )
    }

    /// 分享访客的本机续播点：看过 90% 或到了片尾就记 0（下次从头放），同服务端 resolve_progress 的口径
    static func localResume(_ positionMs: Int?, durationMs: Int?) -> Int? {
        guard let positionMs, let durationMs, durationMs > 0 else { return positionMs }
        return positionMs * 10 >= durationMs * 9 || positionMs >= durationMs - 1000 ? 0 : positionMs
    }

    // MARK: 遥测（分享访客一律不报）

    func clientLog(_ event: String, _ detail: [String: API.JSONValue]) {
        guard telemetry else { return }
        let api = self.api
        Task { _ = try? await api.playbackClientLog(body: API.PlaybackClientLogPayload(event: event, detail: detail)) }
    }

    func metric(_ payload: API.PlaybackMetricPayload) {
        guard telemetry else { return }
        let api = self.api
        Task { _ = try? await api.playbackMetricReport(body: payload) }
    }

    // MARK: 取流地址

    /// 后端给的是服务器根相对路径（`/api/v1/playback/...`，token 已在里面）
    func streamURL(_ path: String?) -> URL? {
        api.server.resolve(path)
    }

    /// 从已签名的地址里取 token（诊断、缩略图、原文件直出复用同一份授权）
    static func token(in path: String?) -> String? {
        guard let path, let components = URLComponents(string: path) else { return nil }
        return components.queryItems?.first { $0.name == "token" }?.value
    }
}
