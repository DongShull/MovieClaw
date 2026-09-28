import Foundation
import Testing
@testable import MovieClaw

/// 秒开用的本机快照：会话快照（冷启动直接进主界面）与页面快照（媒体库 / 订阅首页先画上次的数据）。
/// 两者都按「服务器 + 账号」隔离，退出时删除——串到别的账号上就是数据泄露。
@MainActor
struct PageSnapshotTests {
    private let nas = ServerAddress(origin: URL(string: "http://192.168.1.10:3000")!)
    private let office = ServerAddress(origin: URL(string: "https://movie.example.com")!)

    private func session(_ username: String, role: String = "member") -> API.SessionView {
        API.SessionView(
            username: username, nickname: username.uppercased(), avatarUrl: nil, role: role,
            capabilities: API.SessionCapabilities(allowSubscribe: true, allowSearch: true, allowDirectDownload: false),
            device: nil
        )
    }

    @Test func sessionCacheIsPerServerAndAccount() {
        let name = "snap-test-\(UUID().uuidString.prefix(8))"
        defer {
            SessionCache.remove(server: nas, username: name)
            SessionCache.remove(server: office, username: name)
        }
        SessionCache.save(session(name, role: "admin"), server: nas)
        #expect(SessionCache.load(server: nas, username: name)?.role == "admin")
        #expect(SessionCache.load(server: nas, username: name.uppercased())?.role == "admin", "用户名不区分大小写，同 TokenVault")
        #expect(SessionCache.load(server: office, username: name) == nil, "另一台服务器上的同名账号不共用")
        #expect(SessionCache.load(server: nas, username: name + "x") == nil)

        SessionCache.remove(server: nas, username: name)
        #expect(SessionCache.load(server: nas, username: name) == nil, "退出后不能再用快照直接进主界面")
    }

    @Test func pageSnapshotRoundTripAndOwnerIsolation() async throws {
        let owner = PageSnapshots.owner(server: nas, username: "snap-\(UUID().uuidString.prefix(8))")
        let other = PageSnapshots.owner(server: office, username: "someone")
        defer { PageSnapshots.remove(owner: owner) }
        let value = ["a", "b", "c"]
        PageSnapshots.write(value, "unit-test", owner: owner)
        // 写盘在后台队列里异步完成
        var read: [String]?
        for _ in 0 ..< 50 {
            read = PageSnapshots.read([String].self, "unit-test", owner: owner)
            if read != nil { break }
            try await Task.sleep(for: .milliseconds(20))
        }
        #expect(read == value)
        #expect(PageSnapshots.read([String].self, "unit-test", owner: other) == nil, "别的账号读不到")
        #expect(PageSnapshots.read([Int].self, "unit-test", owner: owner) == nil, "结构对不上（App 升级改了模型）当作没有")
    }

    @Test func memoizedDateParsingMatchesFormatter() {
        let withZone = "2026-09-27T10:20:30.123456+00:00"
        let noZone = "2026-09-27T10:20:30"
        let first = Formatters.date(withZone)
        #expect(first != nil)
        #expect(Formatters.date(withZone) == first, "缓存命中与首次解析一致")
        #expect(Formatters.date(noZone) == ISO8601DateFormatter().date(from: noZone + "Z"), "不带时区按 UTC")
        #expect(Formatters.date("not a date") == nil)
        #expect(Formatters.date("not a date") == nil, "解析不了的也缓存成 nil")
    }
}
