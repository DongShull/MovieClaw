import AVFoundation
import SwiftUI

/// 片段：上下整页滑动，每页从一部电影 / 一部剧里挑出的 30～60 秒（docs/design/reels.md）。
///
/// 版式对齐 Instagram Reels / 抖音（2026-09-30 用户给的参照图）：
/// - 媒体库导航栈里压栈打开，**底部标签栏保留**（停在这页时标签栏不随滑动收起）；不带返回键，
///   再点一次「媒体库」页签回到媒体库首页；左上是标题「片段」，右上一个玻璃胶囊「全部 ⌄」，
///   点开选「全部」或某个类型；这一页锁竖屏，转手机不会把信息流转横；
/// - 纯黑底，影片居中成一条 16:9 的横带；没出第一帧前先显示封面（服务端抓的就是起点那一帧）；
///   横带下方一个描边小胶囊「全屏观看」（TikTok 横屏视频的做法），点了横过来全屏看这一段，
///   用的是同一个播放器，不打断、不重新加载，退出回到竖屏接着刷（ReelFullscreenView）；
/// - 右下角一列**无底色**的白色图标按钮（不用毛玻璃，和画面融在一起，带投影保证亮画面上也看得清）：
///   收藏、播放、已看、分享；
/// - 左下角四行，层级从强到弱：导演（剧集是主创，对应 TikTok / Instagram 的作者行，点了进人物页）、
///   片名（剧集后面跟季集）、年份 · 评分 · 类型、一行简介（剧集前面是集名，放不下就「展开」）；
/// - 最底下一条细进度线（这一段的进度），右边是「在整部片里放到哪 / 整部片多长」。
///
/// 播放按钮：点一下从当前位置转到播放器页（同一个文件，刷片下过的字节直接复用），长按可选「从头看」。
/// 点画面暂停 / 继续，放到片段终点停下、再点重播。
struct ReelsView: View {
    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Router.self) private var router
    @Environment(\.scenePhase) private var scenePhase
    @State private var store: ReelsStore?
    @State private var visible = false
    @State private var sharing: API.ReelItemView?
    /// 全屏（横屏）观看当前这一条
    @State private var fullscreen = false
    /// 最近一次竖屏时的整页尺寸。全屏看、横着进播放器页时整页会转横，翻页区要是跟着按横屏重排，
    /// 转回竖屏后滚动位置对不上原来那一条（模拟器实测从第二条跳到了第三条）。所以每页始终按竖屏尺寸排：
    /// 横屏期间翻页区被全屏层或播放器页整个盖住，看不出差别
    @State private var portraitPage: PageGeometry?

    var body: some View {
        GeometryReader { geo in
            let current = PageGeometry(size: geo.size, insets: geo.safeAreaInsets)
            let page = portraitPage ?? current
            ZStack {
                Color.black
                if let store {
                    if store.items.isEmpty {
                        emptyState(store)
                    } else {
                        pager(store, size: page.size, insets: page.insets)
                    }
                }
            }
            .ignoresSafeArea()
            .onChange(of: current, initial: true) { _, now in
                if now.size.height > now.size.width { portraitPage = now }
            }
        }
        .background(Color.black.ignoresSafeArea())
        .overlay {
            if fullscreen, let store {
                ZStack {
                    // 点「看全片」时刷片的引擎先收掉、播放器页随后才盖上来：这期间垫黑，不露出底下横着的信息流
                    Color.black.ignoresSafeArea()
                    if let player = store.player {
                        ReelFullscreenView(
                            store: store, player: player,
                            onExit: { setFullscreen(false) },
                            onPlayFull: {
                                // 横着看的直接横着进播放器页：方向锁不动，不先转回竖屏再转过去；
                                // 全屏状态留到播放器页盖上之后再收（onDisappear）
                                play(from: store, restart: false, item: player.item, landscape: true)
                            }
                        )
                    }
                }
                .transition(.opacity)
            }
        }
        // 标题不用系统的大标题：系统大标题跟着滚动视图走，一滑到下一条就缩成正中的小字。
        // 这里放在左上角的工具栏位、去掉玻璃底，字号与媒体库首页的标题一致，滑动时不变
        .navigationTitle("")
        .toolbarTitleDisplayMode(.inline)
        .navigationBarBackButtonHidden(true)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Text("片段")
                    .font(.title.weight(.bold))
                    .foregroundStyle(.white)
                    .fixedSize()
                    .accessibilityAddTraits(.isHeader)
                    .accessibilityIdentifier("reels-title")
            }
            .sharedBackgroundVisibility(.hidden)
            ToolbarItem(placement: .topBarTrailing) { genreMenu }
        }
        .toolbarVisibility(fullscreen ? .hidden : .automatic, for: .navigationBar, .tabBar)
        .statusBarHidden(fullscreen)
        .persistentSystemOverlays(fullscreen ? .hidden : .automatic)
        .preferredColorScheme(.dark)
        .task {
            if store == nil { store = ReelsStore(api: api, metered: NetworkCost.shared.isMetered) }
            await store?.start()
        }
        .onAppear {
            visible = true
            UIApplication.shared.isIdleTimerDisabled = true
            let audio = AVAudioSession.sharedInstance()
            try? audio.setCategory(.playback, mode: .moviePlayback, policy: .longFormVideo)
            try? audio.setActive(true)
            // 信息流锁竖屏：转手机不该把整页转横（横着看走「全屏观看」）
            PlayerOrientation.request(landscape: false)
            store?.resume()
        }
        .onDisappear {
            visible = false
            if fullscreen { setFullscreen(false, rotate: false) }
            // 被播放器页盖住时方向归播放器页管（它关掉时会解锁）；真正离开这一页才解开竖屏锁
            if router.player == nil { PlayerOrientation.release() }
            store?.suspend()
            UIApplication.shared.isIdleTimerDisabled = false
        }
        // 播放器页是根部的全屏弹层：盖上来时收掉刷片的引擎，收起后当前这条重新起播
        .onChange(of: router.player == nil) { _, closed in
            if closed {
                // 从全屏「看全片」过去、盖上时没收到 onDisappear 的，回来前把全屏收掉
                if fullscreen { setFullscreen(false, rotate: false) }
                if visible {
                    PlayerOrientation.request(landscape: false)
                    store?.resume()
                }
            } else {
                store?.suspend()
            }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase != .active { store?.pause() }
        }
        .sheet(isPresented: Binding(get: { sharing != nil }, set: { if !$0 { sharing = nil } })) {
            if let item = sharing {
                LibraryShareSheet(
                    target: .item(libraryId: item.title.libraryId, mediaItemId: item.title.mediaItemId),
                    title: item.title.name, kind: item.title.kind, year: item.title.year,
                    posterUrl: item.title.posterUrl
                )
                .sheetFeedback()
            }
        }
    }

    // MARK: - 全屏

    /// 进出全屏：播放器不换，只把画面挪到全屏层、转横 / 转回竖屏，字幕换成对应字号
    private func setFullscreen(_ on: Bool, rotate: Bool = true) {
        guard fullscreen != on else { return }
        store?.player?.setFullscreen(on)
        if rotate { PlayerOrientation.request(landscape: on) }
        withAnimation(.easeInOut(duration: 0.2)) { fullscreen = on }
    }

    // MARK: - 顶部类型选择

    private var genreMenu: some View {
        Menu {
            Picker("类型", selection: Binding(get: { store?.genre }, set: { store?.selectGenre($0) })) {
                Text("全部").tag(String?.none)
                ForEach(store?.genres ?? [], id: \.name) { genre in
                    Text(genre.name).tag(String?.some(genre.name))
                }
            }
        } label: {
            // 右上角的工具栏位由系统画液态玻璃底，这里只给内容
            HStack(spacing: 5) {
                Text(store?.genre ?? "全部")
                    .font(.subheadline.weight(.semibold))
                Image(systemName: "chevron.down")
                    .font(.caption2.weight(.bold))
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 4)
        }
        .accessibilityLabel("类型：\(store?.genre ?? "全部")")
        .accessibilityIdentifier("reels-genre")
    }

    // MARK: - 翻页

    private func pager(_ store: ReelsStore, size: CGSize, insets: EdgeInsets) -> some View {
        ScrollView(.vertical) {
            LazyVStack(spacing: 0) {
                ForEach(store.items, id: \.id) { item in
                    ReelPage(
                        item: item, store: store, isCurrent: item.id == store.currentID, insets: insets,
                        showsVideo: !fullscreen,
                        canCreateShareLink: permissions.isAdmin,
                        onPlay: { play(from: store, restart: false, item: item) },
                        onPlayFromStart: { play(from: store, restart: true, item: item) },
                        onShare: {
                            store.pause()
                            sharing = item
                        },
                        onOpenPerson: { router.push(.person(tmdbId: $0)) },
                        onFullscreen: { setFullscreen(true) }
                    )
                    .frame(width: size.width + insets.leading + insets.trailing,
                           height: size.height + insets.top + insets.bottom)
                }
            }
            .scrollTargetLayout()
        }
        .scrollTargetBehavior(.paging)
        // 全屏时整页转横、翻页区跟着重新排版，滚动位置会被动地变：这期间不认，免得把正在放的那条换掉
        .scrollPosition(id: Binding(get: { store.currentID }, set: { if !fullscreen { store.currentID = $0 } }))
        .scrollIndicators(.hidden)
        .scrollDisabled(fullscreen)
        .onScrollPhaseChange { _, phase in
            // 滑动停稳才换播放器：拖动途中 currentID 会跟着变，不能每变一次就起一个引擎
            if phase == .idle, !fullscreen { store.settle() }
        }
    }

    /// - Parameter landscape: 从全屏过来的，保持横屏锁（与播放器页自己的横屏键同一种锁，
    ///   播放器页里「退出横屏」、关掉播放器都照常）
    private func play(from store: ReelsStore, restart: Bool, item: API.ReelItemView, landscape: Bool = false) {
        let request = restart ? store.openRequest(for: item) : (store.continueRequest() ?? store.openRequest(for: item))
        store.suspend()
        // 播放器页跟随手机方向：先解开刷片的竖屏锁
        if !landscape { PlayerOrientation.release() }
        router.play(request)
    }

    // MARK: - 空态

    private func emptyState(_ store: ReelsStore) -> some View {
        VStack(spacing: 14) {
            if store.loading {
                ProgressView()
                Text("正在挑片段…")
            } else if let message = store.errorMessage {
                Text(message)
                Button("重试") { Task { await store.retry() } }
                    .buttonStyle(.glass)
            } else if let genre = store.genre {
                Text("「\(genre)」里还没有能刷的片子")
                Button("看全部") { store.selectGenre(nil) }
                    .buttonStyle(.glass)
            } else {
                Text("片库里还没有能刷的片子")
                Text("目前支持 MKV / MP4 的电影与剧集")
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
            }
        }
        .font(.subheadline)
        .foregroundStyle(Theme.textMuted)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct PageGeometry: Equatable {
    var size: CGSize
    var insets: EdgeInsets
}

/// 片段的一页
private struct ReelPage: View {
    let item: API.ReelItemView
    let store: ReelsStore
    let isCurrent: Bool
    let insets: EdgeInsets
    /// 全屏时画面挪到全屏层，这里只留封面
    let showsVideo: Bool
    let canCreateShareLink: Bool
    let onPlay: () -> Void
    let onPlayFromStart: () -> Void
    let onShare: () -> Void
    let onOpenPerson: (Int) -> Void
    let onFullscreen: () -> Void

    @Environment(\.api) private var api
    /// 简介展开了
    @State private var expanded = false

    var body: some View {
        GeometryReader { geo in
            let width = geo.size.width
            let bandHeight = width * 9 / 16
            // 横带的中心放在屏幕 46% 高处（参照图：略高于正中，给底部信息留地方）
            let bandCenter = geo.size.height * 0.46
            ZStack(alignment: .bottom) {
                Color.black
                videoBand(width: width, height: bandHeight)
                    .position(x: width / 2, y: bandCenter)
                if !expanded {
                    fullscreenButton
                        .position(x: width / 2, y: bandCenter + bandHeight / 2 + 28)
                }
                VStack(spacing: 12) {
                    HStack(alignment: .bottom, spacing: 12) {
                        info
                        actions
                    }
                    ReelProgressRow(item: item, player: player)
                }
                .padding(.horizontal, Theme.pagePadding)
                .padding(.bottom, insets.bottom + 10)
            }
        }
        .onChange(of: isCurrent) { _, current in
            if !current { expanded = false }
        }
    }

    /// 这一条的播放器：当前这条，或预起好的下一条（滑动途中下一页就是它的第一帧，不是封面）
    private var player: ReelPlayer? { store.player(for: item) }

    // MARK: 画面

    private func videoBand(width: CGFloat, height: CGFloat) -> some View {
        ZStack {
            Color.black
            RemoteImage(url: api.image(item.coverUrl, .landscapeCard), contentMode: .fit)
            if showsVideo, let player {
                ReelVideoSurface(engineView: player.core.view)
                    .id(ObjectIdentifier(player))
                    .opacity(store.frameReady(for: item) ? 1 : 0)
            }
            if showsVideo { overlay }
        }
        .frame(width: width, height: height)
        .clipped()
        .contentShape(Rectangle())
        .onTapGesture { if isCurrent { store.togglePause() } }
        .accessibilityIdentifier("reels-video")
    }

    @ViewBuilder
    private var overlay: some View {
        if isCurrent {
            switch store.playerState {
            case .loading where !store.firstFrameShown:
                ProgressView().tint(.white)
            case .paused:
                ReelCenterGlyph(symbol: "play.fill")
            case .ended:
                ReelCenterGlyph(symbol: "arrow.counterclockwise")
            case let .failed(message):
                VStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle")
                    Text("这一段放不出来，往下滑换一条")
                        .font(.footnote)
                    Text(message)
                        .font(.caption2)
                        .foregroundStyle(Theme.textFaint)
                        .lineLimit(2)
                }
                .foregroundStyle(Theme.textMuted)
                .padding(.horizontal, 24)
            default:
                EmptyView()
            }
        }
    }

    /// 横带下方的「全屏观看」：描边小胶囊（不用毛玻璃，与右下角按钮同一种「融在画面里」的做法）
    private var fullscreenButton: some View {
        Button(action: onFullscreen) {
            Label("全屏观看", systemImage: "arrow.up.left.and.arrow.down.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.white.opacity(0.92))
                .padding(.horizontal, 14)
                .padding(.vertical, 7)
                .overlay(Capsule().stroke(.white.opacity(0.35), lineWidth: 1))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .disabled(!isCurrent || store.player == nil)
        .opacity(isCurrent ? 1 : 0)
        .accessibilityIdentifier("reels-fullscreen-button")
    }

    // MARK: 右下角按钮

    private var actions: some View {
        let favorite = store.isFavorite(item)
        let played = store.isPlayed(item)
        return VStack(spacing: 18) {
            ReelActionButton(symbol: favorite ? "heart.fill" : "heart", title: "收藏",
                             tint: favorite ? Color(red: 1, green: 0.27, blue: 0.35) : .white) {
                Task { await store.toggleFavorite(item) }
            }
            .accessibilityValue(favorite ? "已收藏" : "未收藏")
            .accessibilityIdentifier("reels-favorite")
            ReelActionButton(symbol: "play.fill", title: "播放", action: onPlay)
                .contextMenu {
                    Button("从这里接着看", systemImage: "play.fill", action: onPlay)
                    Button("从头看", systemImage: "backward.end.fill", action: onPlayFromStart)
                }
                .accessibilityIdentifier("reels-play")
            ReelActionButton(symbol: played ? "checkmark.circle.fill" : "checkmark.circle", title: "已看",
                             tint: played ? Theme.success : .white) {
                Task { await store.togglePlayed(item) }
            }
            .accessibilityValue(played ? "已看过" : "没看过")
            .accessibilityIdentifier("reels-played")
            if canCreateShareLink {
                ReelActionButton(symbol: "paperplane", title: "分享", action: onShare)
                    .accessibilityIdentifier("reels-share")
            } else {
                ShareLink(item: shareText) {
                    ReelActionLabel(symbol: "paperplane", title: "分享", tint: .white)
                }
                .accessibilityIdentifier("reels-share")
            }
        }
    }

    private var shareText: String {
        var text = "《\(item.title.name)》"
        if let year = item.title.year { text += "（\(year)）" }
        if let episode = item.title.episode { text += " 第 \(episode.season) 季第 \(episode.episode) 集" }
        return text
    }

    // MARK: 左下角信息

    /// 四行，层级从强到弱：导演 → 片名 → 年份评分类型 → 一行简介。组与组之间留得比组内宽，
    /// 字号只用两档（片名 headline、其余 footnote / subheadline），颜色只用白与 70% 白
    private var info: some View {
        VStack(alignment: .leading, spacing: 10) {
            if !item.title.directors.isEmpty { directorRow }
            VStack(alignment: .leading, spacing: 4) {
                titleRow
                if !metaLine.isEmpty {
                    Text(metaLine)
                        .font(.footnote)
                        .foregroundStyle(.white.opacity(0.7))
                        .lineLimit(1)
                }
            }
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("reels-info")
            if let caption, !caption.isEmpty { captionView(caption) }
        }
        .foregroundStyle(.white)
        .shadow(color: .black.opacity(0.55), radius: 3, y: 1)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(alignment: .bottom) {
            // 简介展开后可能往上压到画面：垫一层渐暗，字才看得清
            if expanded {
                LinearGradient(colors: [.clear, .black.opacity(0.75)], startPoint: .top, endPoint: .bottom)
                    .padding(.horizontal, -Theme.pagePadding)
                    .padding(.top, -40)
                    .allowsHitTesting(false)
            }
        }
    }

    /// 导演（剧集是主创）：头像 + 名字 + 身份，对应 Instagram「头像 · 作者名」那一行
    private var directorRow: some View {
        let people = item.title.directors
        let lead = people[0]
        let role = item.title.kind == "tv" ? "主创" : "导演"
        return Button {
            if let id = lead.tmdbPersonId { onOpenPerson(id) }
        } label: {
            HStack(spacing: 8) {
                RemoteImage(url: api.image(lead.avatarUrl), placeholderSymbol: "person.fill")
                    .frame(width: 24, height: 24)
                    .clipShape(Circle())
                    .overlay(Circle().stroke(.white.opacity(0.25), lineWidth: 0.5))
                Text(people.map(\.name).joined(separator: " / "))
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(1)
                Text(role)
                    .font(.subheadline)
                    .foregroundStyle(.white.opacity(0.65))
            }
        }
        .buttonStyle(.plain)
        .disabled(lead.tmdbPersonId == nil)
        .accessibilityLabel("\(role)：\(people.map(\.name).joined(separator: "、"))")
        .accessibilityIdentifier("reels-director")
    }

    /// 片名；剧集后面跟季集（小一号、弱一档）
    private var titleRow: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(item.title.name)
                .font(.headline.weight(.bold))
                .lineLimit(1)
            if let episode = item.title.episode {
                Text("第\(episode.season)季·第\(episode.episode)集")
                    .font(.subheadline)
                    .foregroundStyle(.white.opacity(0.7))
                    .lineLimit(1)
                    .fixedSize()
            }
        }
    }

    /// 年份 · 评分 · 类型（片长在进度线右边，不再重复）
    private var metaLine: String {
        var parts: [String] = []
        if let year = item.title.year { parts.append(String(year)) }
        if let rating = item.title.rating, rating > 0 { parts.append(String(format: "★ %.1f", rating)) }
        if !item.title.genres.isEmpty { parts.append(item.title.genres.prefix(2).joined(separator: " / ")) }
        return parts.joined(separator: " · ")
    }

    /// 简介：剧集前面放集名（「打个车吧｜……」），优先用分集简介
    private var caption: String? {
        let overview = item.title.episode?.overview ?? item.title.overview
        guard let name = item.title.episode?.name, !name.isEmpty else { return overview }
        guard let overview, !overview.isEmpty else { return name }
        return "\(name)｜\(overview)"
    }

    /// 收起时一行；放不下才在后面给「展开」（放得下就原样一行），点开最多六行
    @ViewBuilder
    private func captionView(_ text: String) -> some View {
        Group {
            if expanded {
                Text("\(text)  \(Text("收起").fontWeight(.semibold))")
                    .lineLimit(6)
            } else {
                ViewThatFits(in: .horizontal) {
                    Text(text).lineLimit(1).fixedSize()
                    HStack(spacing: 4) {
                        Text(text).lineLimit(1)
                        Text("展开").fontWeight(.semibold).fixedSize()
                    }
                }
            }
        }
        .font(.footnote)
        .foregroundStyle(.white.opacity(0.85))
        .contentShape(Rectangle())
        .onTapGesture { withAnimation(.easeInOut(duration: 0.2)) { expanded.toggle() } }
        .accessibilityLabel(text)
        .accessibilityHint(expanded ? "收起简介" : "展开简介")
        .accessibilityIdentifier("reels-caption")
    }
}

