import AetherCore
import SwiftUI

/// 刷片页的状态：翻页、当前这条的播放器、预取窗口、事件上报（docs/design/reels.md §4、§6）。
///
/// **当前这条 + 预起的下一条**：当前这条出画 1 秒后（且下一条的预取已下完），为下一条另建一个引擎，
/// 装载到片段起点停着、不再往前下载（`standby`）。滑过去时只要「播放」：不用现建引擎、打开、探测、
/// 等第一帧。换条时旧引擎先停声、半秒后再拆（拆引擎要在主线程上花几十毫秒，别赶在滑动收尾时）。
/// 往回滑、一次跳过好几条时没有预起，退回现建引擎。代价是同时多一个引擎（4K 杜比视界约 135MB 内存）。
/// 2026-09-30 模拟器实测（软件解码通路、字节都已预取，10 次换条中位数）：停稳到开播 139 → 36 毫秒，
/// 到出画 184 → 79 毫秒（剩下的是软件通路停着时不解第一帧、以及旧引擎停声本身）。
///
/// **预取窗口**：当前这条出第一个画面后（不和它抢起播的线路），后台把接下来几条要读的字节
/// 按服务端给的范围写进引擎的片源字节缓存：下一条全量（文件头 + 索引 + 起点后约 4 秒），
/// 再往后两条只取文件头与索引（都很小）。计费网络只预取下一条。滑走的条目的预取任务直接取消。
///
/// **事件**：曝光、出画面（带等待时长）、滑走（带看了多久）、看完、接着看、看正片、放不出，
/// 攒满 10 条或离开页面时批量上报；上报失败直接丢弃（只是统计，不重试）。
///
/// **类型筛选**：顶部「全部 ⌄」换类型时整个信息流重来（新种子、从头抽）。
///
/// **收藏 / 已看**：接口给出每条的初始状态，点按钮走播放器页同一个标记接口，界面先按点击结果
/// 显示、失败再改回去。收藏落在整部（电影 / 整剧），已看电影是整部、剧集是这一集。
///
/// **挂起 / 恢复**：页面被切走（换标签、盖上播放器页）时收掉播放器与预取，回来时当前这条从片段
/// 起点重新起播——引擎不能留在后台占着解码器和内存。
@Observable
@MainActor
final class ReelsStore {
    private(set) var items: [API.ReelItemView] = []
    /// 滚动位置绑定的「当前条」；滑动停稳后由 `settle()` 切换播放器
    var currentID: String?
    private(set) var loading = false
    private(set) var errorMessage: String?
    private(set) var exhausted = false
    private(set) var player: ReelPlayer?
    private(set) var playerState: ReelPlayer.State = .loading
    private(set) var firstFrameShown = false
    /// 预起好的下一条：已装载到片段起点、停在第一帧上，滑过去直接播（见 `scheduleStandby`）
    private(set) var standby: ReelPlayer?
    /// 预起的那条出了第一帧
    private(set) var standbyReady = false
    /// 当前筛选的类型；nil = 全部
    private(set) var genre: String?
    /// 能刷到的类型（顶部下拉的选项）
    private(set) var genres: [API.ReelGenreView] = []
    /// 点过收藏 / 已看后的最新状态（接口给的是初始状态，条目是不可变的结构体）
    private var favoriteOverrides: [Int: Bool] = [:]
    private var playedOverrides: [String: Bool] = [:]

    @ObservationIgnored private let api: APIClient
    @ObservationIgnored private let metered: Bool
    @ObservationIgnored private var seed: Int?
    @ObservationIgnored private var nextOffset = 0
    @ObservationIgnored private var prefetchTasks: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private var standbyTask: Task<Void, Never>?
    @ObservationIgnored private var pendingEvents: [API.ReelEventIn] = []
    @ObservationIgnored private var shownAt: ContinuousClock.Instant?
    /// 当前这条已经「接着看 / 看正片」转去播放器页了
    @ObservationIgnored private var handedOff = false

    static let pageSize = 10
    /// 剩几条时拉下一页
    static let loadAheadThreshold = 3

    init(api: APIClient, metered: Bool) {
        self.api = api
        self.metered = metered
    }

    // MARK: - 翻页

