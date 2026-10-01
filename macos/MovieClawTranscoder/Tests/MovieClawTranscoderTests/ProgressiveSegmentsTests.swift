import XCTest
@testable import MovieClawTranscoder

/// 用手搭的 MP4 盒子验证切段：与 ffmpeg `-f mp4 -movflags frag_keyframe+delay_moov+default_base_moof
/// +frag_discont` 的输出同一种结构（ftyp + moov，之后一个个 moof + mdat）。
final class ProgressiveSegmentsTests: XCTestCase {
    private let timescale: UInt32 = 12_288

    func testSplitsFragmentsOnTheSegmentGrid() {
        var segmenter = FragmentSegmenter(segmentSeconds: 4, startSegment: 150)
        var outputs = segmenter.append(initSegment())
        XCTAssertEqual(outputs.count, 1)
        guard case let .initSegment(initData) = outputs[0] else { return XCTFail("应先产出 init") }
        XCTAssertEqual(initData, initSegment())

        // 续播从 600 秒起转：第一个片段是关键帧，属于第 150 段
        outputs = segmenter.append(fragment(pts: 600.0, sync: true))
        XCTAssertEqual(outputs.first.map(segmentInfo), .some(Info(segment: 150, starts: true)))
        outputs = segmenter.append(fragment(pts: 600.5, sync: false))
        XCTAssertEqual(outputs.first.map(segmentInfo), .some(Info(segment: 150, starts: false)))
        // 编码器在格子中间自己插的关键帧（场景切换）不是新的一段
        outputs = segmenter.append(fragment(pts: 602.0, sync: true))
        XCTAssertEqual(outputs.first.map(segmentInfo), .some(Info(segment: 150, starts: false)))
        // 604 秒的强制关键帧：第 151 段开头（23.976 帧率时会略晚于格点，照样归进这一段）
        outputs = segmenter.append(fragment(pts: 604.04, sync: true))
        XCTAssertEqual(outputs.first.map(segmentInfo), .some(Info(segment: 151, starts: true)))
    }

    func testFirstSegmentTakesTheNASNumberEvenWhenTheFirstFrameIsEarly() {
        // 片源 start_time 不为零：-ss 1376 起转、-start_at_zero 平移后第一帧在 1375.993。
        // 按时间除以 4 会算成第 343 段，播放器要的第 344 段就永远不来了（真机踩过）。
        // 强制关键帧从第一帧起每 4 秒一个（1379.993……），切点同样从第一帧起算。
        var segmenter = FragmentSegmenter(segmentSeconds: 4, startSegment: 344)
        _ = segmenter.append(initSegment())
        XCTAssertEqual(segmenter.append(fragment(pts: 1375.993, sync: true)).map(segmentInfo),
                       [Info(segment: 344, starts: true)])
        XCTAssertEqual(segmenter.append(fragment(pts: 1376.493, sync: false)).map(segmentInfo),
                       [Info(segment: 344, starts: false)])
        XCTAssertEqual(segmenter.append(fragment(pts: 1379.993, sync: true)).map(segmentInfo),
                       [Info(segment: 345, starts: true)])
        XCTAssertEqual(segmenter.append(fragment(pts: 1383.993, sync: true)).map(segmentInfo),
                       [Info(segment: 346, starts: true)])
    }

    func testHandlesArbitraryByteBoundaries() {
        var stream = initSegment()
        stream.append(fragment(pts: 0, sync: true))
        stream.append(fragment(pts: 0.5, sync: false))
        stream.append(fragment(pts: 4.0, sync: true))
        var segmenter = FragmentSegmenter(segmentSeconds: 4)
        var outputs: [FragmentSegmenter.Output] = []
        // 一次只喂 7 个字节：TCP 怎么拆包都要切得一样
        var cursor = stream.startIndex
        while cursor < stream.endIndex {
            let end = min(cursor + 7, stream.endIndex)
            outputs += segmenter.append(Data(stream[cursor..<end]))
            cursor = end
        }
        XCTAssertEqual(outputs.count, 4)
        XCTAssertEqual(outputs.dropFirst().map(segmentInfo), [
            Info(segment: 0, starts: true),
            Info(segment: 0, starts: false),
            Info(segment: 1, starts: true),
        ])
        guard case let .fragment(_, data, _) = outputs[1] else { return XCTFail() }
        XCTAssertEqual(data, fragment(pts: 0, sync: true), "片段原样交出（moof + mdat）")
    }

