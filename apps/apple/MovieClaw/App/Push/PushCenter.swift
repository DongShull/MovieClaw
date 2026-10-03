import os
import UIKit
import UserNotifications

/// App 推送的 App 这一侧（docs/design/cloud-push.md §9）：通知权限、APNs 令牌、给本机每个登录登记推送、点开通知。
///
/// - **权限**：新登录（账号密码登录、创建管理员、配对码登录、添加账号）后马上请求——推送不止订阅，
///   还有账号安全、管理员告警，等第一次订阅再问就晚了。系统只会弹一次，之后每次启动只读取当前状态。
/// - **登记**：给本机保存的每一个登录（每台服务器 × 每个账号）各登记一次：每个登录一把自己的密钥
///   （`PushKeyStore`），用那个登录自己的设备令牌调 `PUT /push/me/registration`。每次启动、每次新登录、
///   APNs 令牌变化、回到前台时权限变了都重新登记，内容没变的不重复发。用户关掉了通知、拿不到 APNs 令牌
///   （模拟器、没有推送权限的侧载包）时只上报权限状态。登记失败只记日志，不打扰用户。
/// - **点开**：按 `key_id` 认出是哪个登录，记进 `pendingTap`；主界面出来后由它切账号、打开 `open`
///   （见 MainTabView.openPushTarget）。人在欢迎页时不跳转（见 RootView）。
@Observable
final class PushCenter: NSObject {
    static let shared = PushCenter()

    /// 系统通知权限
    private(set) var permission: PushPermission = .notDetermined
    /// 点开了、还没处理完的通知：冷启动时主界面还没出来；要先切账号时，等主界面按新账号重建后接着处理
    var pendingTap: PushTapTarget?
    /// 每登记完一轮加一：「通知」页据此刷新「我的设备」
    private(set) var registrationRound = 0

    @ObservationIgnored private var apnsToken: String?
    @ObservationIgnored private var syncing = false
    @ObservationIgnored private var syncAgain = false
    /// 这次运行里每个登录最近一次登记成功的内容（设备令牌 + 登记内容的摘要）：没变就不重复登记
    @ObservationIgnored private var registered: [String: Int] = [:]
    @ObservationIgnored private var foregroundObserver: (any NSObjectProtocol)?

    private nonisolated static let log = Logger(subsystem: "io.movieclaw.push", category: "registration")

    // MARK: - 启动

