import AVFoundation
import Foundation
import Testing
@testable import MovieClaw

/// 播放器看门狗的纯逻辑：掉帧窗口、卡顿归因、拖动跟随节奏。阈值逐条对照 Web
/// `lib/player/framedrop.ts`、`lib/player/stall.ts`（含 engine.ts watchStall 的推一把）、`lib/player/scrub-follow.ts`。
struct PlaybackWatchdogsTests {
    @Test func frameDropNeedsFullWindowAndMinFrames() {
        var tracker = FrameDropTracker()
        // 前 10 个样本构不成 10 秒窗口
        for second in 0 ..< 10 {
            #expect(tracker.sample(dropped: second * 5, total: second * 24) == nil)
        }
        // 第 11 个：窗口 240 帧掉 50 帧 ≈ 20.8% ≥ 10%
        let ratio = tracker.sample(dropped: 50, total: 240)
        #expect(ratio != nil && ratio! >= FrameDropTracker.ratio)
    }

    @Test func frameDropIgnoresTinyWindowsAndResetsOnCounterDrop() {
        var tracker = FrameDropTracker()
        for second in 0 ... 10 {
            // 10 秒只解了 50 帧：不够 100 帧，不判
            #expect(tracker.sample(dropped: second, total: second * 5) == nil)
        }
        // 计数变小 = 换了流：窗口作废，从头攒
        #expect(tracker.sample(dropped: 0, total: 10) == nil)
    }

    @Test func stallDecodeStalledAfterNudges() {
        var watch = StallWatch()
        var verdicts: [StallWatch.Verdict] = []
        // 先正常播几秒（真正播起来过），然后卡住不动、前方缓冲 10 秒
        for second in 0 ..< 3 {
            verdicts.append(watch.sample(time: Double(second), bufferedAhead: 10, paused: false, ended: false, seeking: false, receiving: true, deadLimit: 45))
        }
        for _ in 0 ..< 20 {
            verdicts.append(watch.sample(time: 2, bufferedAhead: 10, paused: false, ended: false, seeking: false, receiving: true, deadLimit: 45))
        }
        let index = try! #require(verdicts.firstIndex(of: .decodeStalled))
        // 判死之前恰好推了两把；推满两把后 8 秒判死：3 + 3 + 8
        #expect(verdicts[..<index].filter { $0 == .nudge }.count == StallWatch.maxNudges)
        #expect(index - 2 == 3 + 3 + 8)
    }

    @Test func stallSlowLinkNeverFails() {
        // 缓冲见底但字节一直在进来（线路比码率慢）：等多久都不报失败——不换引擎、不自动降码率（《哪吒》现场）
        var watch = StallWatch()
        for _ in 1 ... 600 {
            let verdict = watch.sample(time: 0, bufferedAhead: 0.5, paused: false, ended: false, seeking: false,
                                       receiving: true, deadLimit: StallWatch.directDeadSeconds)
            #expect(verdict == .ok)
        }
    }

    @Test func stallDeadAfterSilentSeconds() {
        // 缓冲见底且连续没有字节：原文件直出 15 秒判断线，服务端流 45 秒
        for limit in [StallWatch.directDeadSeconds, StallWatch.serverDeadSeconds] {
            var watch = StallWatch()
            var deadAt: Int?
            for second in 1 ... 60 {
                if watch.sample(time: 0, bufferedAhead: 0.5, paused: false, ended: false, seeking: false,
                                receiving: false, deadLimit: limit) == .dead {
                    deadAt = second
                    break
                }
            }
            #expect(deadAt == limit)
        }
        // 中途来过一次字节：重新计时
        var watch = StallWatch()
        var verdicts: [StallWatch.Verdict] = []
        for second in 1 ... 25 {
            verdicts.append(watch.sample(time: 0, bufferedAhead: 0.5, paused: false, ended: false, seeking: false,
                                         receiving: second == 10, deadLimit: 15))
        }
        #expect(verdicts.firstIndex(of: .dead) == 24)  // 第 10 秒收到字节，之后再静默 15 秒
        #expect(StallWatch.reason(.dead, deadLimit: 15).hasPrefix("连续 15 秒没有收到数据"))
        #expect(StallWatch.reason(.dead, deadLimit: 45).hasPrefix("连续 45 秒没有收到服务端的数据"))
    }

    @Test func stallIgnoresPausedAndSeeking() {
        var watch = StallWatch()
        for _ in 0 ..< 60 {
            #expect(watch.sample(time: 5, bufferedAhead: 0, paused: true, ended: false, seeking: false, receiving: false, deadLimit: 15) == .ok)
            #expect(watch.sample(time: 5, bufferedAhead: 0, paused: false, ended: false, seeking: true, receiving: false, deadLimit: 15) == .ok)
        }
    }

