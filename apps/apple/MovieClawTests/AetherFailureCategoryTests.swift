import Testing
@testable import AetherCore
import AetherEngine

/// 引擎错误 → 归因的对照表（`AetherPlayback.category`，docs/design/player-engine.md §3）。
/// 归因决定下一步：网络问题等片源回来原地重开、片源不在了直接说明、存储写满收小缓冲重开，
/// 只有「解不了」才可能换播放器——归错一类就是一次白降级。
@MainActor
struct AetherFailureCategoryTests {
    private func category(_ kind: PlaybackErrorKind, domain: String? = nil, code: Int? = nil)
        -> AetherPlayback.FailureCategory {
        AetherPlayback.category(of: PlaybackErrorInfo(kind: kind, message: "", underlyingDomain: domain, underlyingCode: code))
    }

    @Test func notDecodeFailures() {
        // 存储写满（内置引擎补丁 P25）
        #expect(category(.storageExhausted) == .storageFull)
        // 404 是文件不在了；令牌过期、服务端 5xx、限流换张令牌原地重开
        #expect(category(.sourceRefused, code: 404) == .sourceMissing)
        #expect(category(.sourceRefused, code: 401) == .network)
        #expect(category(.sourceRefused, code: 502) == .network)
        #expect(category(.sourceRateLimited, code: 429) == .network)
        // 源中途断掉是网络问题
        #expect(category(.vodSourceFailed, code: -5) == .network)
    }

    @Test func decodeFailures() {
        // -22 是「源音频封装不进 fMP4」，可能是一时的（例如存储刚写满），先原位重开
        #expect(category(.vodSourceFailed, code: -22) == .decode(final: false))
        // 起播那一刻「打不开」分不出网络还是格式不认：归成确定解不了，由控制器先探片源再定
        #expect(category(.sourceOpenFailed) == .decode(final: true))
        #expect(category(.customSourceProbeFailed) == .decode(final: true))
        #expect(category(.dolbyVisionRequiresHardware) == .decode(final: true))
        // AVPlayer 的解码判决：引擎已经转过本机软解（补丁 P24）也接不住，重开一样
        #expect(category(.nativeItemFailed, domain: "AVFoundationErrorDomain", code: -11833) == .decode(final: true))
        #expect(category(.nativeItemFailed, domain: "CoreMediaErrorDomain", code: -12906) == .decode(final: true))
        // AVPlayer 别的失败、中途重建失败：可能是一时的
        #expect(category(.nativeItemFailed, domain: "NSURLErrorDomain", code: -1005) == .decode(final: false))
        #expect(category(.reloadFailed) == .decode(final: false))
        #expect(category(.audioTrackSwitchFailed) == .decode(final: false))
        #expect(AetherPlayback.category(of: nil) == .decode(final: false))
    }
}
