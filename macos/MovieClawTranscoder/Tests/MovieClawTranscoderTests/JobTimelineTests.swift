import XCTest
@testable import MovieClawTranscoder

final class JobTimelineTests: XCTestCase {
    func testRecognizesStartupMarkersFromInfoLines() {
        // jellyfin-ffmpeg 8.1 以 `-loglevel level+info` 输出的真实行
        XCTAssertEqual(
            FFmpegLogMarker.parse("[info] Input #0, matroska,webm, from 'http://nas/source?token=x':"),
            .input
        )
        XCTAssertEqual(
            FFmpegLogMarker.parse("[hls @ 0x7861058500] [info] Opening 'http://127.0.0.1:5123/init.mp4?token=x' for writing"),
            .initOpen
        )
        XCTAssertEqual(
            FFmpegLogMarker.parse("[hls @ 0x7861058500] [info] Opening 'http://127.0.0.1:5123/seg00150.m4s?token=x' for writing"),
            .segmentOpen("seg00150.m4s")
        )
        // 进度列表的写入不是节点；warning 行一律不认（它们要进尾巴）
        XCTAssertNil(FFmpegLogMarker.parse("[hls @ 0x1] [info] Opening 'http://127.0.0.1:1/live.m3u8.tmp' for writing"))
        XCTAssertNil(FFmpegLogMarker.parse("[h264_videotoolbox @ 0x1] [warning] Color range not set"))
        XCTAssertFalse(FFmpegLogMarker.isInfo("[h264_videotoolbox @ 0x1] [warning] Input #0 looks odd"))
    }

    func testRewritesWarningLogLevelOnly() {
        let rewritten = FFmpegLogMarker.withInfoLogging(["-nostdin", "-loglevel", "warning", "-i", "x"])
        XCTAssertEqual(rewritten, ["-nostdin", "-loglevel", "level+info", "-nostats", "-i", "x"])
        // 不是 NAS 的那种装法就原样不动：认不出节点只少几段计时，不能改坏命令
        XCTAssertEqual(FFmpegLogMarker.withInfoLogging(["-loglevel", "error"]), ["-loglevel", "error"])
        XCTAssertEqual(FFmpegLogMarker.withInfoLogging(["-i", "x"]), ["-i", "x"])
    }

    func testTracksInitAndFirstTwoSegmentsOnly() {
        let timeline = JobTimeline()
        XCTAssertTrue(timeline.markArtifact("recv", name: "init.mp4"))
        XCTAssertTrue(timeline.markArtifact("recv", name: "seg00150.m4s"))
        XCTAssertTrue(timeline.markArtifact("recv", name: "seg00151.m4s"))
        XCTAssertFalse(timeline.markArtifact("recv", name: "seg00152.m4s"))
        // 已经在跟的分片后续节点照记
        XCTAssertTrue(timeline.markArtifact("up", name: "seg00150.m4s"))
        XCTAssertTrue(timeline.isFirstSegment("seg00150.m4s"))
        XCTAssertFalse(timeline.isFirstSegment("seg00151.m4s"))
    }

    func testTakeUnsentHandsOutEachEventOnce() {
        let timeline = JobTimeline()
        timeline.mark("proxy")
        timeline.mark("ffmpeg")
        let first = timeline.takeUnsent()
        XCTAssertEqual(first.map { $0["ev"] as? String }, ["proxy", "ffmpeg"])
        XCTAssertTrue(first.allSatisfy { ($0["ms"] as? Int).map { $0 >= 0 } ?? false })
        XCTAssertTrue(timeline.takeUnsent().isEmpty)
        timeline.mark("input")
        XCTAssertEqual(timeline.takeUnsent().map { $0["ev"] as? String }, ["input"])
    }

    func testStopsRecordingAtTheLimit() {
        let timeline = JobTimeline()
        for index in 0..<(JobTimeline.limit + 10) {
            timeline.mark("e\(index)")
        }
        XCTAssertEqual(timeline.takeUnsent().count, JobTimeline.limit)
    }
}
