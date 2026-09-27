import Foundation
import Observation

/// App 顶层状态机：决定此刻显示欢迎页还是主界面，并管理本机登录过的全部服务器与账号。
///
/// **多服务器 × 多账号**：每台服务器给本机发一个 Cookie 账号袋（最多 5 个账号，见 docs/design/account-switching.md），
/// Cookie 按主机隔离，所以登到几台服务器都互不干扰。「当前」只有一个：`server` 加上它那边的激活账号（`.ready`）。
/// 本机登录过哪些服务器、各有哪些账号记在 `savedServers`（见 `SavedServer`），切换账号列表与欢迎页「选择账号」都读它。
///
/// **欢迎页的几种状态**（都渲染同一个 `WelcomeView`，状态之间切换时表单里填的内容不丢）：
/// - `.needsServer`：本机从没登录过任何服务器 → 放片头，再给「服务器地址 + 账号密码」一张卡片；
/// - `.needsSetup`：服务器是全新的 → 同一张卡片补一个确认密码，创建超级管理员（同 Web /setup）；
/// - `.needsLogin`：要输密码——当前账号登录过期（`expiredUsername` 预填用户名），或账号全部退出后（预填最近的服务器）；
/// - `.chooseAccount`：当前服务器上没有登录中的账号了，但别的服务器上还有 → 先让用户选一个，一点即进，不用输密码；
/// - `.unreachable`：冷启动连不上当前服务器 → 重试 / 改地址 / 换账号。登录态其实还在，服务器恢复后点「重试」就能进。
///
/// **登录**一步做完：测通服务器 → 查 `/auth/bootstrap` → 已初始化就直接登录（`signIn`），全部成功才记下服务器并进入 `.ready`；
/// 中途任何一步失败都不落盘。
///
/// 任意接口 401（会话过期、被踢下线）经 `.apiUnauthorized` 通知把状态打回 `.needsLogin`，对应 Web 端全站的 401 跳登录页兜底；
/// 只认当前服务器的 401——切换账号时会去问别的服务器，它们的 401 与当前会话无关。
@Observable
final class AppModel {
    enum Phase: Equatable {
        /// 启动时正在恢复上次的服务器与会话
        case launching
        case needsServer
        case needsSetup
        case needsLogin
        case chooseAccount
        case unreachable
        case ready(API.SessionView)
    }

    private(set) var phase: Phase = .launching
    /// 当前服务器；为空表示本机还没登录过任何服务器
    private(set) var server: ServerAddress?
    /// 本机登录过的服务器，最近用过的在前
    private(set) var savedServers: [SavedServer] = []
    /// 连不上服务器的原因：「连不上」卡片与登录卡片顶部的提示
    var launchError: String?
    /// 登录过期的账号：欢迎页据此预填用户名、提示「登录已过期」；重新登录或换了账号后清空
    private(set) var expiredUsername: String?
    /// 自动换了账号时要告诉用户的一句话（「已退出 A，已切换到 B」）。换账号后主界面整棵重建，
    /// 提示条也跟着重建，当场弹的提示会丢，所以先存在这里，新的主界面出现时取走弹出来
    private(set) var pendingNotice: String?

    var api: APIClient? { server.map { APIClient(server: $0) } }

    var session: API.SessionView? {
        if case let .ready(session) = phase { return session }
        return nil
    }

    /// 别的服务器上已登录的账号（当前服务器除外）：欢迎页「选择账号」与「切换到其他账号」入口用。
    /// 当前服务器上的不算——到了欢迎页，说明当前服务器上已经没有能直接切换的账号了（都退出了，或当前账号过期、
    /// 切换接口要求登录态也用不了）
    var accountsOnOtherServers: [SavedAccount] {
        savedServers.filter { $0.address != server }.flatMap { saved in
            saved.accounts.map { SavedAccount(server: saved.address, account: $0) }
        }
    }

    /// 本机登录过的账号总数（所有服务器）：「退出全部账号」的确认文案用
    var savedAccountCount: Int { savedServers.reduce(0) { $0 + $1.accounts.count } }

    /// 会话过期时的浏览位置（Web 401 → `/login?next=原路径`）：重新登录后回到这里。
    /// 只记当前标签和它的导航栈；落地前按新身份的权限再过滤一遍（同 Web accessiblePathFor）。
    struct ResumePoint {
        var tab: MainTab
        var path: [AppRoute]
    }

