import Foundation
import Testing
@testable import AetherEngine

/// 慢线路冷起播的两条读取策略（引擎补丁 P53、P55）：都是纯计算，与网络无关。
/// 端到端效果在模拟器上用 `scripts/faultlab.py slow-start`（6 Mbit/s 共享限速）对照，见 docs/design/playback-qoe.md §9.11
struct SlowLinkReaderPolicyTests {
    // MARK: P53 等在途的提前取：往回送着就接着等

    @Test func waitsWhileThePrefetchKeepsDelivering() {
        let now = Date()
        let fixed = now.addingTimeInterval(-2)   // 按往返定的上限早就过了（慢线路上 0.25 秒）
        // 还没收到过字节：只认按往返定的上限（首字节该到了还没到，别等）
        #expect(AVIOReader.prefetchWaitDeadline(fixed: fixed, now: now, idleSeconds: nil, allowance: 1) == fixed)
        // 0.2 秒前刚到过一批：再等到「停下不动满 1 秒」为止
        let deadline = AVIOReader.prefetchWaitDeadline(fixed: fixed, now: now, idleSeconds: 0.2, allowance: 1)
        #expect(abs(deadline.timeIntervalSince(now) - 0.8) < 0.001)
        // 已经停了 1.5 秒：卡住了，截止时刻落在过去，放弃等待另发请求
        #expect(AVIOReader.prefetchWaitDeadline(fixed: fixed, now: now, idleSeconds: 1.5, allowance: 1) < now)
    }

    @Test func neverCutsTheRoundTripBoundShort() {
        // 按往返定的上限还没到：不管有没有到货，至少等到它
        let now = Date()
        let fixed = now.addingTimeInterval(3)
        #expect(AVIOReader.prefetchWaitDeadline(fixed: fixed, now: now, idleSeconds: 0.9, allowance: 1) == fixed)
    }

    // MARK: P55 整块旁路补取：线路慢到限时内到不齐就不取

    @Test func detourOnlyWhenTheBlockFitsTheBudget() {
        let block = 4 * 1024 * 1024
        // 没测出线路速度：照旧走旁路
        #expect(AVIOReader.detourFitsLink(bytesPerSecond: nil, budget: 4, blockSize: block))
        // 局域网 / 快外网：一整块转眼就到
        #expect(AVIOReader.detourFitsLink(bytesPerSecond: 50_000_000, budget: 4, blockSize: block))
        // 6 Mbit/s（750 KB/s）：4 MB 要 5.6 秒，4 秒限时到不齐——模拟器实测每次都下到 2.6～2.9 MB 作废
        #expect(!AVIOReader.detourFitsLink(bytesPerSecond: 750_000, budget: 4, blockSize: block))
        // 分界在约 10 Mbit/s：留两成余量，整块要在限时的八成内到齐
        #expect(!AVIOReader.detourFitsLink(bytesPerSecond: 1_250_000, budget: 4, blockSize: block))
        #expect(AVIOReader.detourFitsLink(bytesPerSecond: 1_320_000, budget: 4, blockSize: block))
    }
}
