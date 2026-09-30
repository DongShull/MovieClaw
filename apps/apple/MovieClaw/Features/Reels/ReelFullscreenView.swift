import SwiftUI

/// 片段的全屏（横屏）观看：对应 TikTok 横屏视频下方的「全屏观看」（docs/design/reels.md §6）。
///
/// 用的是信息流里正在放的**同一个播放器**：画面从竖屏页的横带挪到这一层，不重新装载、不打断声音；
/// 退出时挪回去，信息流接着刷。进出时整页转横 / 转回竖屏由 ReelsView 管（PlayerOrientation）。
///
/// 控制层：左上退出与片名，底部播放 / 暂停、进度线与「片中位置 / 片长」、右下「看全片」
/// （从当前位置转到播放器页，同竖屏页的「播放」，但保持横屏直接进）。点画面显隐控制层，放着的时候 3 秒后自动隐去；
/// 暂停或放完时一直显示。控制层不用毛玻璃，上下各垫一层渐暗保证字看得清。
struct ReelFullscreenView: View {
    let store: ReelsStore
    let player: ReelPlayer
    let onExit: () -> Void
    let onPlayFull: () -> Void

    @State private var controlsVisible = true
    @State private var hideTask: Task<Void, Never>?

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            ReelVideoSurface(engineView: player.core.view)
                .id(ObjectIdentifier(player))
                .ignoresSafeArea()
            centerGlyph
            if controlsVisible {
                controls
                    .transition(.opacity)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture { toggleControls() }
        // 整层挂着点按手势：不声明的话读屏会把它当成一个整体，里面的退出、暂停、看全片都点不到
        .accessibilityElement(children: .contain)
        .onAppear { scheduleHide() }
        .onChange(of: store.playerState) { _, state in
            // 暂停、放完时控制层一直在；重新放起来再按时隐去
            if state == .paused || state == .ended {
                hideTask?.cancel()
                withAnimation { controlsVisible = true }
            } else if state == .playing {
                scheduleHide()
            }
        }
        .onDisappear { hideTask?.cancel() }
        .statusBarHidden()
        .persistentSystemOverlays(.hidden)
        .accessibilityIdentifier("reels-fullscreen")
    }

    @ViewBuilder
    private var centerGlyph: some View {
        switch store.playerState {
        case .loading where !store.firstFrameShown:
            ProgressView().tint(.white)
        case .ended:
            ReelCenterGlyph(symbol: "arrow.counterclockwise")
        case .paused:
            ReelCenterGlyph(symbol: "play.fill")
        default:
            EmptyView()
        }
    }

    private var controls: some View {
        VStack(spacing: 0) {
            HStack(spacing: 14) {
                Button(action: onExit) {
                    Image(systemName: "arrow.down.right.and.arrow.up.left")
                        .font(.system(size: 20, weight: .semibold))
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .accessibilityLabel("退出全屏")
                .accessibilityIdentifier("reels-fullscreen-exit")
                VStack(alignment: .leading, spacing: 2) {
                    Text(player.item.title.name)
                        .font(.headline.weight(.bold))
                        .lineLimit(1)
                    if let subtitle {
                        Text(subtitle)
                            .font(.footnote)
                            .foregroundStyle(.white.opacity(0.7))
                            .lineLimit(1)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(.top, 8)
            .background(alignment: .top) { scrim(from: .top) }
            Spacer(minLength: 0)
            HStack(spacing: 16) {
                Button { store.togglePause() } label: {
                    Image(systemName: store.playerState == .playing ? "pause.fill" : "play.fill")
                        .font(.system(size: 22, weight: .semibold))
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .accessibilityLabel(store.playerState == .playing ? "暂停" : "播放")
                ReelProgressRow(item: player.item, player: player)
                Button(action: onPlayFull) {
                    Label("看全片", systemImage: "play.rectangle")
                        .font(.footnote.weight(.semibold))
                        .padding(.horizontal, 12)
                        .padding(.vertical, 7)
                        .overlay(Capsule().stroke(.white.opacity(0.4), lineWidth: 1))
                        .contentShape(Capsule())
                }
                .accessibilityIdentifier("reels-fullscreen-full")
            }
            .padding(.bottom, 10)
            .background(alignment: .bottom) { scrim(from: .bottom) }
        }
        .buttonStyle(.plain)
        .foregroundStyle(.white)
        .shadow(color: .black.opacity(0.5), radius: 3, y: 1)
        .padding(.horizontal, 20)
    }

    /// 年份 · 季集
    private var subtitle: String? {
        var parts: [String] = []
        if let year = player.item.title.year { parts.append(String(year)) }
        if let episode = player.item.title.episode {
            var code = "第 \(episode.season) 季第 \(episode.episode) 集"
            if let name = episode.name, !name.isEmpty { code += " · \(name)" }
            parts.append(code)
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// 控制层背后的渐暗：只压画面边缘一条，不压中间
    private func scrim(from edge: VerticalEdge) -> some View {
        LinearGradient(colors: [.black.opacity(0.55), .clear],
                       startPoint: edge == .top ? .top : .bottom,
                       endPoint: edge == .top ? .bottom : .top)
            .frame(height: 120)
            .padding(.horizontal, -80)
            .padding(edge == .top ? .top : .bottom, -60)
            .allowsHitTesting(false)
    }

    private func toggleControls() {
        withAnimation(.easeInOut(duration: 0.2)) { controlsVisible.toggle() }
        if controlsVisible { scheduleHide() }
    }

    private func scheduleHide() {
        hideTask?.cancel()
        guard store.playerState == .playing || store.playerState == .loading else { return }
        hideTask = Task {
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled else { return }
            withAnimation(.easeInOut(duration: 0.3)) { controlsVisible = false }
        }
    }
}