    @Test func scrubFollowPlans() {
        #expect(ScrubFollow.plan(nowMs: 1000, lastFollowMs: 0, cheap: true, reachable: false, settleOnly: false) == .skip)
        #expect(ScrubFollow.plan(nowMs: 1000, lastFollowMs: 0, cheap: false, reachable: true, settleOnly: false) == .skip)
        #expect(ScrubFollow.plan(nowMs: 1000, lastFollowMs: 0, cheap: false, reachable: true, settleOnly: true) == .deferred(ms: 60))
        #expect(ScrubFollow.plan(nowMs: 1000, lastFollowMs: 0, cheap: true, reachable: true, settleOnly: false) == .follow)
        // 上次跟随刚过 70ms：再等 30ms 就到 100ms 兜底
        #expect(ScrubFollow.plan(nowMs: 1070, lastFollowMs: 1000, cheap: true, reachable: true, settleOnly: false) == .deferred(ms: 30))
    }

    // MARK: 取流失败归因与同档重开上限（第二轮审计 N-05-1）

    @Test func formatNotRecognizedIsNotNetwork() {
        // -11828 = AVErrorFileFormatNotRecognized：这一档放不了，要降档而不是同档重开
        let format = NSError(domain: AVFoundationErrorDomain, code: -11828)
        #expect(AVPlayerEngine.cause(of: format) == .decode)
        // 真正的网络类错误仍走同档重开
        #expect(AVPlayerEngine.cause(of: NSError(domain: AVFoundationErrorDomain, code: -11863)) == .network)
        #expect(AVPlayerEngine.cause(of: NSError(domain: NSURLErrorDomain, code: NSURLErrorTimedOut)) == .network)
        let wrapped = NSError(domain: AVFoundationErrorDomain, code: -11800, userInfo: [
            NSUnderlyingErrorKey: NSError(domain: NSURLErrorDomain, code: NSURLErrorNetworkConnectionLost),
        ])
        #expect(AVPlayerEngine.cause(of: wrapped) == .network)
    }

    @Test func networkRestartBudgetCapsConsecutiveRestarts() {
        var budget = NetworkRestartBudget()
        // 连续两次没出画还能重开，第三次不再相信「网络」归因
        let first = budget.allowRestart()
        let second = budget.allowRestart()
        let third = budget.allowRestart()
        let fourth = budget.allowRestart()
        #expect(first && second)
        #expect(!third && !fourth)
        // 放起来过一次就清零，下次断线照常重开
        budget.reachedPlaying()
        let afterPlaying = budget.allowRestart()
        #expect(afterPlaying)
        budget.reset()
        #expect(budget.consecutive == 0)
    }

    @Test func prematureEndResumesOnlyFarFromTheEnd() {
        // 45 分钟的片放到 12 分钟报播完：断流，从当前位置重开
        var guardian = PrematureEndGuard()
        let midway = guardian.shouldResume(positionMs: 731_000, durationMs: 2_700_000)
        #expect(midway)
        // 片尾 30 秒内报播完：真播完（片尾字幕、时长略有出入）
        var nearEnd = PrematureEndGuard()
        let atCredits = nearEnd.shouldResume(positionMs: 2_680_000, durationMs: 2_700_000)
        #expect(!atCredits)
        // 不知道片长：没法判断，按播完处理
        var unknown = PrematureEndGuard()
        let noDuration = unknown.shouldResume(positionMs: 731_000, durationMs: nil)
        #expect(!noDuration)
    }

    @Test func prematureEndGivesUpWhenItEndsAgainAtTheSameSpot() {
        var guardian = PrematureEndGuard()
        let first = guardian.shouldResume(positionMs: 731_000, durationMs: 2_700_000)
        // 重开后原地附近又报播完：片长写错了，这里就是真结尾，不再重开
        let again = guardian.shouldResume(positionMs: 735_000, durationMs: 2_700_000)
        // 往后又放了一大段才再断：照常重开
        let later = guardian.shouldResume(positionMs: 1_500_000, durationMs: 2_700_000)
        #expect(first && !again && later)
        // 换单元后从头计
        guardian.reset()
        let afterReset = guardian.shouldResume(positionMs: 1_500_000, durationMs: 2_700_000)
        #expect(afterReset)
    }

    @Test func reconnectBackoffSpansAboutAMinuteThenGivesUp() {
        var backoff = ReconnectBackoff()
        var delays: [Double] = []
        while let delay = backoff.nextDelay() { delays.append(delay) }
        // 覆盖一次服务端重启（停机二三十秒）还有富余，但不会无限转圈
        let total = delays.reduce(0, +)
        #expect(total >= 45 && total <= 90)
        #expect(delays == delays.sorted())
        backoff.reset()
        let firstAgain = backoff.nextDelay()
        #expect(firstAgain == delays.first)
    }
}