/// 细线是这一段的进度；右边的时间是「在整部片里放到哪 / 整部片多长」（剧集是这一集），
/// 让人知道这一段出自片子的什么位置。竖屏页与全屏共用
struct ReelProgressRow: View {
    let item: API.ReelItemView
    let player: ReelPlayer?

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.25)) { _ in
            HStack(spacing: 10) {
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(.white.opacity(0.22))
                        Capsule().fill(.white.opacity(0.9))
                            .frame(width: geo.size.width * (player?.progress ?? 0))
                    }
                }
                .frame(height: 2)
                Text(timeText)
                    .font(.caption2.weight(.medium).monospacedDigit())
                    .foregroundStyle(.white.opacity(0.78))
                    .shadow(color: .black.opacity(0.5), radius: 2, y: 1)
                    .fixedSize()
                    .accessibilityIdentifier("reels-time")
            }
        }
        .frame(height: 14)
        .allowsHitTesting(false)
    }

    private var timeText: String {
        let position = player?.position ?? Double(item.segment.startMs) / 1000
        let total = item.segment.durationMs.map { Double($0) / 1000 } ?? player?.core.duration
        guard let total, total > 0 else { return Self.clock(position, long: position >= 3600) }
        let long = total >= 3600
        return "\(Self.clock(position, long: long)) / \(Self.clock(total, long: long))"
    }

    /// 1:36:17 / 45:08：一小时以上的片子两边都带小时位，对齐不跳
    private static func clock(_ seconds: Double, long: Bool) -> String {
        let total = max(0, Int(seconds.rounded(.down)))
        let h = total / 3600, m = total % 3600 / 60, s = total % 60
        return long ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
    }
}

