import SwiftUI

/// 设置首页：分区列表（Web 手机端 /settings，components/settings-index.tsx）。
/// App 里叫「服务器设置」：这里改的都是服务器上的配置（对所有设备生效），与 App 本机偏好区分开。
/// 成员只看到「设备」（自己的设备；「个人信息」走「我的」页头像卡，不在这里列）；空标题的组（概览）不渲染组头。
/// 最底部是人人可见的「关于 MovieClaw」。
struct SettingsIndexView: View {
    @Environment(\.permissions) private var permissions

    var body: some View {
        List {
            ForEach(SettingsSection.groups, id: \.title) { group in
                let items = group.items.filter { $0.availableInApp && (permissions.isAdmin || $0.memberVisible) }
                if !items.isEmpty {
                    Section {
                        ForEach(items) { section in
                            NavigationLink(value: AppRoute.settingsSection(section)) {
                                Label {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(section.title)
                                        Text(section.subtitle)
                                            .font(.caption)
                                            .foregroundStyle(Theme.textMuted)
                                            .lineLimit(2)
                                    }
                                } icon: {
                                    Image(systemName: section.systemImage)
                                }
                            }
                            .accessibilityIdentifier("settings-\(section.rawValue)")
                        }
                    } header: {
                        if !group.title.isEmpty { Text(group.title) }
                    }
                }
            }
            // 版本、开源许可与数据来源声明（上架必需，人人可见）：低频，放在最底部（原在「我的」页，用户认为太重）
            Section {
                NavigationLink {
                    AboutView()
                } label: {
                    Label("关于 MovieClaw", systemImage: "info.circle")
                }
                .accessibilityIdentifier("settings-about")
            }
        }
        .navigationTitle("服务器设置")
        .appBackground()
    }
}
