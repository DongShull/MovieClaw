import Foundation

/// 把 ffmpeg 输出的分片化 MP4 切成 HLS 的 init 与一个个分片（docs/design/transcode-latency.md §5）。
///
/// 为什么不用 ffmpeg 自己的 HLS muxer：它把一整段（4 秒）攒在内存里，转完才写出，播放器
/// 最早也要等一整段才能出画——转码起播与跳转首帧里最大的一块。改让 ffmpeg 输出分片化 MP4
/// （`-movflags frag_keyframe+delay_moov+default_base_moof+frag_discont -frag_duration 0.5s`）：
/// 每 0.5 秒吐出一个片段（moof + mdat），在这里按 NAS 预生成播放列表的同一个栅格归到第几段，
/// 一个片段一个片段地回传。AVPlayer 收到一个完整片段就能解码。
///
/// 切段规则与 ffmpeg 的 HLS muxer 一模一样（这是播放列表与分片对得上的前提）：第一段的编号
/// 是 NAS 给的起转分片号；之后从这一轮第一帧起算，满一格（4 秒）后的第一个同步帧开新的一段，
/// 编号顺延。不能拿时间戳直接除以 4 取整：`-force_key_frames expr:gte(t,n_forced*4)` 的 t 是
/// 从这一轮第一帧起算的（实测从 41 秒起转，关键帧在 41、45、49……），片源 start_time 不为零时
/// 第一帧也会比格点早一点点——除法会把第一段算成前一个编号，播放器要的那一段永远不来。
/// `frag_keyframe` 让每个关键帧另起一个片段，所以分片边界一定是片段边界；编码器自己插的关键帧
/// （场景切换）不满一格，不会被当成新的一段。
struct FragmentSegmenter {
    enum Output: Equatable {
        /// ftyp + moov：HLS 的 init.mp4
        case initSegment(Data)
        /// 一个片段（moof + mdat）属于第几段；`startsSegment` 为真时它是这一段的第一个片段
        case fragment(segment: Int, data: Data, startsSegment: Bool)
    }

    let segmentSeconds: Double
    /// 这一轮从第几段起转（NAS 在 job.start 里给）；没给时按第一帧的时间四舍五入
    let startSegment: Int?
    /// 单个顶层盒子的上限：防止坏数据让缓冲无限长（正常片段远小于此）
    static let maxBoxBytes = 256 * 1024 * 1024

    private var buffer = Data()
    private var ftyp: Data?
    private var videoTrackID: UInt32?
    private var timescale: Double = 90_000
    private var pendingMoof: Data?
    /// 第一个视频片段之前的片段（音频比视频早几毫秒开头时会单独成片）：并进第一段的开头
    private var leadingFragments = Data()
    private(set) var currentSegment: Int?
    private(set) var failure: String?
    /// 这一轮第一帧的显示时间，与第一段的编号：之后的切点都从它起算
    private var runStart: (pts: Double, segment: Int)?

    init(segmentSeconds: Double, startSegment: Int? = nil) {
        self.segmentSeconds = segmentSeconds
        self.startSegment = startSegment
    }

    /// 喂进一段字节，返回这一段里凑齐的产出（init 或片段）。
    mutating func append(_ data: Data) -> [Output] {
        guard failure == nil else { return [] }
        buffer.append(data)
        var outputs: [Output] = []
        while let (kind, box) = nextBox() {
            switch kind {
            case "ftyp":
                ftyp = box
            case "moov":
                parseMoov(box)
                outputs.append(.initSegment((ftyp ?? Data()) + box))
            case "moof":
                pendingMoof = box
            case "mdat":
                guard let moof = pendingMoof else { continue }
                pendingMoof = nil
                if let output = place(moof: moof, fragment: moof + box) {
                    outputs.append(output)
                }
            default:
                // styp / sidx / mfra / free：HLS 用不到
                continue
            }
        }
        return outputs
    }

    /// 取出一个完整的顶层盒子；不够一个就留在缓冲里等下一次。
    private mutating func nextBox() -> (String, Data)? {
        guard buffer.count >= 8 else { return nil }
        let start = buffer.startIndex
        var size = UInt64(buffer.readUInt32(at: start))
        let kind = String(decoding: buffer[(start + 4)..<(start + 8)], as: UTF8.self)
        if size == 1 {
            guard buffer.count >= 16 else { return nil }
            size = buffer.readUInt64(at: start + 8)
        }
        guard size >= 8, size <= UInt64(Self.maxBoxBytes) else {
            failure = "分片化 MP4 里出现了长度不合理的盒子（\(kind) \(size) 字节）"
            buffer.removeAll()
            return nil
        }
        guard UInt64(buffer.count) >= size else { return nil }
        let box = Data(buffer[start..<(start + Int(size))])
        buffer.removeSubrange(start..<(start + Int(size)))
        return (kind, box)
    }

