import Foundation
import Testing
@testable import AetherEngine

/// 软件通路的点播时间轴（引擎补丁 P35）：容器起点明显不为 0 的片源，装载时就把起点定为 session zero，
/// 发布的是从 0 算的内容时间，定位时再加回源时间轴。
struct SWClockAnchorPolicyTests {
    @Test func farOriginBecomesSessionZero() {
        // 《戴珍珠耳环》VC-1 原盘：raw 从 599.996 秒起
        #expect(SWClockAnchorPolicy.vodSessionZero(sourceOriginSeconds: 599.996, isLive: false) == 599.996)
    }

    @Test func smallOriginKeepsRawAxis() {
        // DVD、B 帧 MP4 这类几百毫秒的起点照旧按 raw，行为不变
        #expect(SWClockAnchorPolicy.vodSessionZero(sourceOriginSeconds: 0.28, isLive: false) == 0)
        #expect(SWClockAnchorPolicy.vodSessionZero(sourceOriginSeconds: 0, isLive: false) == 0)
        #expect(SWClockAnchorPolicy.vodSessionZero(sourceOriginSeconds: .nan, isLive: false) == 0)
    }

    @Test func liveIsLeftToFirstSampleAnchoring() {
        #expect(SWClockAnchorPolicy.vodSessionZero(sourceOriginSeconds: 19123, isLive: true) == 0)
    }

    @Test func resumeOnFarOriginSourceStaysAligned() {
        // 续播到内容 600 秒：定位与锚点都在源轴 1199.996，首样本落在那里不触发重锚，发布时间 = raw − 起点
        let zero = SWClockAnchorPolicy.vodSessionZero(sourceOriginSeconds: 599.996, isLive: false)
        let anchor = 600 + zero
        let resolution = SWClockAnchorPolicy.resolve(initialSeconds: anchor, firstSampleSeconds: anchor + 0.04)
        #expect(resolution == .init(anchorSeconds: anchor, sessionZeroSeconds: 0))
        #expect(SWClockAnchorPolicy.sourceSeconds(forSession: 300, sessionZeroSeconds: zero) == 899.996)
    }
}
