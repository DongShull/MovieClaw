import Foundation
import Testing
@testable import AetherEngine

/// 主力通路跳转按代价吸附关键帧（引擎补丁 P36）：精确落点要逐帧解太久才吸附，方向不反，解得快的片子不动。
struct KeyframeSnapPolicyTests {
    /// 《抓特务》520～640 秒的真实关键帧（ffprobe）
    private let keyframes: [Double] = [519.0, 522.667, 525.25, 527.75, 530.333, 534.467, 536.717, 539.467, 543.417,
                                       550.967, 553.3, 563.3, 568.333, 574.333, 578.583, 583.25, 587.383, 597.383,
                                       600.0, 603.717, 605.967, 609.75, 619.75, 629.083, 632.75, 636.467]
    private var cost4K60: Double { KeyframeSnapPolicy.decodeCostPerSecond(frameRate: 60, width: 3840, height: 2160) }

    @Test func costScalesWithFrameRateAndPixels() {
        #expect(abs(cost4K60 - 0.27) < 0.001)
        let hd24 = KeyframeSnapPolicy.decodeCostPerSecond(frameRate: 24, width: 1920, height: 1080)
        #expect(abs(hd24 - 0.027) < 0.001)
        #expect(KeyframeSnapPolicy.decodeCostPerSecond(frameRate: nil, width: 3840, height: 2160) == 0)
    }

    @Test func farFromKeyframeSnapsToNearest() {
        // 手测 #1：533.5 → 562.5，前一关键帧 553.3（9.2 秒，约 2.5 秒解码），后一个 563.3 更近
        let landing = KeyframeSnapPolicy.landing(target: 562.486, from: 533.486, keyframes: keyframes,
                                                 costPerSecond: cost4K60, budget: 0.2)
        #expect(landing == 563.3 + KeyframeSnapPolicy.landingLeadSeconds)
    }

    @Test func nearKeyframeStaysExact() {
        // 离前一关键帧 0.5 秒：约 0.14 秒，在预算内
        #expect(KeyframeSnapPolicy.landing(target: 600.5, from: 590, keyframes: keyframes,
                                           costPerSecond: cost4K60, budget: 0.2) == nil)
    }

    @Test func cheapDecodeStaysExact() {
        // 1080p24 离关键帧 7 秒也只要约 0.19 秒
        let hd24 = KeyframeSnapPolicy.decodeCostPerSecond(frameRate: 24, width: 1920, height: 1080)
        #expect(KeyframeSnapPolicy.landing(target: 616.75, from: 600, keyframes: keyframes,
                                           costPerSecond: hd24, budget: 0.2) == nil)
    }

    @Test func forwardNeverLandsAtOrBeforeOrigin() {
        // 从 554 往前跳 +8 到 562：前一关键帧 553.3 在起点之前，只能取后一个 563.3
        let landing = KeyframeSnapPolicy.landing(target: 562, from: 554, keyframes: keyframes,
                                                 costPerSecond: cost4K60, budget: 0.2)
        #expect(landing == 563.3 + KeyframeSnapPolicy.landingLeadSeconds)
    }

    @Test func backwardNeverLandsAtOrAfterOrigin() {
        // 从 562 往回跳 -1 到 561：后一关键帧 563.3 在起点之后，只能取前一个 553.3
        let landing = KeyframeSnapPolicy.landing(target: 561, from: 562, keyframes: keyframes,
                                                 costPerSecond: cost4K60, budget: 0.2)
        #expect(landing == 553.3 + KeyframeSnapPolicy.landingLeadSeconds)
    }

    @Test func disabledOrNoIndexStaysExact() {
        #expect(KeyframeSnapPolicy.landing(target: 562.486, from: 533, keyframes: keyframes,
                                           costPerSecond: cost4K60, budget: 0) == nil)
        #expect(KeyframeSnapPolicy.landing(target: 562.486, from: 533, keyframes: [],
                                           costPerSecond: cost4K60, budget: 0.2) == nil)
        // 落点在第一个关键帧之前
        #expect(KeyframeSnapPolicy.landing(target: 510, from: 530, keyframes: keyframes,
                                           costPerSecond: cost4K60, budget: 0.2) == nil)
    }
}