    func start() async {
        if genres.isEmpty, let list = try? await api.reelsGenres() { genres = list }
        if items.isEmpty { await loadMore() }
        if currentID == nil { currentID = items.first?.id }
        settle()
    }

    /// 换类型：整个信息流重来
    func selectGenre(_ name: String?) {
        guard name != genre else { return }
        suspend()
        genre = name
        items = []
        currentID = nil
        seed = nil
        nextOffset = 0
        exhausted = false
        errorMessage = nil
        Task { await start() }
    }

    func loadMore() async {
        guard !loading, !exhausted else { return }
        loading = true
        defer { loading = false }
        do {
            let page = try await api.reelsFeed(seed: seed, offset: nextOffset, limit: Self.pageSize,
                                               modes: "seek", genre: genre)
            seed = page.seed
            nextOffset = page.nextOffset
            exhausted = !page.hasMore
            let known = Set(items.map(\.id))
            items += page.items.filter { $0.play.mode == "seek" && !known.contains($0.id) }
            errorMessage = nil
        } catch {
            errorMessage = "片段加载失败：\(error.localizedDescription)"
        }
    }

    func retry() async {
        errorMessage = nil
        await start()
    }

    // MARK: - 当前条

    /// 滑动停稳：当前条变了就换播放器。下一条预起好了（`standby`）就直接接着放，否则现建引擎
    func settle() {
        guard let id = currentID, player?.item.id != id,
              let index = items.firstIndex(where: { $0.id == id }) else { return }
        // 旧的先停声、稍后再拆：拆引擎要在主线程上花几十毫秒，正赶在滑动收尾时会顿一下
        leaveCurrent(deferTeardown: true)
        let item = items[index]
        shownAt = .now
        record(item, kind: "impression", positionMs: item.segment.startMs)
        if let ready = standby, ready.item.id == id, !ready.state.isFailed {
            standby = nil
            standbyReady = false
            adopt(ready, item: item, index: index)
            ready.play()
            // 第一帧早就出了，引擎不会再报：这里补上「出画面」的记录与后续预取
            if ready.hasFirstFrame { firstFrame(of: item, index: index) }
        } else {
            dropStandby()
            do {
                let player = try ReelPlayer(item: item)
                adopt(player, item: item, index: index)
                player.start(server: api.server)
            } catch {
                playerState = .failed("播放器创建失败")
                record(item, kind: "fail", detail: ["reason": .string("engine_init")])
                schedulePrefetch(after: index)
            }
        }
        if items.count - index <= Self.loadAheadThreshold {
            Task { await loadMore() }
        }
    }

    /// 让一个播放器成为当前这条（新建的，或预起好的）
    private func adopt(_ player: ReelPlayer, item: API.ReelItemView, index: Int) {
        player.onStateChange = { [weak self] state in self?.stateChanged(state, of: item) }
        player.onFirstFrame = { [weak self] in self?.firstFrame(of: item, index: index) }
        self.player = player
        firstFrameShown = player.hasFirstFrame
        playerState = .loading
    }

    /// 某一条现在由哪个播放器出画：当前这条，或预起好的下一条
    func player(for item: API.ReelItemView) -> ReelPlayer? {
        if let player, player.item.id == item.id { return player }
        if let standby, standby.item.id == item.id { return standby }
        return nil
    }

    /// 这一条的画面能不能直接显示（出了第一帧）；不能时页面先垫封面
    func frameReady(for item: API.ReelItemView) -> Bool {
        if player?.item.id == item.id { return firstFrameShown }
        if standby?.item.id == item.id { return standbyReady }
        return false
    }

    func togglePause() { player?.togglePause() }

    func pause() { player?.pause() }

    /// 「接着看」：从当前位置转到播放器页（同一个文件，刷片下过的字节直接复用）
    func continueRequest() -> PlayRequest? {
        guard let player else { return nil }
        let item = player.item
        let position = player.position
        record(item, kind: "continue", positionMs: Int(position * 1000), watchedMs: watchedMs(player))
        handedOff = true
        return PlayRequest(mediaItemId: item.title.mediaItemId, season: item.title.episode?.season,
                           episode: item.title.episode?.episode, startSeconds: position,
                           fileId: item.segment.fileId)
    }