    private var resumePoint: ResumePoint?
    /// 刚因 401 被打回登录页：主界面拆掉时据此决定要不要记下位置（主动退出、切换账号不记）
    private var expiredPendingCapture = false

    private static let serverKey = "movieclaw.server.origin"
    private var unauthorizedObserver: (any NSObjectProtocol)?

    init() {
        // UI 自动化测试用：以全新安装的状态启动（清掉服务器记录与全部 Cookie）
        if ProcessInfo.processInfo.arguments.contains("--reset-state") {
            UserDefaults.standard.removeObject(forKey: Self.serverKey)
            SavedServers.clearAll()
            HTTPCookieStorage.shared.removeCookies(since: .distantPast)
            CookieVault.clearAll()
        }
        var saved = SavedServers.load()
        if let origin = UserDefaults.standard.url(forKey: Self.serverKey) {
            let address = ServerAddress(origin: origin)
            server = address
            // 早先的版本只记了一台服务器、没有账号快照：补一条记录，首次进入后再取账号列表填上
            if !saved.contains(where: { $0.address == address }) {
                saved = SavedServers.touching(saved, address, accounts: nil)
            }
        }
        savedServers = saved
        for record in saved {
            CookieVault.restore(for: record.address)
        }
        unauthorizedObserver = NotificationCenter.default.addObserver(
            forName: .apiUnauthorized, object: nil, queue: .main
        ) { [weak self] note in
            let target = note.object as? ServerAddress
            MainActor.assumeIsolated { self?.sessionExpired(on: target) }
        }
    }

    // MARK: - 启动与重连

    /// 冷启动：有服务器就尝试恢复会话，否则进入欢迎页片头。
    func restore() async {
        #if DEBUG
        if let raw = DebugLaunch.server, let address = try? ServerAddress(parsing: raw) {
            // 上次的会话仍有效就沿用，免得每次调试启动都在服务器上多开一个会话
            CookieVault.restore(for: address)
            if let session = try? await APIClient(server: address).authMe() {
                activate(address, session: session)
                return
            }
            if let user = DebugLaunch.username, let pass = DebugLaunch.password,
               (try? await signIn(to: address, username: user, password: pass, remember: true)) == .signedIn {
                return
            }
        }
        #endif
        guard server != nil else {
            phase = .needsServer
            return
        }
        await reconnect()
    }

    /// 连当前服务器、恢复它上面的会话：冷启动与「连不上」卡片的「重试」共用
    func reconnect() async {
        guard let server else { return }
        let api = APIClient(server: server)
        do {
            let status = try await api.bootstrapStatus()
            guard status.initialized else {
                phase = .needsSetup
                return
            }
            activate(server, session: try await api.authMe())
        } catch let error as APIError where error.isUnauthorized {
            // 当前账号登录过期：预填快照里它的用户名，让用户只输密码
            launchError = nil
            expiredUsername = savedServers.first { $0.address == server }?.activeAccount?.username
            phase = .needsLogin
        } catch {
            // 服务器连不上：不当成「要重新登录」——登录态还在，恢复后点重试就能进
            launchError = error.localizedDescription
            phase = .unreachable
        }
    }

    // MARK: - 登录

    /// 一步登录的结果：登录成功，或服务器是全新的、需要先创建超级管理员
    enum SignInResult: Equatable {
        case signedIn
        case needsSetup
    }

    /// 欢迎页与「添加账号」的登录：测通服务器后直接用账号密码登录，全部成功才记下服务器并进入主界面。
    /// 可以是另一台服务器——登录成功它就成为当前服务器，原来那台的账号照样留在本机，随时切回去。
    /// 服务器尚未初始化时不登录，返回 `.needsSetup`，由界面补一次确认密码后调 `createAdmin(on:)`。
    func signIn(to address: ServerAddress, username: String, password: String, remember: Bool) async throws -> SignInResult {
        let api = APIClient(server: address)
        guard try await Self.probe(api) else { return .needsSetup }
        let session = try await api.authLogin(body: .init(username: username, password: password, remember: remember))
        activate(address, session: session)
        return .signedIn
    }

    /// 全新服务器：用欢迎页填的账号创建超级管理员并直接进入（全生命周期仅一次）
    func createAdmin(on address: ServerAddress, username: String, password: String) async throws {
        let api = APIClient(server: address)
        do {
            let session = try await api.authBootstrapCreate(body: .init(username: username, password: password))
            activate(address, session: session)
        } catch let error as APIError where error.status == 409 {
            // 一次性初始化锁已闭合（别的设备/浏览器先一步完成了初始化）：界面据此退回普通登录
            throw ConnectError.alreadyInitialized
        }
    }

