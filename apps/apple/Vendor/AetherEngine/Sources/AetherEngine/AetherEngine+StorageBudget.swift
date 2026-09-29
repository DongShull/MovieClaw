import Foundation

/// [MovieClaw P25] 存储紧张时照样能放（MovieClaw 补丁，见 PATCHES.md）。
///
/// 换封装通路把切好的分片写进临时目录（`SegmentCache`：前方 10 段、后方 20 段，另有留存预算），
/// UHD 原盘一段 30～45 MB，光这两个硬窗口就要 1 GB 上下；片源字节缓存（P22）与软件通路的读前缓存也落在同一个卷。
/// 手机存储快满时分片写不进去，起播直接失败，App 原来只好在可用空间低于 512 MB 时改走服务端流。
///
/// 这里给宿主两样东西：
/// 1. `LoadOptions.backwardBufferSegments`：后方窗口可设（前方窗口本来就有 `forwardBufferSegments`），
///    宿主按剩余空间把两个窗口一起收小，自研引擎在存储紧张时照样能放；
/// 2. 写分片遇到 ENOSPC 记下「存储已满」（补丁 P8 的延伸：P8 只认建分片目录失败），最终报 `storageExhausted`，
///    宿主据此收小窗口原位重开，而不是当成「解不了」换播放器。
/// 另有两个测试钩子，用来在模拟器上复现「手机存储快满」与「播放中被写满」。
extension AetherEngine {
    /// 测试用：假装临时目录所在的卷只剩这么多字节（nil = 读真实值）。分片留存预算、片源字节缓存预算、
    /// 软件通路读前缓存预算都按它算
    public nonisolated(unsafe) static var volumeAvailableBytesOverrideForTesting: Int64?

    /// 测试用：在这个系统运行时刻（`ProcessInfo.systemUptime`）之前，写分片一律按 ENOSPC 失败
    public nonisolated(unsafe) static var simulateStorageFullUntilUptimeForTesting: TimeInterval?

    /// 临时目录所在卷的可用字节（测试覆盖优先）。`importantUsage` 沿用各处原来的读法：iOS 上分片与
    /// 片源缓存按「重要用途可用」（含系统可清掉的空间）算，tvOS 没有这个键，一律按普通可用
    nonisolated static func temporaryVolumeAvailableBytes(importantUsage: Bool) -> Int64? {
        if let override = volumeAvailableBytesOverrideForTesting { return override }
        let temp = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
        #if !os(tvOS)
        if importantUsage {
            return (try? temp.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]))?
                .volumeAvailableCapacityForImportantUsage
        }
        #endif
        return (try? temp.resourceValues(forKeys: [.volumeAvailableCapacityKey]))?
            .volumeAvailableCapacity.map(Int64.init)
    }

    /// 测试钩子是否正在模拟「存储已满」
    nonisolated static var storageFullSimulated: Bool {
        guard let until = simulateStorageFullUntilUptimeForTesting else { return false }
        return ProcessInfo.processInfo.systemUptime < until
    }
}
