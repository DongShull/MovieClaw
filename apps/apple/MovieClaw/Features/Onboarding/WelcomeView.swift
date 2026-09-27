import SwiftUI

/// 欢迎页：本机没有登录中的账号时的整屏页面，也是「添加账号」的那张卡片。
///
/// 背景始终是同一片写实的深空（`CosmosBackdrop`：星空、银河、行星地平线），几种状态在同一页里切换，不跳页面：
/// - **片头**（仅本机从没登录过任何服务器时）：星空浮现、地平线像轨道日出一样亮起，片名浮现，
///   下方像电影字幕一样轮播科幻电影的台词；点「启程」进入登录卡片；
/// - **登录卡片**：服务器地址、用户名、密码一张表，一步进入。服务器是全新的，就在同一张卡片里补一个确认密码，
///   创建超级管理员。登录过期时预填服务器与用户名，只需输密码；
/// - **选择账号**：当前服务器上已经没有登录中的账号、别的服务器上还有时（比如退出了这台上的最后一个账号），
///   先列出来让用户一点即进，不用输密码；也可以「登录其他账号」；
/// - **连不上服务器**：冷启动连不上时不逼人重新登录——登录态还在，给「重试 / 修改服务器地址 / 切换到其他账号」。
///
/// 已登录时从「切换账号」里点「添加账号」，也打开这一页（`Mode.addAccount`）：直接是登录卡片，服务器预填当前这台、
/// 可以改——改成别的地址就是登录到另一台服务器。右上角可以关掉回到原来的账号。
struct WelcomeView: View {
    /// 从哪里打开
    enum Mode: Equatable {
        /// 顶层：没有登录中的账号时由 RootView 渲染，显示哪种状态跟着 `AppModel.phase` 走
        case root
        /// 已登录时从「切换账号」里打开：添加账号，或给某个登录过期的账号重新输密码（带用户名）
        case addAccount(server: ServerAddress?, username: String?)
    }

    private enum Stage {
        case intro
        case form
        case chooser
        case unreachable
    }

    let mode: Mode
    /// 「添加账号」卡片点关闭时调用（登录成功后主界面会整棵重建，用不着回调）
    var onClose: (() -> Void)?

    @Environment(AppModel.self) private var model
    @State private var stage: Stage
    /// 页内跳转带来的预填（从「选择账号」「连不上」跳到登录卡片时），盖过按状态推出来的默认预填
    @State private var override: WelcomeSignInPanel.Prefill?
    @State private var sceneIndex = 0
    /// 宇宙从黑暗中亮起：先是星空，再是地平线
    @State private var lit = false
    /// 片名、副标题、字幕、按钮依次浮现
    @State private var revealed = false
    /// 正在输入（表单里有输入框获得焦点、键盘弹起）时收起顶部片名：
    /// 小屏上「片名 + 表单」高过键盘上方的空间，登录按钮会被键盘挡住。
    /// 用焦点而不是键盘通知判断：转场动画中途弹键盘时，通知的先后顺序不可靠，片名会残留在状态栏下
    @State private var editing = false

    private static let panelID = "welcome-panel"

    /// 每句台词停留的时长
    private static let sceneDuration: Duration = .seconds(7)

    init(mode: Mode, phase: AppModel.Phase, onClose: (() -> Void)? = nil) {
        self.mode = mode
        self.onClose = onClose
        _stage = State(initialValue: Self.stage(for: phase, mode: mode))
    }

