import Foundation

/// 一轮转码任务的分段计时（docs/design/transcode-latency.md §2）。
///
/// 转码起播慢，慢在哪一段，单看 NAS 的日志看不出来：NAS 只知道「派了单」和「首片
/// 落盘了」，中间一秒多全在这台 Mac 上。这里以收到 job.start 为零点，记下每个节点
/// 距它的毫秒数：
///
/// | 事件 | 时刻 |
/// |---|---|
/// | `proxy` | 本机上传代理开始监听 |
/// | `ffmpeg` | ffmpeg 进程起来 |
/// | `input` | ffmpeg 读完源的文件头、探测完流（stderr 的 `Input #0`） |
/// | `init_open` | 第一帧过完滤镜、编码器起好，HLS 开写 init.mp4 |
/// | `seg_open` | 一个分片编完、开始交给上传代理（只记前两个） |
/// | `recv` | 代理收齐 ffmpeg 交来的一个产物 |
/// | `up` | 这个产物传到 NAS（带状态码与重试次数） |
///
/// 首片传到 NAS 的那一刻整份发一次 `job.timeline`，任务结束前把之后新记的补发一次；
/// NAS 把它并进会话时间线，与自己记的取源、落盘、交付拼成一条完整的起播链路。
/// 旧版服务端不认识这条消息，按未知消息忽略（只记一行调试日志），不影响转码。
final class JobTimeline: @unchecked Sendable {
    /// 每轮最多记这么多条：起播链路十来条就够，之后的分片不再逐个记。
    static let limit = 24
    /// 逐个记前几个分片（之后的分片节奏稳定，记了也只是重复）。
    static let trackedSegments = 2

    private let origin: UInt64
    private let lock = NSLock()
    private var events: [[String: Any]] = []
    private var sentCount = 0
    /// 已经记过的分片名（按出现顺序），只有前 ``trackedSegments`` 个逐个记。
    private var segmentNames: [String] = []

    init(origin: UInt64 = DispatchTime.now().uptimeNanoseconds) {
        self.origin = origin
    }

    /// 记一个节点。字段要短：整份经控制连接发给 NAS，并出现在诊断接口里。
    func mark(_ event: String, _ fields: [String: Any] = [:]) {
        let now = DispatchTime.now().uptimeNanoseconds
        let ms = now > origin ? Int((now - origin) / 1_000_000) : 0
        lock.withLock {
            guard events.count < Self.limit else { return }
            var entry = fields
            entry["ms"] = ms
            entry["ev"] = event
            events.append(entry)
        }
    }

    /// 记一个产物相关的节点：init.mp4 与前两个分片才记，返回是否记了。
    @discardableResult
    func markArtifact(_ event: String, name: String, _ fields: [String: Any] = [:]) -> Bool {
        guard tracks(name) else { return false }
        var entry = fields
        entry["name"] = name
        mark(event, entry)
        return true
    }

    /// 这个产物要不要逐个记：init 总记；分片只记最先出现的前两个。
    func tracks(_ name: String) -> Bool {
        if name == "init.mp4" { return true }
        guard name.hasPrefix("seg") else { return false }
        return lock.withLock {
            if segmentNames.contains(name) { return true }
            guard segmentNames.count < Self.trackedSegments else { return false }
            segmentNames.append(name)
            return true
        }
    }

    /// 是不是逐个记的分片里的第一个（它传到 NAS 时整份发一次）。
    func isFirstSegment(_ name: String) -> Bool {
        lock.withLock { segmentNames.first == name }
    }

    /// 取出还没发出去的那部分，并记为已发。
    func takeUnsent() -> [[String: Any]] {
        lock.withLock {
            let pending = Array(events[sentCount...])
            sentCount = events.count
            return pending
        }
    }
}

/// 从 ffmpeg 的 stderr 里认出分段计时用的节点。
///
/// 要认节点，ffmpeg 得以 info 级别输出（``JobExecution`` 把 NAS 下发的
/// `-loglevel warning` 改成 `-loglevel level+info -nostats`）：每行带上级别前缀，
/// info 行只拿来认节点、不进 stderr 尾巴，尾巴里仍只有 warning 以上——
/// 与改之前给 NAS 看的诊断内容一致，也不会把带令牌的源地址（`Input #0, … from '…'`）
/// 留进日志。
enum FFmpegLogMarker: Equatable {
    case input
    case initOpen
    case segmentOpen(String)

    /// 这一行是不是 info 级别（`[info]` 前缀出现在组件前缀之后，如 `[hls @ 0x…] [info] …`）。
    static func isInfo(_ line: String) -> Bool {
        line.contains("[info]")
    }

    static func parse(_ line: String) -> FFmpegLogMarker? {
        guard isInfo(line) else { return nil }
        if line.contains("Input #0") {
            return .input
        }
        guard line.contains("Opening '"), line.contains("for writing") else { return nil }
        if line.contains("init.mp4") {
            return .initOpen
        }
        if let range = line.range(of: #"seg[0-9]{5}\.(m4s|ts)"#, options: .regularExpression) {
            return .segmentOpen(String(line[range]))
        }
        return nil
    }

    /// 把 NAS 下发的 `-loglevel warning` 换成带级别前缀的 info，并关掉进度统计行
    /// （进度走 `-progress pipe:1`，stderr 上的统计行只会刷屏）。没有 `-loglevel`
    /// 的参数原样返回——那就认不出节点，计时只少几段，不影响转码。
    static func withInfoLogging(_ arguments: [String]) -> [String] {
        guard let index = arguments.firstIndex(of: "-loglevel"),
              index + 1 < arguments.count,
              arguments[index + 1] == "warning"
        else {
            return arguments
        }
        var rewritten = arguments
        rewritten[index + 1] = "level+info"
        if !rewritten.contains("-nostats") {
            rewritten.insert("-nostats", at: index + 2)
        }
        return rewritten
    }
}
