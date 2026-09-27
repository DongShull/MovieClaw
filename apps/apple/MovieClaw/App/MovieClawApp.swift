import SwiftUI

@main
struct MovieClawApp: App {
    /// 最先执行（存储属性按声明顺序初始化，早于下面的 AppModel）：打点记下 main 的时刻，并配好图片加载器
    private let bootstrap: Void = {
        PerfTrace.markMain()
        ImagePipelineSetup.configure()
    }()
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var model = AppModel()

    init() {
        PlayerCapability.prewarm()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .preferredColorScheme(.dark)
        }
    }
}

/// 应用代理：目前只负责界面方向锁（见 `OrientationLock`）。
final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication, supportedInterfaceOrientationsFor window: UIWindow?) -> UIInterfaceOrientationMask {
        OrientationLock.mask
    }
}

/// 按 AppModel.phase 切换顶层界面。
struct RootView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Group {
            switch model.phase {
            case .launching:
                ProgressView()
                    .task { await model.restore() }
            case .needsServer, .needsSetup, .needsLogin, .chooseAccount, .unreachable:
                // 这几种状态共用同一个欢迎页（同一分支 = 同一视图身份），状态之间切换时表单里填的内容不丢
                WelcomeView(mode: .root, phase: model.phase, expired: model.expiredUsername != nil)
            case let .ready(session):
                MainTabView()
                    // 换账号（含换到另一台服务器上的同名账号）时整棵树重建，避免残留上个账号的数据
                    .id("\(model.server?.origin.absoluteString ?? "")#\(session.username)")
            }
        }
        .animation(.default, value: model.phase)
        .onAppear {
            PerfTrace.record("root.appear")
            PerfTrace.afterCommit("root.firstFrame")
        }
        .onChange(of: model.session != nil) { _, ready in
            if ready { PerfTrace.record("session.ready") }
        }
    }
}
