import Foundation

/// [MovieClaw P36] 主力通路跳转按代价吸附到关键帧。
///
/// 主力通路给 AVPlayer 的跳转是零容差的（放开容差在换封装的 fMP4 上会落到任意同步帧，openradar 44904505），
/// AVPlayer 于是从落点前一个关键帧起逐帧解到落点。低码率 WEB 片的关键帧按场景最远隔 10 秒，4K60 手机硬解
/// 约 3.7 倍速：《抓特务》拖进度条缓冲内跳转要 2～2.5 秒，数据早就在本机（2026-09-29 用户手测，
/// 耗时与「落点离前一关键帧的距离」成正比，每秒约 0.27 秒）。
///
/// 这里先估精确落点要逐帧解多久（距离 × 帧率 × 按像素折算的单帧耗时），超过预算才改落到最近的关键帧
/// ——落在关键帧上的精确跳转只解一帧。解得快的片子（1080p24 等）照旧精确落点。往前跳不落到起点及其之前，
/// 往回跳不落到起点及其之后，按钮的方向感不会反。用户 2026-09-29 拍板「按代价自适应吸附」。
enum KeyframeSnapPolicy {
    /// 4K（3840×2160）单帧的硬解耗时，秒。iPhone Air 实测 4K60 HEVC 约 220 帧/秒
    static let secondsPerFrameAt4K: Double = 0.0045
    /// 落在关键帧之后多远。索引里的关键帧时间与 AVPlayer 播放轴之间可能差几帧（B 帧 MP4 的合成偏移：《他是谁》
    /// 首个关键帧 −0.067 秒），落在「关键帧 + 0.01 秒」实际仍在关键帧之前，AVPlayer 又从上一个关键帧解起（真机 1.6 秒）。
    /// 0.2 秒越过这类偏移，4K60 多解约 12 帧（约 50 毫秒）；另夹在到下一个关键帧距离的一半以内
    static let landingLeadSeconds: Double = 0.2

    /// 每秒源时长逐帧解码的耗时（秒）。帧率或尺寸未知时返回 0（不吸附）
    static func decodeCostPerSecond(frameRate: Double?, width: Int, height: Int) -> Double {
        guard let fps = frameRate, fps.isFinite, fps > 0, width > 0, height > 0 else { return 0 }
        let pixelScale = Double(width * height) / Double(3840 * 2160)
        return fps * pixelScale * secondsPerFrameAt4K
    }

    /// 吸附后的落点；返回 nil 表示照旧精确落点。
    /// - Parameters:
    ///   - target: 请求的落点（与 `keyframes` 同一时间轴）
    ///   - from: 跳转发起时的播放位置
    ///   - keyframes: 升序的关键帧时间
    ///   - costPerSecond: `decodeCostPerSecond` 的结果
    ///   - budget: 逐帧解码的预算（秒），≤ 0 关闭吸附
    static func landing(target: Double, from: Double, keyframes: [Double],
                        costPerSecond: Double, budget: Double) -> Double? {
        guard budget > 0, costPerSecond > 0, target.isFinite, from.isFinite, !keyframes.isEmpty else { return nil }
        // 第一个大于 target 的关键帧下标（二分）
        var lo = 0, hi = keyframes.count
        while lo < hi {
            let mid = (lo + hi) / 2
            if keyframes[mid] <= target { lo = mid + 1 } else { hi = mid }
        }
        guard lo > 0 else { return nil }
        let previous = keyframes[lo - 1]
        guard (target - previous) * costPerSecond > budget else { return nil }
        var candidates = [previous]
        if lo < keyframes.count { candidates.append(keyframes[lo]) }
        let forward = target > from
        let allowed = candidates.filter { forward ? $0 > from : $0 < from }
        guard let best = allowed.min(by: { abs($0 - target) < abs($1 - target) }) else { return nil }
        let next = keyframes.first { $0 > best }
        let lead = next.map { min(landingLeadSeconds, ($0 - best) / 2) } ?? landingLeadSeconds
        return best + lead
    }
}

extension AetherEngine {
    /// [MovieClaw P36] 主力通路精确落点允许的逐帧解码预算（秒）；超过就吸附到最近的关键帧。≤ 0 关闭（真机对照用）
    nonisolated(unsafe) public static var seekSnapDecodeBudgetSeconds: Double = 0.2
}
