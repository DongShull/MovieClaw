import Foundation

/// App 的发行版本：完整版与商店版。
///
/// - 完整版：开发调试、自行构建、只给团队成员的内部 TestFlight。功能与网页端一致。
/// - 商店版：对外 TestFlight 与 App Store。设置里不提供资源站点、下载器、自动入库、订阅规则
///   这几项配置（改在网页端管理），降低审核按条款 5.2.3（便利文件共享）拒审的风险；
///   其余功能与完整版相同（2026-09-28 用户决定只藏设置里的配置）。
///
/// 由编译条件 `MC_STORE` 决定：打包脚本 `scripts/release.sh --store` 通过构建设置
/// `MC_EDITION_CONDITIONS=MC_STORE` 打开它（见 project.yml 与 XcodeConfig/Signing.xcconfig）。
/// 完整版上传时导出选项带 testFlightInternalTestingOnly，App Store Connect 不允许它进入对外测试
/// 或提交审核，两个版本不会传错渠道。
enum AppEdition {
    #if MC_STORE
    static let isStore = true
    #else
    static let isStore = false
    #endif
}