    var body: some View {
        ZStack {
            CosmosBackdrop(lit: lit, dimmed: stage != .intro)
                .ignoresSafeArea()

            GeometryReader { proxy in
                ScrollViewReader { scroller in
                ScrollView {
                    VStack(spacing: 0) {
                        if stage == .intro || !editing {
                            WelcomeMasthead(revealed: revealed, compact: stage != .intro)
                                .padding(.top, stage == .intro ? proxy.size.height * 0.22 : 12)
                                .transition(.opacity)
                        }

                        Spacer(minLength: 28)

                        switch stage {
                        case .intro:
                            FilmSubtitle(scene: WelcomeScene.all[sceneIndex])
                                .id(sceneIndex)
                                .transition(.blurReplace)
                                .opacity(revealed ? 1 : 0)
                                .animation(.easeInOut(duration: 1.4).delay(revealed ? 2.2 : 0), value: revealed)
                            Spacer(minLength: 40)
                            startButton
                        case .form:
                            WelcomeSignInPanel(
                                editing: $editing,
                                purpose: purpose,
                                prefill: prefill,
                                onSwitchAccount: canChooseAccount ? { go(.chooser) } : nil,
                                onSignedIn: onClose
                            )
                            // 预填变了（从「选择账号」点到另一个过期账号）就换一张新卡片，按新的预填初始化
                            .id(prefill)
                            .id(Self.panelID)
                            .transition(.move(edge: .bottom).combined(with: .opacity))
                        case .chooser:
                            WelcomeAccountChooser(
                                onSignInAnother: { go(.form, prefill: .init(server: model.server, username: nil)) },
                                onNeedsPassword: { server, username in go(.form, prefill: .init(server: server, username: username)) }
                            )
                            .id(Self.panelID)
                            .transition(.move(edge: .bottom).combined(with: .opacity))
                        case .unreachable:
                            WelcomeUnreachableCard(
                                onEditAddress: { go(.form, prefill: .init(server: model.server, username: nil)) },
                                onChooseAccount: canChooseAccount ? { go(.chooser) } : nil
                            )
                            .id(Self.panelID)
                            .transition(.move(edge: .bottom).combined(with: .opacity))
                        }
                    }
                    .padding(.horizontal, 24)
                    .padding(.bottom, 12)
                    .frame(maxWidth: 440)
                    .frame(maxWidth: .infinity, minHeight: proxy.size.height)
                }
                .scrollBounceBehavior(.basedOnSize)
                .scrollDismissesKeyboard(.interactively)
                // 收起 / 放出片名后内容高度变了，滚动位置却还停在原处（卡片会被顶到状态栏下）：
                // 等键盘动画走完，把卡片底边贴回可视区底部（键盘上方）
                .onChange(of: editing) {
                    Task {
                        try? await Task.sleep(for: .milliseconds(380))
                        withAnimation(.smooth(duration: 0.3)) { scroller.scrollTo(Self.panelID, anchor: .bottom) }
                    }
                }
                }
            }
        }
        .overlay(alignment: .topTrailing) { closeButton }
        .task { await play() }
        .onChange(of: model.phase) { _, phase in
            // 顶层：状态机换了状态（重试后变成要重新登录等）就换到对应的卡片；片头不因此被打断
            guard mode == .root, stage != .intro else { return }
            let next = Self.stage(for: phase, mode: mode)
            guard next != stage, next != .intro else { return }
            go(next)
        }
    }

    private static func stage(for phase: AppModel.Phase, mode: Mode) -> Stage {
        if case .addAccount = mode { return .form }
        switch phase {
        case .needsServer: return .intro
        case .chooseAccount: return .chooser
        case .unreachable: return .unreachable
        default: return .form
        }
    }

    /// 登录卡片的预填：页内跳转带来的优先；否则添加账号用传进来的，顶层用当前服务器与登录过期的用户名
    private var prefill: WelcomeSignInPanel.Prefill {
        if let override { return override }
        switch mode {
        case let .addAccount(server, username): return .init(server: server ?? model.server, username: username)
        case .root: return .init(server: model.server, username: model.expiredUsername)
        }
    }

    private var purpose: WelcomeSignInPanel.Purpose {
        if prefill.username != nil { return .reauth }
        if case .addAccount = mode { return .addAccount }
        return .signIn
    }

    /// 别的服务器上还有登录中的账号，才给「切换到其他账号」（添加账号的卡片不给：身后就是切换账号列表）
    private var canChooseAccount: Bool {
        mode == .root && !model.accountsOnOtherServers.isEmpty
    }

    private func go(_ next: Stage, prefill: WelcomeSignInPanel.Prefill? = nil) {
        withAnimation(.smooth(duration: 0.6)) {
            override = prefill
            stage = next
        }
    }