    /// 「看正片」：按正常播放的规则起播（续播点或片头、默认版本）
    func openRequest(for item: API.ReelItemView) -> PlayRequest {
        record(item, kind: "open", watchedMs: player.map { watchedMs($0) })
        handedOff = true
        return PlayRequest(mediaItemId: item.title.mediaItemId, season: item.title.episode?.season,
                           episode: item.title.episode?.episode)
    }

    /// 页面被切走（换标签、返回、盖上播放器页）：收掉播放器与预取，把攒着的事件报上去
    func suspend() {
        leaveCurrent(deferTeardown: false)
        dropStandby()
        for task in prefetchTasks.values { task.cancel() }
        prefetchTasks.removeAll()
        Task { await flush() }
    }

    /// 回到页面：当前这条从片段起点重新起播
    func resume() {
        guard player == nil else { return }
        settle()
    }

    // MARK: - 收藏 / 已看

    func isFavorite(_ item: API.ReelItemView) -> Bool {
        favoriteOverrides[item.title.mediaItemId] ?? item.title.favorite
    }

    func isPlayed(_ item: API.ReelItemView) -> Bool {
        playedOverrides[item.id] ?? item.title.played
    }

    /// 收藏：落在整部（电影 / 整剧）上
    func toggleFavorite(_ item: API.ReelItemView) async {
        let target = !isFavorite(item)
        favoriteOverrides[item.title.mediaItemId] = target
        do {
            let state = try await api.playbackMarksSet(body: API.PlaybackMarksRequest(
                mediaItemId: item.title.mediaItemId, favorite: target
            ))
            favoriteOverrides[item.title.mediaItemId] = state.isFavorite
        } catch {
            favoriteOverrides[item.title.mediaItemId] = !target
        }
    }

    /// 已看：电影标整部，剧集标这一集
    func togglePlayed(_ item: API.ReelItemView) async {
        let target = !isPlayed(item)
        playedOverrides[item.id] = target
        do {
            let state = try await api.playbackMarksSet(body: API.PlaybackMarksRequest(
                mediaItemId: item.title.mediaItemId, seasonNumber: item.title.episode?.season,
                episodeNumber: item.title.episode?.episode, played: target
            ))
            playedOverrides[item.id] = state.played
        } catch {
            playedOverrides[item.id] = !target
        }
    }

    /// - Parameter deferTeardown: 先停声，拆引擎放到半秒后（滑动收尾时不占主线程）；
    ///   挂起页面时要立刻拆，把解码器与内存让给播放器页
    private func leaveCurrent(deferTeardown: Bool) {
        guard let player else { return }
        // 已经接着看 / 看正片了：这一条是转去正片，不是滑走
        if player.state != .ended, !handedOff {
            record(player.item, kind: "leave", positionMs: Int(player.position * 1000), watchedMs: watchedMs(player))
        }
        player.onStateChange = nil
        player.onFirstFrame = nil
        if deferTeardown {
            player.pause()
            Task {
                try? await Task.sleep(for: .milliseconds(500))
                player.destroy()
            }
        } else {
            player.destroy()
        }
        self.player = nil
        handedOff = false
    }

    private func watchedMs(_ player: ReelPlayer) -> Int {
        Int(max(0, player.position - player.startSeconds) * 1000)
    }

    private func stateChanged(_ state: ReelPlayer.State, of item: API.ReelItemView) {
        guard player?.item.id == item.id else { return }
        playerState = state
        switch state {
        case .ended:
            if let player { record(item, kind: "complete", positionMs: item.segment.endMs, watchedMs: watchedMs(player)) }
        case let .failed(message):
            record(item, kind: "fail", detail: ["reason": .string(message)])
            if let index = items.firstIndex(where: { $0.id == item.id }) { schedulePrefetch(after: index) }
        default:
            break
        }
    }

    private func firstFrame(of item: API.ReelItemView, index: Int) {
        guard player?.item.id == item.id else { return }
        firstFrameShown = true
        let waited = shownAt.map { ContinuousClock.now - $0 } ?? .zero
        let waitMs = Int(waited.components.seconds * 1000 + waited.components.attoseconds / 1_000_000_000_000_000)
        record(item, kind: "first_frame", positionMs: item.segment.startMs, waitMs: waitMs)
        schedulePrefetch(after: index)
        scheduleStandby(after: index)
    }