    /// 测通服务器：确认地址上跑的是健康的 MovieClaw，返回它是否已完成初始化
    private static func probe(_ api: APIClient) async throws -> Bool {
        let health: API.HealthResponse
        do {
            health = try await api.health()
        } catch APIError.decoding {
            throw ConnectError.notMovieClaw
        } catch let error as APIError where error.status == 404 {
            throw ConnectError.notMovieClaw
        }
        guard health.status == "ok" else { throw ConnectError.unhealthy(health.status) }
        return try await api.bootstrapStatus().initialized
    }

    // MARK: - 切换账号

    /// 切到某台服务器上一个已登录的账号（可以跨服务器），不用输密码。
    /// 那个账号的登录已过期（404）、或那台服务器上当前账号已过期（401，切换接口要求登录态）时
    /// 抛 `AccountError.needsPassword`，界面据此打开登录卡片，服务器与用户名都预填好。
    func switchAccount(to username: String, on address: ServerAddress) async throws {
        do {
            let session = try await APIClient(server: address).authAccountsSwitch(body: .init(username: username))
            resumePoint = nil
            activate(address, session: session)
        } catch let error as APIError where error.status == 404 || error.isUnauthorized {
            if error.status == 404 {
                // 账号袋里已经没有它了：快照里也去掉
                savedServers = SavedServers.removingAccount(savedServers, username, from: address)
                persist()
            }
            throw AccountError.needsPassword(server: address, username: username)
        }
    }

    /// 从本机移除一个账号（那台服务器的账号袋里去掉它，账号本身不受影响）。
    /// 移除的是当前账号时处理同「退出登录」，返回自动切到的会话。
    /// 那台服务器连不上、或那边的当前账号已过期时只从本机记录里去掉；它是那台上的最后一个账号，就连 Cookie 一起清掉
    @discardableResult
    func removeAccount(_ username: String, on address: ServerAddress) async throws -> API.SessionView? {
        let isCurrent = address == server && session?.username == username
        do {
            let next = try await APIClient(server: address).authAccountsRemove(username: username)
            savedServers = SavedServers.removingAccount(savedServers, username, from: address)
            persist()
            if isCurrent {
                if let next {
                    pendingNotice = "已移除「\(username)」，已切换到「\(next.nickname)」"
                    activate(address, session: next)
                } else {
                    leaveCurrentServer()
                }
                return next
            }
            _ = try? await refreshAccounts(on: address)
            return nil
        } catch let error as APIError where !isCurrent && (error.status == nil || error.isUnauthorized) {
            // 那台服务器连不上，或那边的当前账号已过期（接口要求登录态）：在本机去掉就是了
            forgetLocally(username, on: address)
            return nil
        }
    }

    /// 取某台服务器上本机已登录的账号，顺手更新快照。连不上、或那边当前账号已过期时抛错，快照保持原样
    @discardableResult
    func refreshAccounts(on address: ServerAddress) async throws -> [API.AccountView] {
        let list = try await APIClient(server: address).authAccountsList()
        savedServers = SavedServers.replacingAccounts(savedServers, address, with: list)
        persist()
        return list
    }

    /// 改昵称、换头像后同步全局会话
    func update(session: API.SessionView) {
        phase = .ready(session)
        if let server { Task { try? await refreshAccounts(on: server) } }
    }

    // MARK: - 退出

    /// 退出当前账号。同一台服务器上还有别的账号就自动切过去（与网页一致），返回切到的会话；
    /// 这台服务器上没有了：别的服务器上还有账号 → 欢迎页「选择账号」，一个都没有 → 欢迎页登录卡片（预填这台服务器）。
    @discardableResult
    func logout() async -> API.SessionView? {
        resumePoint = nil
        guard let server else { return nil }
        let leaving = session?.nickname
        do {
            if let next = try await APIClient(server: server).authLogout(body: .init(all: false)) {
                // 退出后人还在 App 里，很容易以为没退成：明确说出现在换成了谁
                pendingNotice = leaving.map { "已退出「\($0)」，已切换到「\(next.nickname)」" } ?? "已切换到「\(next.nickname)」"
                activate(server, session: next)
                return next
            }
        } catch {
            // 退出接口没调通（服务器连不上）：在本机清掉这台服务器的 Cookie，
            // 不能出现「以为退出了，下次打开又自动登录」
            CookieVault.clear(for: server)
        }
        leaveCurrentServer()
        return nil
    }