    /// 片段归到第几段；还没见到视频（只有音频的开头片段）时先攒着，返回 nil。
    private mutating func place(moof: Data, fragment: Data) -> Output? {
        let video = videoTiming(in: moof)
        guard let start = runStart else {
            guard let video else {
                leadingFragments.append(fragment)
                return nil
            }
            // 这一轮的第一个视频片段：第一段的开头，视频之前攒下的片段一起带上
            let index = startSegment ?? Int((video.pts / segmentSeconds).rounded())
            runStart = (video.pts, index)
            currentSegment = index
            let data = leadingFragments + fragment
            leadingFragments = Data()
            return .fragment(segment: index, data: data, startsSegment: true)
        }
        guard let current = currentSegment else { return nil }
        if let video, video.isSync {
            // 与 HLS muxer 同一条：从这一轮第一帧起，满 n 格后的第一个同步帧开第 n 段
            let due = start.pts + Double(current - start.segment + 1) * segmentSeconds - 0.001
            if video.pts >= due {
                currentSegment = current + 1
                return .fragment(segment: current + 1, data: fragment, startsSegment: true)
            }
        }
        return .fragment(segment: current, data: fragment, startsSegment: false)
    }

    // MARK: - 盒子解析

    private mutating func parseMoov(_ moov: Data) {
        for trak in children(of: moov, header: 8) where trak.kind == "trak" {
            var trackID: UInt32?
            var handler: String?
            var scale: UInt32?
            for child in children(of: trak.data, header: 8) {
                if child.kind == "tkhd" {
                    let version = child.data[child.data.startIndex + 8]
                    let offset = child.data.startIndex + 12 + (version == 1 ? 16 : 8)
                    trackID = child.data.readUInt32(at: offset)
                } else if child.kind == "mdia" {
                    for item in children(of: child.data, header: 8) {
                        if item.kind == "mdhd" {
                            let version = item.data[item.data.startIndex + 8]
                            let offset = item.data.startIndex + 12 + (version == 1 ? 16 : 8)
                            scale = item.data.readUInt32(at: offset)
                        } else if item.kind == "hdlr" {
                            let base = item.data.startIndex + 16
                            handler = String(decoding: item.data[base..<(base + 4)], as: UTF8.self)
                        }
                    }
                }
            }
            if handler == "vide", let trackID {
                videoTrackID = trackID
                if let scale, scale > 0 { timescale = Double(scale) }
            }
        }
    }

    private struct Timing {
        let pts: Double
        let isSync: Bool
    }

    /// 视频轨在这个片段里第一个样本的显示时间与是否同步帧。
    private func videoTiming(in moof: Data) -> Timing? {
        for traf in children(of: moof, header: 8) where traf.kind == "traf" {
            var trackID: UInt32?
            var defaultFlags: UInt32?
            var base: UInt64 = 0
            var firstFlags: UInt32?
            var compositionOffset: Int64 = 0
            for box in children(of: traf.data, header: 8) {
                let data = box.data
                let start = data.startIndex
                switch box.kind {
                case "tfhd":
                    let flags = data.readUInt32(at: start + 8) & 0x00FF_FFFF
                    trackID = data.readUInt32(at: start + 12)
                    var cursor = start + 16
                    if flags & 0x01 != 0 { cursor += 8 }
                    if flags & 0x02 != 0 { cursor += 4 }
                    if flags & 0x08 != 0 { cursor += 4 }
                    if flags & 0x10 != 0 { cursor += 4 }
                    if flags & 0x20 != 0 { defaultFlags = data.readUInt32(at: cursor) }
                case "tfdt":
                    base = data[start + 8] == 1
                        ? data.readUInt64(at: start + 12)
                        : UInt64(data.readUInt32(at: start + 12))
                case "trun":
                    let word = data.readUInt32(at: start + 8)
                    let version = word >> 24
                    let flags = word & 0x00FF_FFFF
                    var cursor = start + 16
                    if flags & 0x01 != 0 { cursor += 4 }
                    if flags & 0x04 != 0 {
                        firstFlags = data.readUInt32(at: cursor)
                        cursor += 4
                    }
                    if flags & 0x100 != 0 { cursor += 4 }
                    if flags & 0x200 != 0 { cursor += 4 }
                    if flags & 0x400 != 0 {
                        if firstFlags == nil { firstFlags = data.readUInt32(at: cursor) }
                        cursor += 4
                    }
                    if flags & 0x800 != 0 {
                        let raw = data.readUInt32(at: cursor)
                        compositionOffset = version == 0 ? Int64(raw) : Int64(Int32(bitPattern: raw))
                    }
                default:
                    break
                }
            }
            guard trackID == videoTrackID else { continue }
            let flags = firstFlags ?? defaultFlags ?? 0
            // sample_is_non_sync_sample 是第 16 位
            let isSync = flags & 0x0001_0000 == 0
            return Timing(pts: (Double(base) + Double(compositionOffset)) / timescale, isSync: isSync)
        }
        return nil
    }

    private struct Child {
        let kind: String
        let data: Data
    }

    /// 一个盒子的子盒子（`header` 是父盒子头的长度）。
    private func children(of box: Data, header: Int) -> [Child] {
        var result: [Child] = []
        var cursor = box.startIndex + header
        while cursor + 8 <= box.endIndex {
            let size = Int(box.readUInt32(at: cursor))
            guard size >= 8, cursor + size <= box.endIndex else { break }
            let kind = String(decoding: box[(cursor + 4)..<(cursor + 8)], as: UTF8.self)
            result.append(Child(kind: kind, data: Data(box[cursor..<(cursor + size)])))
            cursor += size
        }
        return result
    }
}

private extension Data {
    func readUInt32(at index: Index) -> UInt32 {
        self[index..<(index + 4)].reduce(0) { $0 << 8 | UInt32($1) }
    }

    func readUInt64(at index: Index) -> UInt64 {
        self[index..<(index + 8)].reduce(0) { $0 << 8 | UInt64($1) }
    }
}