    @ViewBuilder
    private var closeButton: some View {
        if case .addAccount = mode {
            Button {
                onClose?()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 15, weight: .semibold))
                    .frame(width: 22, height: 22)
            }
            .buttonStyle(.glass)
            .buttonBorderShape(.circle)
            .controlSize(.large)
            .padding(.trailing, 16)
            .padding(.top, 4)
            .accessibilityLabel("关闭")
            .accessibilityIdentifier("welcome-close")
        }
    }

    private var startButton: some View {
        Button {
            go(.form)
        } label: {
            Text("启程")
                .font(.welcomeSerif(size: 17))
                .tracking(8)
                .padding(.horizontal, 36)
                .padding(.vertical, 4)
        }
        .buttonStyle(.glass)
        .controlSize(.large)
        .opacity(revealed ? 1 : 0)
        .offset(y: revealed ? 0 : 12)
        .animation(.easeOut(duration: 1.0).delay(revealed ? 2.8 : 0), value: revealed)
        .accessibilityIdentifier("welcome-start")
    }

    /// 片头调度：先点亮星空、再浮字，之后按固定节奏换台词，直到页面离开
    private func play() async {
        withAnimation(.easeInOut(duration: stage == .intro ? 2.6 : 1.2)) { lit = true }
        revealed = true
        while !Task.isCancelled {
            try? await Task.sleep(for: Self.sceneDuration)
            guard !Task.isCancelled else { return }
            withAnimation(.easeInOut(duration: 1.2)) {
                sceneIndex = (sceneIndex + 1) % WelcomeScene.all.count
            }
        }
    }
}

/// 片名：衬线体「Movie*Claw*」+ 一道细线 + 宋体「智能影音服务器」。
/// 片头时字距从宽收紧、缓缓浮现；进入登录后整体缩小留在顶部。
private struct WelcomeMasthead: View {
    let revealed: Bool
    let compact: Bool

    var body: some View {
        VStack(spacing: 14) {
            Text("\(Text("Movie"))\(Text("Claw").italic())")
                .font(.system(size: 46, weight: .light, design: .serif))
                .tracking(revealed ? 1 : 12)
                .foregroundStyle(Theme.accentStrong)
                .opacity(revealed ? 1 : 0)
                .animation(.easeOut(duration: 2.4).delay(revealed ? 0.6 : 0), value: revealed)

            Rectangle()
                .fill(Theme.text.opacity(0.5))
                .frame(width: revealed ? 28 : 0, height: 0.5)
                .animation(.easeInOut(duration: 1.2).delay(revealed ? 1.4 : 0), value: revealed)

            Text("智能影音服务器")
                .font(.welcomeSerif(size: 13))
                .tracking(8)
                .foregroundStyle(Theme.textMuted)
                .opacity(revealed ? 1 : 0)
                .animation(.easeOut(duration: 1.4).delay(revealed ? 1.6 : 0), value: revealed)
        }
        .shadow(color: .black.opacity(0.35), radius: 16)
        .scaleEffect(compact ? 0.72 : 1, anchor: .top)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }
}

/// 电影字幕（宋体）：中文一行（或两行）在上，外语原句小一号斜体在下，再下是片名与年份。
/// 固定最小高度，换场时一行与三行的字幕不会把下面的按钮顶来顶去。
private struct FilmSubtitle: View {
    let scene: WelcomeScene

    var body: some View {
        VStack(spacing: 10) {
            Text(scene.line)
                .font(.welcomeSerif(size: 18))
                .lineSpacing(7)
                .foregroundStyle(Theme.text)
            if let original = scene.original {
                Text(original)
                    .font(.system(size: 13, weight: .regular, design: .serif))
                    .italic()
                    .lineSpacing(3)
                    .foregroundStyle(Theme.textMuted)
            }
            Text(verbatim: "——《\(scene.film)》\(scene.year)")
                .font(.welcomeSerif(size: 11))
                .tracking(2)
                .foregroundStyle(Theme.textFaint)
                .padding(.top, 6)
        }
        .multilineTextAlignment(.center)
        .shadow(color: .black.opacity(0.6), radius: 10)
        .frame(maxWidth: .infinity, minHeight: 150, alignment: .bottom)
        .accessibilityElement(children: .combine)
    }
}
