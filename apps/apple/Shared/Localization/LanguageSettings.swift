import SwiftUI
#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

#if os(iOS) || os(macOS)
struct AppLanguageSettings: View {
    var body: some View {
        Button(action: openSettings) {
            HStack {
                Label(.settingsAppearanceLanguage, systemImage: "globe")
                Spacer()
                Text(verbatim: AppLanguage.name).foregroundStyle(.secondary)
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary)
            }
        }
        .accessibilityIdentifier("app-language-settings")
    }

    private func openSettings() {
        #if os(iOS)
        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
        #elseif os(macOS)
        if let url = URL(string: "x-apple.systempreferences:com.apple.Localization-Settings.extension") {
            NSWorkspace.shared.open(url)
        }
        #endif
    }
}
#endif

#if os(macOS)
struct MacLanguageSettings: View {
    var body: some View {
        Form {
            Section {
                AppLanguageSettings()
            } header: {
                Text(.settingsAppearanceGeneral)
            } footer: {
                Text(.settingsAppearanceMacHint)
            }
        }
        .formStyle(.grouped)
        .frame(width: 500)
        .padding()
    }
}
#endif