/// 刷片的画面容器：同一个引擎画面要在竖屏页的横带与全屏层之间来回挪。
///
/// 不能直接用播放器页的 `EngineSurface`：它每次更新都会把画面「抢」回自己的容器。进出全屏有一段
/// 过渡动画，新旧两个容器同时在——退出时全屏层在淡出途中又把画面抢回去，淡出结束画面跟着它一起被
/// 移走，横带就成了黑的（模拟器实测）。这里只在新建时接管画面；之后画面已经在别的容器里就不动它，
/// 只有画面无处可挂（没有父视图）时才接回来。
struct ReelVideoSurface: UIViewRepresentable {
    let engineView: UIView

    func makeUIView(context: Context) -> UIView {
        let container = UIView()
        container.backgroundColor = .black
        attach(to: container)
        return container
    }

    func updateUIView(_ container: UIView, context: Context) {
        if engineView.superview == nil { attach(to: container) }
    }

    private func attach(to container: UIView) {
        engineView.frame = container.bounds
        engineView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        container.addSubview(engineView)
    }
}

/// 画面正中的暂停 / 重播标记
struct ReelCenterGlyph: View {
    let symbol: String

    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: 36, weight: .semibold))
            .foregroundStyle(.white.opacity(0.92))
            .shadow(color: .black.opacity(0.5), radius: 8)
    }
}

/// 右下角的按钮：白色图标 + 小字，无底色（不用毛玻璃，和画面融在一起），带投影保证亮画面上也看得清
private struct ReelActionButton: View {
    let symbol: String
    let title: String
    var tint: Color = .white
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            ReelActionLabel(symbol: symbol, title: title, tint: tint)
        }
        .buttonStyle(.plain)
    }
}

private struct ReelActionLabel: View {
    let symbol: String
    let title: String
    let tint: Color

    var body: some View {
        VStack(spacing: 4) {
            Image(systemName: symbol)
                .font(.system(size: 26, weight: .regular))
                .frame(height: 30)
            Text(title)
                .font(.caption2.weight(.medium))
        }
        .foregroundStyle(tint)
        .shadow(color: .black.opacity(0.5), radius: 3, y: 1)
        .frame(width: 52)
        .contentShape(Rectangle())
    }
}
