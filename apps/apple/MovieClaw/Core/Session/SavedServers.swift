import Foundation

/// 本机登录过的一台服务器，以及它上面已登录账号的本地快照。
///
/// 登录态本身在服务器那边：每台服务器给本机发一个 Cookie 账号袋（最多 5 个账号，见 docs/design/account-switching.md），
/// Cookie 按主机隔离，所以「多台服务器 × 各自多个账号」天然互不干扰。App 这边只记两样东西：
/// - 有哪些服务器（地址 + 最近使用时间）：冷启动时据此把每台的 Cookie 从钥匙串灌回来；
/// - 每台上有哪些账号（昵称、头像、角色）：列账号的接口要那台服务器在线、且那边的当前账号没过期，
///   连不上或过期时，切换账号列表和欢迎页「选择账号」照样要能把人列出来。
///   快照只用于展示，每次成功取到 `/auth/accounts` 就整体覆盖；真正能不能切，以切换接口的结果为准。
nonisolated struct SavedServer: Codable, Hashable, Sendable, Identifiable {
    var address: ServerAddress
    var accounts: [API.AccountView]
    var lastUsed: Date

    var id: URL { address.origin }

    /// 快照里标为「当前」的账号：冷启动发现会话过期时，据此预填用户名
    var activeAccount: API.AccountView? { accounts.first(where: \.active) ?? accounts.first }
}

/// 服务器记录的读写与增删（纯函数部分有单元测试）
nonisolated enum SavedServers {
    private static let key = "movieclaw.savedServers"

    static func load() -> [SavedServer] {
        guard let data = UserDefaults.standard.data(forKey: key),
              let list = try? JSONDecoder().decode([SavedServer].self, from: data)
        else { return [] }
        return list
    }

    static func save(_ list: [SavedServer]) {
        if let data = try? JSONEncoder().encode(list) {
            UserDefaults.standard.set(data, forKey: key)
        }
    }

    static func clearAll() {
        UserDefaults.standard.removeObject(forKey: key)
    }

    /// 记一次使用：这台服务器置顶；给了账号列表就一并覆盖快照（`nil` 表示只更新时间）
    static func touching(_ list: [SavedServer], _ address: ServerAddress, accounts: [API.AccountView]?, at date: Date = .now) -> [SavedServer] {
        var result = list
        if let index = result.firstIndex(where: { $0.address == address }) {
            result[index].lastUsed = date
            if let accounts { result[index].accounts = accounts }
        } else {
            result.append(SavedServer(address: address, accounts: accounts ?? [], lastUsed: date))
        }
        return result.sorted { $0.lastUsed > $1.lastUsed }
    }

    /// 只覆盖某台服务器的账号快照，不动排序（后台刷新别的服务器时用）
    static func replacingAccounts(_ list: [SavedServer], _ address: ServerAddress, with accounts: [API.AccountView]) -> [SavedServer] {
        list.map { $0.address == address ? SavedServer(address: $0.address, accounts: accounts, lastUsed: $0.lastUsed) : $0 }
    }

    /// 从某台服务器的快照里去掉一个账号
    static func removingAccount(_ list: [SavedServer], _ username: String, from address: ServerAddress) -> [SavedServer] {
        list.map { saved in
            guard saved.address == address else { return saved }
            var copy = saved
            copy.accounts.removeAll { $0.username == username }
            return copy
        }
    }

    /// 没有账号、也不是当前服务器的记录不再保留：它既不会出现在账号列表里，也没有 Cookie 需要恢复
    static func pruned(_ list: [SavedServer], keeping current: ServerAddress?) -> [SavedServer] {
        list.filter { !$0.accounts.isEmpty || $0.address == current }
    }
}

/// 某台服务器上的一个已登录账号：跨服务器的账号列表（欢迎页「选择账号」）用
nonisolated struct SavedAccount: Identifiable, Hashable, Sendable {
    let server: ServerAddress
    let account: API.AccountView

    var id: String { "\(server.origin.absoluteString)#\(account.username)" }
}
