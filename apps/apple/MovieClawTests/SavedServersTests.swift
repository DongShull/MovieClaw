import Foundation
import Testing
@testable import MovieClaw

/// 本机服务器记录的增删：多服务器切换账号、退出、清理都靠这几个纯函数
struct SavedServersTests {
    private let nas = ServerAddress(origin: URL(string: "http://192.168.1.10:3000")!)
    private let office = ServerAddress(origin: URL(string: "https://movie.example.com")!)

    private func account(_ username: String, active: Bool = false) -> API.AccountView {
        API.AccountView(username: username, nickname: username.uppercased(), avatarUrl: nil, role: "member", active: active)
    }

    @Test func touchingInsertsAndMovesToFront() {
        var list = SavedServers.touching([], nas, accounts: [account("a")], at: Date(timeIntervalSince1970: 1))
        list = SavedServers.touching(list, office, accounts: nil, at: Date(timeIntervalSince1970: 2))
        #expect(list.map(\.address) == [office, nas], "最近用的在前")
        #expect(list[1].accounts.map(\.username) == ["a"])

        // 再用一次 NAS：置顶，没给账号列表就保留原快照
        list = SavedServers.touching(list, nas, accounts: nil, at: Date(timeIntervalSince1970: 3))
        #expect(list.map(\.address) == [nas, office])
        #expect(list[0].accounts.map(\.username) == ["a"])
    }

    @Test func replacingAccountsKeepsOrder() {
        let list = [
            SavedServer(address: office, accounts: [], lastUsed: Date(timeIntervalSince1970: 2)),
            SavedServer(address: nas, accounts: [account("a")], lastUsed: Date(timeIntervalSince1970: 1)),
        ]
        let replaced = SavedServers.replacingAccounts(list, nas, with: [account("b"), account("c")])
        #expect(replaced.map(\.address) == [office, nas], "后台刷新不改变排序")
        #expect(replaced[1].accounts.map(\.username) == ["b", "c"])
    }

    @Test func removingAccountOnlyTouchesThatServer() {
        let list = [
            SavedServer(address: nas, accounts: [account("a"), account("b")], lastUsed: .now),
            SavedServer(address: office, accounts: [account("a")], lastUsed: .now),
        ]
        let removed = SavedServers.removingAccount(list, "a", from: nas)
        #expect(removed[0].accounts.map(\.username) == ["b"])
        #expect(removed[1].accounts.map(\.username) == ["a"], "另一台服务器上的同名账号不受影响")
    }

    @Test func prunedDropsEmptyServersExceptCurrent() {
        let list = [
            SavedServer(address: nas, accounts: [], lastUsed: .now),
            SavedServer(address: office, accounts: [], lastUsed: .now),
        ]
        #expect(SavedServers.pruned(list, keeping: nas).map(\.address) == [nas])
        #expect(SavedServers.pruned(list, keeping: nil).isEmpty)
    }

    /// 冷启动发现会话过期时，按快照里的「当前」账号预填用户名
    @Test func activeAccountPrefersActiveFlag() {
        let saved = SavedServer(address: nas, accounts: [account("a"), account("b", active: true)], lastUsed: .now)
        #expect(saved.activeAccount?.username == "b")
        let noFlag = SavedServer(address: nas, accounts: [account("a")], lastUsed: .now)
        #expect(noFlag.activeAccount?.username == "a")
    }

    @Test func roundTripsThroughJSON() throws {
        let list = [SavedServer(address: nas, accounts: [account("a", active: true)], lastUsed: Date(timeIntervalSince1970: 100))]
        let decoded = try JSONDecoder().decode([SavedServer].self, from: JSONEncoder().encode(list))
        #expect(decoded == list)
    }

    @Test func hostLabelShowsHostAndPort() {
        #expect(nas.hostLabel == "192.168.1.10:3000")
        #expect(office.hostLabel == "movie.example.com")
    }
}
