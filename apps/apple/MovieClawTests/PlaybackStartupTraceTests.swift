import Foundation
import Testing
@testable import MovieClaw

/// 起播分段计时（PlaybackStartupTrace.swift）的纯逻辑：起点只定一次、同名点只记第一次、
/// 事后补记的点按真实时刻排序、每个单元只上报一次。
struct PlaybackStartupTraceTests {
    @Test func marksAreRelativeToTheFirstBeginAndDeduplicated() {
        var trace = StartupTrace()
        let origin = ContinuousClock.now
        trace.begin(at: origin)
        trace.begin(at: origin + .seconds(5)) // 降档重来：不重置起点
        trace.mark("会话", at: origin + .milliseconds(160))
        trace.mark("会话", at: origin + .milliseconds(900)) // 引擎事件重复到达：只认第一次
        #expect(trace.marks.map(\.name) == ["会话"])
        #expect(trace.marks.first?.ms == 160)
    }

    @Test func lateMarksAreSortedByWhenTheyHappened() {
        var trace = StartupTrace()
        let origin = ContinuousClock.now
        trace.begin(at: origin)
        trace.mark("出现", at: origin + .milliseconds(30))
        // 后台协商的各段回到主线程才补记，时刻早于已经记下的点
        trace.mark("会话", at: origin + .milliseconds(160))
        trace.mark("决策", at: origin + .milliseconds(120))
        #expect(trace.marks.map(\.name) == ["出现", "决策", "会话"])
        #expect(StartupTrace.summary(trace.marks) == "出现 30 → 决策 120 → 会话 160 毫秒")
    }

    @Test func reportsOnlyOnceAndIgnoresMarksWithoutAnOrigin() {
        var idle = StartupTrace()
        idle.mark("首帧")
        #expect(idle.marks.isEmpty)
        #expect(idle.finish() == nil)

        var trace = StartupTrace()
        trace.begin()
        trace.mark("首帧")
        #expect(trace.finish()?.count == 1)
        #expect(trace.finish() == nil)
        trace.mark("播放") // 报完之后的点不再记
        #expect(trace.marks.map(\.name) == ["首帧"])
    }
}