    func testRejectsAbsurdBoxSizes() {
        var segmenter = FragmentSegmenter(segmentSeconds: 4)
        var garbage = Data()
        garbage.append(contentsOf: uint32(2))  // 小于盒子头本身
        garbage.append(contentsOf: Array("moof".utf8))
        XCTAssertTrue(segmenter.append(garbage).isEmpty)
        XCTAssertNotNil(segmenter.failure)
    }

    // MARK: - 盒子

    private struct Info: Equatable {
        let segment: Int
        let starts: Bool
    }

    private func segmentInfo(_ output: FragmentSegmenter.Output) -> Info {
        guard case let .fragment(segment, _, starts) = output else { return Info(segment: -1, starts: false) }
        return Info(segment: segment, starts: starts)
    }

    private func box(_ kind: String, _ payload: Data) -> Data {
        var data = Data(uint32(UInt32(8 + payload.count)))
        data.append(contentsOf: Array(kind.utf8))
        data.append(payload)
        return data
    }

    private func fullBox(_ kind: String, version: UInt8 = 0, flags: UInt32 = 0, _ payload: Data) -> Data {
        var body = Data([version])
        body.append(contentsOf: uint32(flags).suffix(3))
        body.append(payload)
        return box(kind, body)
    }

    private func uint32(_ value: UInt32) -> [UInt8] {
        [UInt8(value >> 24 & 0xFF), UInt8(value >> 16 & 0xFF), UInt8(value >> 8 & 0xFF), UInt8(value & 0xFF)]
    }

    private func uint64(_ value: UInt64) -> [UInt8] {
        uint32(UInt32(value >> 32)) + uint32(UInt32(value & 0xFFFF_FFFF))
    }

    private func initSegment() -> Data {
        var tkhd = Data(uint32(0) + uint32(0))  // creation / modification（version 0）
        tkhd.append(contentsOf: uint32(1))  // track_ID
        tkhd.append(Data(count: 72))
        var mdhd = Data(uint32(0) + uint32(0))
        mdhd.append(contentsOf: uint32(timescale))
        mdhd.append(Data(count: 8))
        var hdlr = Data(uint32(0))
        hdlr.append(contentsOf: Array("vide".utf8))
        hdlr.append(Data(count: 13))
        let mdia = box("mdia", fullBox("mdhd", mdhd) + fullBox("hdlr", hdlr))
        let trak = box("trak", fullBox("tkhd", flags: 3, tkhd) + mdia)
        return box("ftyp", Data(Array("isom".utf8) + uint32(512))) + box("moov", trak)
    }

    private func fragment(pts: Double, sync: Bool) -> Data {
        let decode = UInt64((pts * Double(timescale)).rounded())
        let tfhd = fullBox("tfhd", flags: 0x020000, Data(uint32(1)))
        let tfdt = fullBox("tfdt", version: 1, Data(uint64(decode)))
        // trun：带 data_offset 与 first_sample_flags（ffmpeg 的关键帧片段就是这样写的）
        var trun = Data(uint32(1))  // sample_count
        trun.append(contentsOf: uint32(0))  // data_offset
        trun.append(contentsOf: uint32(sync ? 0x0200_0000 : 0x0101_0000))
        let traf = box("traf", tfhd + tfdt + fullBox("trun", flags: 0x05, trun))
        let moof = box("moof", fullBox("mfhd", Data(uint32(1))) + traf)
        return moof + box("mdat", Data(repeating: 0xAB, count: 32))
    }
}