    /// 退出本机全部账号（所有服务器上的），共用设备交还前用。
    /// 登录态只在 Cookie 里（退出接口本身也只是改写 Cookie），所以直接清掉本机每台服务器的 Cookie，
    /// 服务器连不上也能退干净；之后回到欢迎页登录卡片，预填最近用的服务器。
    func logoutEverywhere() {
        resumePoint = nil
        for record in savedServers {
            CookieVault.clear(for: record.address)
        }
        let emptied = savedServers.map { SavedServer(address: $0.address, accounts: [], lastUsed: $0.lastUsed) }
        savedServers = SavedServers.pruned(emptied, keeping: server)
        persist()
        expiredUsername = nil
        phase = .needsLogin
    }

    /// 当前服务器上已经没有登录中的账号：清空它的快照，去欢迎页
    private func leaveCurrentServer() {
        guard let server else { return }
        savedServers = SavedServers.replacingAccounts(savedServers, server, with: [])
        persist()
        expiredUsername = nil
        phase = accountsOnOtherServers.isEmpty ? .needsLogin : .chooseAccount
    }

    /// 连不上的服务器上移除账号：只从本机记录里去掉；它是那台上的最后一个账号就连 Cookie 与记录一起清掉
    private func forgetLocally(_ username: String, on address: ServerAddress) {
        savedServers = SavedServers.removingAccount(savedServers, username, from: address)
        if savedServers.first(where: { $0.address == address })?.accounts.isEmpty ?? true {
            CookieVault.clear(for: address)
            savedServers = SavedServers.pruned(savedServers, keeping: server)
        }
        persist()
    }

    // MARK: - 内部

    /// 进入某台服务器上的某个账号：记下服务器（置顶）、清掉过期 / 连不上的提示，再在后台刷新这台服务器的账号快照
    private func activate(_ address: ServerAddress, session: API.SessionView) {
        server = address
        UserDefaults.standard.set(address.origin, forKey: Self.serverKey)
        launchError = nil
        expiredUsername = nil
        // 置顶这台；顺手清掉已经一个账号都不剩的旧服务器（比如刚在那台上退出了最后一个账号再切过来）
        savedServers = SavedServers.pruned(SavedServers.touching(savedServers, address, accounts: nil), keeping: address)
        persist()
        phase = .ready(session)
        Task { try? await refreshAccounts(on: address) }
    }

    private func persist() {
        SavedServers.save(savedServers)
    }

    private func sessionExpired(on target: ServerAddress?) {
        guard case let .ready(current) = phase, target == nil || target == server else { return }
        expiredPendingCapture = true
        expiredUsername = current.username
        phase = .needsLogin
    }

    /// 主界面拆掉时调用：只有因会话过期离开才记下位置
    func captureResume(tab: MainTab, path: [AppRoute]) {
        guard expiredPendingCapture else { return }
        expiredPendingCapture = false
        resumePoint = ResumePoint(tab: tab, path: path)
    }

    /// 取走（并清空）待弹出的提示
    func takeNotice() -> String? {
        defer { pendingNotice = nil }
        return pendingNotice
    }

    /// 取走（并清空）待还原的位置
    func takeResume() -> ResumePoint? {
        defer { resumePoint = nil }
        return resumePoint
    }

    enum ConnectError: LocalizedError {
        case notMovieClaw
        case unhealthy(String)
        case alreadyInitialized

        var errorDescription: String? {
            switch self {
            case .notMovieClaw: "该地址能访问，但不是 MovieClaw 服务器（请填写浏览器打开 MovieClaw 时地址栏里的地址）"
            case let .unhealthy(status): "服务器状态异常：\(status)"
            case .alreadyInitialized: "这台服务器刚刚已在别处完成初始化，请用已有的账号登录"
            }
        }
    }

    enum AccountError: LocalizedError {
        /// 切不过去，要重新输密码
        case needsPassword(server: ServerAddress, username: String)

        var errorDescription: String? {
            switch self {
            case let .needsPassword(_, username): "「\(username)」的登录已过期，请重新输入密码"
            }
        }
    }
}
