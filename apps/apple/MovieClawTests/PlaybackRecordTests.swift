import Foundation
import Testing
@testable import MovieClaw

/// 播放记录的计时与判定（docs/design/playback-qoe.md §1.3、§3）：用可以手动拨动的时钟，不依赖真实时间。
@MainActor
struct PlaybackRecordTests {
    /// 手动拨动的单调时钟
    final class FakeClock {
        var now = ContinuousClock.now
        func advance(_ ms: Int) { now = now + .milliseconds(ms) }
    }

    private func makeRecord(_ clock: FakeClock, mediaItemId: Int = 1) -> PlaybackRecord {
        PlaybackRecord(unit: PlaybackUnit(mediaItemId: mediaItemId, season: 0, episode: 0), origin: .tap,
                       now: { clock.now })
    }

    private func payload(_ record: PlaybackRecord, outcome: PlaybackRecord.Outcome = .exited) -> API.PlaybackMetricPayload {
        record.payload(outcome: outcome, network: .home, positionMs: 0, durationMs: nil, watchedMs: 0,
                       droppedFrames: nil, totalFrames: nil, logTail: nil)
    }

    private func detail(_ payload: API.PlaybackMetricPayload) throws -> [String: Any] {
        let data = try JSONEncoder().encode(payload.detail)
        return try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: 起播

    @Test func firstFrameIsMeasuredFromTheTapMinusUserWait() {
        let clock = FakeClock()
        let record = makeRecord(clock)
        clock.advance(300)
        record.beginUserWait()           // 确认转码的弹窗停在用户手里 2 秒
        clock.advance(2000)
        record.endUserWait()
        clock.advance(500)
        record.noteFirstFrame()
        clock.advance(100)
        record.notePlaying()
        let report = payload(record)
        #expect(report.firstFrameMs == 800)
        #expect(report.playingMs == 900)
        #expect(report.userWaitMs == 2000)
        // 旧字段 ttff_ms 也是首帧口径
        #expect(report.ttffMs == 800)
    }

    // MARK: 跳转

    @Test func seekIsMeasuredUntilThePictureArrives() throws {
        let clock = FakeClock()
        let record = makeRecord(clock)
        record.noteFirstFrame()
        record.beginSeek(source: .button, fromMs: 600_000, toMs: 610_000, buffered: true, paused: false, restart: false)
        #expect(record.seekInFlight)
        clock.advance(180)
        #expect(record.seekPresented() == 180)
        #expect(!record.seekInFlight)
        let seeks = try #require(try detail(payload(record))["seeks"] as? [[String: Any]])
        #expect(seeks.count == 1)
        #expect(seeks[0]["ms"] as? Int == 180)
        #expect(seeks[0]["buffered"] as? Bool == true)
        #expect(seeks[0]["outcome"] as? String == "landed")
        #expect(seeks[0]["source"] as? String == "button")
    }

    @Test func scrubStartsAtTheFirstDrag() {
        let clock = FakeClock()
        let record = makeRecord(clock)
        record.noteFirstFrame()
        record.noteScrubActivity()          // 手指开始拖
        clock.advance(700)
        record.noteScrubActivity()          // 拖动中的跟随不改起点
        clock.advance(300)
        record.beginSeek(source: .scrub, fromMs: 0, toMs: 60_000, buffered: false, paused: false, restart: false)
        clock.advance(200)
        #expect(record.seekPresented() == 1200)
    }

    @Test func aNewSeekSupersedesThePendingOne() throws {
        let clock = FakeClock()
        let record = makeRecord(clock)
        record.beginSeek(source: .button, fromMs: 0, toMs: 10_000, buffered: true, paused: false, restart: false)
        clock.advance(50)
        record.beginSeek(source: .button, fromMs: 10_000, toMs: 20_000, buffered: true, paused: false, restart: false)
        clock.advance(100)
        _ = record.seekPresented()
        let seeks = try #require(try detail(payload(record))["seeks"] as? [[String: Any]])
        #expect(seeks.map { $0["outcome"] as? String } == ["superseded", "landed"])
    }

    @Test func restartSeekEndsAtTheNewSessionsFirstFrame() {
        let clock = FakeClock()
        let record = makeRecord(clock)
        record.noteFirstFrame()
        record.beginSeek(source: .scrub, fromMs: 0, toMs: 3_000_000, buffered: false, paused: false, restart: true)
        clock.advance(1500)
        record.noteFirstFrame()
        #expect(!record.seekInFlight)
    }

    @Test func farSeekRightAfterResumeIsAMisguess() throws {
        let clock = FakeClock()
        let record = makeRecord(clock)
        record.resumed = true
        record.noteFirstFrame()
        clock.advance(5000)
        record.beginSeek(source: .scrub, fromMs: 600_000, toMs: 60_000, buffered: false, paused: false, restart: false)
        let behaviors = try #require(try detail(payload(record))["behaviors"] as? [[String: Any]])
        #expect(behaviors.first?["kind"] as? String == "resume_seek")
        #expect(behaviors.first?["misguess"] as? Bool == true)
    }

    // MARK: 卡顿 / 冻帧

    @Test func stallCountsWhenThePlayheadStopsForHalfASecond() throws {
        let clock = FakeClock()
        let record = makeRecord(clock)
        record.noteFirstFrame()
        var position = 0
        for _ in 0 ..< 4 {           // 正常播放：每 250 毫秒走 250 毫秒
            clock.advance(250)
            position += 250
            record.samplePlayhead(position, active: true) { "network" }
        }
        for _ in 0 ..< 8 {           // 播放头 2 秒不走
            clock.advance(250)
            record.samplePlayhead(position, active: true) { "network" }
        }
        clock.advance(250)
        position += 250
        record.samplePlayhead(position, active: true) { "network" }
        #expect(record.rebufferCount == 1)
        #expect(record.rebufferMs == 2250)
        let interruptions = try #require(try detail(payload(record))["interruptions"] as? [[String: Any]])
        #expect(interruptions.first?["kind"] as? String == "rebuffer")
        #expect(interruptions.first?["cause"] as? String == "network")
    }

    @Test func pausesSeeksAndShortHiccupsAreNotStalls() {
        let clock = FakeClock()
        let record = makeRecord(clock)
        record.noteFirstFrame()
        // 用户暂停（active = false）
        for _ in 0 ..< 8 {
            clock.advance(250)
            record.samplePlayhead(1000, active: false) { "network" }
        }
        // 跳转中的等待算跳转耗时
        record.beginSeek(source: .button, fromMs: 1000, toMs: 11_000, buffered: false, paused: false, restart: false)
        for _ in 0 ..< 8 {
            clock.advance(250)
            record.samplePlayhead(1000, active: true) { "network" }
        }
        _ = record.seekPresented()
        // 0.25 秒的停顿不到线
        clock.advance(250)
        record.samplePlayhead(11_000, active: true) { "network" }
        clock.advance(250)
        record.samplePlayhead(11_250, active: true) { "network" }
        #expect(record.rebufferCount == 0)
    }

    @Test func stuckSwitchesAndSeeksTimeOutAndStopBlockingStallDetection() throws {
        let clock = FakeClock()
        let record = makeRecord(clock)
        record.noteFirstFrame()
        record.beginSwitch(kind: "audio", from: "embedded:0", to: "embedded:1")   // 结束事件丢了
        record.beginSeek(source: .button, fromMs: 0, toMs: 10_000, buffered: false, paused: false, restart: false)
        for _ in 0 ..< 130 {          // 32.5 秒
            clock.advance(250)
            record.samplePlayhead(10_000, active: true) { "network" }
        }
        #expect(!record.seekInFlight)
        let seeks = try #require(try detail(payload(record))["seeks"] as? [[String: Any]])
        #expect(seeks.first?["outcome"] as? String == "timeout")
        // 两者作废 / 超时之后，播放头再不走就算卡顿了
        #expect(record.rebufferCount == 0)
        clock.advance(250)
        record.samplePlayhead(10_250, active: true) { "network" }
        #expect(record.rebufferCount == 1)
    }

    @Test func aReconnectStillOpenAtExitIsRecorded() throws {
        let clock = FakeClock()
        let record = makeRecord(clock)
        record.noteFirstFrame()
        record.beginReconnect(reason: "连续 15 秒没有收到数据")
        clock.advance(60_000)                 // 一直没接回来，最后落到错误页
        let interruptions = try #require(try detail(payload(record, outcome: .failed))["interruptions"] as? [[String: Any]])
        let reconnect = try #require(interruptions.first { $0["kind"] as? String == "reconnect" })
        #expect(reconnect["ms"] as? Int == 60_000)
    }

    @Test func frozenPictureWhileTheClockRuns() {
        let clock = FakeClock()
        let record = makeRecord(clock)
        record.noteFirstFrame()
        record.sampleFrames(presented: 100, positionMs: 1000, active: true)
        clock.advance(1000)
        record.sampleFrames(presented: 125, positionMs: 2000, active: true)   // 正常
        clock.advance(1000)
        record.sampleFrames(presented: 125, positionMs: 3000, active: true)   // 时钟走了一秒，一帧没出
        clock.advance(1000)
        record.sampleFrames(presented: 125, positionMs: 4000, active: true)
        clock.advance(1000)
        record.sampleFrames(presented: 150, positionMs: 5000, active: true)   // 恢复
        #expect(record.sawFreeze)
    }

    // MARK: 结局与上报

    @Test func outcomeFollowsWhatTheUserSaw() {
        let clock = FakeClock()
        let record = makeRecord(clock)
        #expect(record.outcome(phaseIsError: false, phaseIsEnded: false, positionMs: 0, durationMs: nil) == .exitBeforeStart)
        #expect(record.outcome(phaseIsError: true, phaseIsEnded: false, positionMs: 0, durationMs: nil) == .failed)
        record.noteFirstFrame()
        #expect(record.outcome(phaseIsError: false, phaseIsEnded: false, positionMs: 600_000, durationMs: 7_200_000) == .exited)
        #expect(record.outcome(phaseIsError: false, phaseIsEnded: false, positionMs: 7_150_000, durationMs: 7_200_000) == .watched)
        #expect(record.outcome(phaseIsError: false, phaseIsEnded: true, positionMs: 0, durationMs: nil) == .watched)
    }

    @Test func payloadCarriesTheAttemptAndSnakeCaseDetail() throws {
        let clock = FakeClock()
        let record = makeRecord(clock)
        record.tier = 0
        record.route = "loopback"
        record.noteFirstFrame()
        record.noteAudioChange(from: "embedded:1", to: "embedded:2")
        record.noteDelivery(PlaybackRecord.Delivery(
            route: "server_transcode", tier: 3, userCapped: false, fallbackReason: nil,
            video: .init(sourceFormat: "hdr10", outputFormat: "sdr"),
            audio: .init(sourceCodec: "truehd", sourceLossless: true, outputCodec: "eac3"),
            subtitle: .init(mode: "burned"), output: .init(audioRoute: "hdmi", displayHdr: true)
        ))
        let report = payload(record, outcome: .watched)
        #expect(report.attemptId == record.id)
        #expect(report.client == "ios")
        #expect(report.outcome == "watched")
        let json = try detail(report)
        // 服务端（services/playback/qoe.py）按这些键判定：键名必须一致
        let delivery = try #require(json["delivery"] as? [String: Any])
        #expect(delivery["user_capped"] as? Bool == false)
        #expect((delivery["video"] as? [String: Any])?["output_format"] as? String == "sdr")
        #expect((delivery["audio"] as? [String: Any])?["source_lossless"] as? Bool == true)
        #expect((delivery["output"] as? [String: Any])?["audio_route"] as? String == "hdmi")
        #expect((delivery["subtitle"] as? [String: Any])?["mode"] as? String == "burned")
        let behaviors = try #require(json["behaviors"] as? [[String: Any]])
        #expect(behaviors.first?["misguess"] as? Bool == true)
        #expect(json["context"] != nil && json["timeline"] != nil && json["startup"] != nil)
    }

    // MARK: 规格与服务端耗时

    @Test func losslessDetection() {
        #expect(PlaybackRecord.isLossless(codec: "truehd", names: []))
        #expect(PlaybackRecord.isLossless(codec: "pcm_s24le", names: []))
        #expect(PlaybackRecord.isLossless(codec: "dts", names: ["dts-hd ma 7.1"]))
        #expect(!PlaybackRecord.isLossless(codec: "dts", names: ["dts 5.1"]))
        #expect(!PlaybackRecord.isLossless(codec: "eac3", names: ["atmos"]))
    }

    @Test func hdrLabelsMapToFormatKeys() {
        #expect(PlaybackRecord.formatKey(nil) == "sdr")
        #expect(PlaybackRecord.formatKey("Dolby Vision") == "dolbyvision")
        #expect(PlaybackRecord.formatKey("HDR10") == "hdr10")
        #expect(PlaybackRecord.formatKey("HLG") == "hlg")
    }

    @Test func serverTimingHeaderIsParsed() {
        #expect(PlaybackAPI.serverTiming("total;dur=38, decide;dur=12.4, prep;desc=\"x\";dur=20") ==
                ["total": 38, "decide": 12, "prep": 20])
        #expect(PlaybackAPI.serverTiming(nil).isEmpty)
        #expect(PlaybackAPI.serverTiming("garbage").isEmpty)
    }
}