    // MARK: - 预取

    private func schedulePrefetch(after index: Int) {
        let window = metered ? 1 : 3
        let targets = Array(items.dropFirst(index + 1).prefix(window))
        let keep = Set(targets.map(\.id))
        for (taskKey, task) in prefetchTasks where !keep.contains(String(taskKey.split(separator: "#")[0])) {
            task.cancel()
            prefetchTasks[taskKey] = nil
        }
        for (position, item) in targets.enumerated() {
            // 下一条全量；再往后只取文件头与索引（起点后几秒那段动辄几十 MB，滑不到就白下了）。
            // 任务按「条目 + 全量/轻量」分开记：一条从后排挪到下一条时，要补上起点那一段——
            // 已经下过的文件头与索引由引擎按缓存覆盖跳过，不会重下
            let full = position == 0
            let taskKey = "\(item.id)#\(full ? "full" : "light")"
            guard prefetchTasks[taskKey] == nil, prefetchTasks["\(item.id)#full"] == nil,
                  let raw = item.play.streamUrl, let url = api.server.resolve(raw),
                  let key = ReelPlayer.cacheKey(for: item) else { continue }
            let ranges = item.play.prefetch
                .filter { full || $0.purpose != "start" }
                .map { (offset: Int64($0.offset), length: Int64($0.length)) }
            prefetchTasks[taskKey] = Task.detached(priority: .utility) {
                _ = await AetherPlayback.prefetchSource(url: url, cacheKey: key, ranges: ranges,
                                                        headers: ["User-Agent": APIClient.userAgent])
            }
        }
    }

    // MARK: - 预起下一条

    /// 当前这条放稳（出画 1 秒后，且下一条的预取已下完）再预起下一条：装载到片段起点、停在第一帧，
    /// 之后不再往前下载。滑过去时只要「播放」，不用再建引擎、打开、探测、出第一帧。
    /// 等 1 秒是让开滑动收尾：旧引擎半秒后才拆，新引擎也要在主线程上建
    private func scheduleStandby(after index: Int) {
        standbyTask?.cancel()
        guard index + 1 < items.count else { return }
        let next = items[index + 1]
        guard standby?.item.id != next.id else { return }
        let prefetch = prefetchTasks["\(next.id)#full"]
        standbyTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(1))
            await prefetch?.value
            guard let self, !Task.isCancelled, self.player != nil, self.currentID != next.id else { return }
            self.prepareStandby(next)
        }
    }

    private func prepareStandby(_ item: API.ReelItemView) {
        dropStandby()
        guard let standby = try? ReelPlayer(item: item) else { return }
        standby.onFirstFrame = { [weak self, weak standby] in
            guard let self, let standby, self.standby === standby else { return }
            self.standbyReady = true
        }
        self.standby = standby
        standbyReady = false
        standby.start(server: api.server, autoplay: false)
    }

    private func dropStandby() {
        standbyTask?.cancel()
        standbyTask = nil
        guard let standby else { return }
        standby.onFirstFrame = nil
        standby.destroy()
        self.standby = nil
        standbyReady = false
    }

    // MARK: - 事件

    private func record(_ item: API.ReelItemView, kind: String, positionMs: Int? = nil, watchedMs: Int? = nil,
                        waitMs: Int? = nil, detail: [String: API.JSONValue]? = nil) {
        var detail = detail ?? [:]
        detail["network"] = .string(NetworkCost.shared.interface)
        pendingEvents.append(API.ReelEventIn(
            reelId: item.id, kind: kind, mode: item.play.mode, mediaItemId: item.title.mediaItemId,
            fileId: item.segment.fileId, positionMs: positionMs, watchedMs: watchedMs, waitMs: waitMs,
            detail: detail
        ))
        if pendingEvents.count >= 10 { Task { await flush() } }
    }

    private func flush() async {
        let batch = pendingEvents
        pendingEvents.removeAll()
        guard !batch.isEmpty else { return }
        _ = try? await api.reelsEvents(body: API.ReelEventBatch(events: batch))
    }
}
