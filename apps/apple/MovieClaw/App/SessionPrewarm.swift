import Foundation

/// 账号就绪的那一刻（冷启动秒开、登录、切换账号）为它预热各页面：在后台读出页面快照（`PageSnapshots`），
/// 媒体库首页、订阅首页（以及发现页海报的「已订阅」角标）第一帧就有上次的完整数据，随后各自静默刷新。
///
/// 放在 App 层：AppModel 只知道「哪台服务器上的哪个账号就绪了」，具体有哪些页面要预热由这里汇总。
enum SessionPrewarm {
    /// `landing`：冷启动时将要落在的页签。落在媒体库 / 订阅首页的，它的快照当场读完（几毫秒），
    /// 让第一帧就是完整页面；其余页面的快照在后台线程读，切过去之前早已读好
    static func start(server: ServerAddress, session: API.SessionView, landing: MainTab? = nil) {
        let username = session.username
        let owner = PageSnapshots.owner(server: server, username: username)
        let api = APIClient(server: server)
        let canSubscribe = Permissions(session: session).canSubscribe
        // 播放器按网络环境记画质：服务器配的是域名时先在后台查好地址，第一次播放就判得出在家还是在外面
        PlaybackNetwork.prewarm(server: server)
        // 暂停时要不要连下载也停按网络是否计费定：先开始监听，第一次播放时已经有结果
        _ = NetworkCost.shared
        DiscoverSnapshots.adopt(owner: owner, synchronously: landing == .discover)
        var reads = [LibraryHomeStore.shared.adopt(owner: owner, synchronously: landing == .library)]
        if canSubscribe {
            reads.append(SubscriptionIndex.shared.adopt(api: api, owner: username, synchronously: landing == .subscriptions))
            reads.append(SubscriptionsHomeFeed.shared.adopt(owner: owner, synchronously: landing == .subscriptions))
        }
        // 其余页面：快照读好就把它们的首屏图片低优先级解码进内存（后台线程），
        // 启动后马上切过去也不会先出占位底、再渐显
        let others: [MainTab] = [.library, .subscriptions, .discover].filter { $0 != landing && ($0 != .subscriptions || canSubscribe) }
        let pending = reads.compactMap { $0 }
        Task {
            for read in pending { await read.value }
            await FirstFrameGate.wait()
            warmImages(for: others, api: api)
        }
        // 落点页面的首屏图片马上开始从磁盘解码进内存，和主界面的搭建同时进行（见 FirstScreenImages）
        switch landing {
        case .discover:
            let feed = DiscoverFeed(mediaType: "movie", provider: "tmdb")
            FirstScreenImages.warm(feed.firstScreenImageURLs(api: api), urgent: true)
        case .library:
            FirstScreenImages.warm(LibraryHomeStore.shared.firstScreenImageURLs(api: api), urgent: true)
        case .subscriptions:
            if let subscriptions = SubscriptionIndex.shared.subscriptions {
                FirstScreenImages.warm(SubscriptionsHomeFeed.shared.firstScreenImageURLs(subscriptions: subscriptions, api: api), urgent: true)
            }
        default:
            break
        }
    }

    /// 还没打开的页签：数据刷新好之后，把它们的首屏图片也低优先级解码进内存，第一次切过去就是图文齐全的样子
    static func warmImages(for tabs: [MainTab], api: APIClient) {
        for tab in tabs {
            switch tab {
            case .library:
                FirstScreenImages.warm(LibraryHomeStore.shared.firstScreenImageURLs(api: api), urgent: false)
            case .subscriptions:
                if let subscriptions = SubscriptionIndex.shared.subscriptions {
                    FirstScreenImages.warm(SubscriptionsHomeFeed.shared.firstScreenImageURLs(subscriptions: subscriptions, api: api), urgent: false)
                }
            case .discover:
                FirstScreenImages.warm(DiscoverFeed(mediaType: "movie", provider: "tmdb").firstScreenImageURLs(api: api), urgent: false)
            default:
                break
            }
        }
    }

    /// 冷启动的落点（与 MainTabView.land 同一口径）：都落「媒体库」（首页）；调试参数可指定
    static func landingTab(for session: API.SessionView) -> MainTab {
        #if DEBUG
        if let tab = DebugLaunch.tab { return tab }
        #endif
        return .library
    }

    /// 账号退出 / 被移除：连同它的页面快照与会话快照一起删掉
    static func forget(server: ServerAddress, username: String) {
        PageSnapshots.remove(owner: PageSnapshots.owner(server: server, username: username))
        SessionCache.remove(server: server, username: username)
    }
}