    /// App 启动时调用（AppDelegate 的 didFinishLaunching 返回前：点通知冷启动时，系统要在那之前拿到代理）
    func start() {
        UNUserNotificationCenter.current().delegate = self
        foregroundObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.willEnterForegroundNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.returnedToForeground() }
        }
        Task {
            await refreshPermission()
            // 每次启动都向 APNs 注册（不弹任何框）：令牌可能变了；拒绝了通知也照样拿，以后打开就能直接收到
            UIApplication.shared.registerForRemoteNotifications()
        }
    }

    func didRegister(deviceToken: Data) {
        apnsToken = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task { await sync() }
    }

    /// 模拟器、没有推送权限的包拿不到令牌：只上报权限
    func didFailToRegister(_ error: any Error) {
        Self.log.info("APNs 注册失败：\(error.localizedDescription, privacy: .public)")
        apnsToken = nil
        Task { await sync() }
    }

    /// 这次启动里问过权限没有（新登录、或进入主界面时补问）
    @ObservationIgnored private var askedThisLaunch = false

    /// 新登录之后（见 AppModel.freshLogins）：请求通知权限，再给新登录登记
    func didLogIn() {
        askedThisLaunch = true
        Task { await requestAuthorization(prompt: Self.promptAllowed) }
    }

    /// 进入主界面（本机有登录中的账号）：升级前就登录着的人从没被问过，这次启动补问一次。
    /// 系统只在还没问过时弹框，问过的直接返回现状
    func sessionReady() {
        guard !askedThisLaunch else { return }
        askedThisLaunch = true
        Task { await requestAuthorization(prompt: Self.promptAllowed) }
    }

    /// 请求通知权限（提醒、声音、角标；系统只在还没问过时弹框），然后向 APNs 注册、给本机每个登录登记。
    /// 不管允不允许都注册：拒绝了令牌照样有用，以后在系统设置里打开就能直接收到
    func requestAuthorization(prompt: Bool = true) async {
        await refreshPermission()
        if prompt, permission == .notDetermined {
            do {
                _ = try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])
            } catch {
                Self.log.info("请求通知权限失败：\(error.localizedDescription, privacy: .public)")
            }
            await refreshPermission()
        }
        UIApplication.shared.registerForRemoteNotifications()
        await sync()
    }

    /// 回到前台：权限在系统设置里改过就重新登记（允许了要带上令牌，关掉了要告诉服务器别再发）
    private func returnedToForeground() {
        Task {
            let before = permission
            await refreshPermission()
            guard permission != before else { return }
            UIApplication.shared.registerForRemoteNotifications()
            await sync()
        }
    }

    private func refreshPermission() async {
        let status = await withCheckedContinuation { continuation in
            UNUserNotificationCenter.current().getNotificationSettings { continuation.resume(returning: $0.authorizationStatus) }
        }
        let current = PushPermission(status)
        if current != permission { permission = current }
    }

    // MARK: - 登记

    /// 给本机每个登录登记一次。正在登记时又来的请求合并成「这轮完了再来一轮」
    func sync() async {
        guard !syncing else {
            syncAgain = true
            return
        }
        syncing = true
        defer { syncing = false }
        // 冷启动先让第一帧上屏，不和落地页抢主线程与连接（见 FirstFrameGate）
        await FirstFrameGate.wait()
        repeat {
            syncAgain = false
            await registerAll()
        } while syncAgain
        registrationRound += 1
    }

    private func registerAll() async {
        let store = PushKeyStore.shared
        var jobs: [Job] = []
        for login in Self.savedLogins() {
            let body = API.PushRegistrationRequest.make(permission: permission, apnsToken: apnsToken, topic: Self.topic,
                                                        environment: Self.environment) { store.ensureKey(for: login.info) }
            var hasher = Hasher()
            hasher.combine(login.token)
            hasher.combine(body)
            let fingerprint = hasher.finalize()
            guard registered[login.info.login.account] != fingerprint else { continue }
            jobs.append(Job(account: login.info.login.account, host: login.server.hostLabel,
                            client: APIClient(server: login.server, token: login.token), body: body, fingerprint: fingerprint))
        }
        guard !jobs.isEmpty else { return }
        // 各台服务器同时登记：一台连不上不耽误别的
        let done = await withTaskGroup(of: (String, Int)?.self) { group in
            for job in jobs {
                group.addTask {
                    do {
                        _ = try await job.client.pushMeRegistrationSet(body: job.body)
                        return (job.account, job.fingerprint)
                    } catch {
                        // 只记日志：服务器还不支持推送（404）、连不上、令牌失效（401 由全局处理）都不打扰用户
                        Self.log.info("推送登记失败 \(job.host, privacy: .public)：\(error.localizedDescription, privacy: .public)")
                        return nil
                    }
                }
            }
            var done: [(String, Int)] = []
            for await result in group {
                if let result { done.append(result) }
            }
            return done
        }
        for (account, fingerprint) in done { registered[account] = fingerprint }
    }

    private struct Job: Sendable {
        var account: String
        var host: String
        var client: APIClient
        var body: API.PushRegistrationRequest
        var fingerprint: Int
    }

    /// 本机保存的、钥匙串里还有令牌的全部登录（不只当前账号）
    private static func savedLogins() -> [(server: ServerAddress, token: String, info: PushLoginInfo)] {
        SavedServers.load().flatMap { saved in
            saved.accounts.compactMap { account in
                TokenVault.token(server: saved.address, username: account.username).map { token in
                    (saved.address, token, PushLoginInfo(origin: saved.address.origin.absoluteString, username: account.username,
                                                         serverName: saved.address.hostLabel, accountName: account.nickname))
                }
            }
        }
    }

    /// 推送的 Bundle ID（自己打包的 App 是自己的 Bundle ID，实例据此选通道）
    static var topic: String { Bundle.main.bundleIdentifier ?? "" }

    /// 调试版走 APNs 的开发环境，TestFlight 与 App Store 是生产环境
    static var environment: String {
        #if DEBUG
        "development"
        #else
        "production"
        #endif
    }

    /// 包里有通知扩展：未签名的侧载包（scripts/build-unsigned-ipa.sh）去掉了它，也没有推送权限，收不到推送
    static let hasNotificationService: Bool = {
        guard let plugins = Bundle.main.builtInPlugInsURL else { return false }
        return FileManager.default.fileExists(atPath: plugins.appending(path: "MovieClawNotificationService.appex").path)
    }()

    /// 登录后自动弹权限框的条件：收不到推送的包不弹（允许了也什么都收不到）；自动化测试不弹（系统弹窗会挡住
    /// 测试要点的界面：UI 测试带 --ui-testing，调试启动参数 -mcServer 直接登录），要验证这个权限框的测试带
    /// -mcPushPrompt YES
    private static var promptAllowed: Bool {
        #if DEBUG
        if UserDefaults.standard.bool(forKey: "mcPushPrompt") { return hasNotificationService }
        if DebugLaunch.server != nil { return false }
        #endif
        if ProcessInfo.processInfo.arguments.contains("--ui-testing") { return false }
        return hasNotificationService
    }
}

// MARK: - 前台展示与点开

extension PushCenter: UNUserNotificationCenterDelegate {
    /// 在前台也照常横幅、进通知列表、响铃
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        if response.actionIdentifier == UNNotificationDefaultActionIdentifier,
           let target = PushTapTarget(userInfo: response.notification.request.content.userInfo, store: .shared) {
            Task { @MainActor in PushCenter.shared.pendingTap = target }
        }
        completionHandler()
    }
}
