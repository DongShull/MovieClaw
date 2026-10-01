// 转码链路实验台的客户端探针（docs/design/transcode-latency.md §3）。
//
// 用 macOS 上的系统 AVPlayer 当客户端——与 iOS App「限画质 → 服务端转码流」那条路
// （自研引擎的 remoteBypass，AVPlayer 直接取服务端 HLS）是同一套 AVFoundation——量用户
// 看得见的两个数：
//
// - 起播首帧：从发出「开始播放」请求（POST /playback/sessions）到第一帧可上屏；
// - 跳转出画：从发起 seek 到落点那一帧可上屏。
//
// 另有 `segments` 模式模拟 hls.js：它要把一个分片整段收完才喂给解码器，所以网页端的
// 「首帧」近似等于「首片整段到齐」。
//
// 协议：标准输入一行一个 JSON 指令，标准输出一行一个 JSON 事件。时间一律是
// DispatchTime 的 uptime 纳秒（mach_absolute_time），与 Python 的 time.monotonic_ns()
// 是同一个时钟——实验台的取流代理按同一时钟记请求，两边可以直接对齐。
//
// 编译：swiftc -O scripts/perf/transcode_probe.swift -o <输出>（transcode_lab.py 会自动做）

import AVFoundation
import CoreMedia
import Foundation
import QuartzCore

// MARK: - 输出

let outputQueue = DispatchQueue(label: "probe.output")

func now() -> UInt64 { DispatchTime.now().uptimeNanoseconds }

func emit(_ object: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else { return }
    outputQueue.sync {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([10]))
    }
}

// MARK: - 一次运行的参数

struct SeekStep {
    let label: String
    /// 首帧（或上一次跳转落地）之后再等多少秒才跳
    let after: Double
    /// 目标位置（文件绝对时间，秒）
    let target: Double
}

struct RunSpec {
    let id: String
    let base: URL
    let cookie: String
    let body: [String: Any]
    let mode: String
    let seeks: [SeekStep]
    /// 最后一次落地后再看几秒（看有没有卡）
    let tail: Double
    let timeout: Double

    /// 直接给播放地址时不开会话（本机实验：自己起的 HLS 服务）
    let directURL: URL?

    init?(_ json: [String: Any]) {
        guard let id = json["id"] as? String,
              let baseText = json["base"] as? String, let base = URL(string: baseText)
        else { return nil }
        let directURL = (json["url"] as? String).flatMap(URL.init(string:))
        guard let body = json["body"] as? [String: Any] ?? (directURL != nil ? [:] : nil) else { return nil }
        self.id = id
        self.base = base
        self.directURL = directURL
        self.cookie = json["cookie"] as? String ?? ""
        self.body = body
        self.mode = json["mode"] as? String ?? "avplayer"
        self.seeks = (json["seeks"] as? [[String: Any]] ?? []).compactMap { step in
            guard let target = step["to"] as? Double else { return nil }
            return SeekStep(
                label: step["label"] as? String ?? "seek",
                after: step["after"] as? Double ?? 5,
                target: target
            )
        }
        self.tail = json["tail"] as? Double ?? 4
        self.timeout = json["timeout"] as? Double ?? 45
    }
}

// MARK: - 网络

/// 开会话与取分片共用的会话：与 App 的 playbackSession 一样常驻、复用连接。
let apiSession: URLSession = {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.httpShouldSetCookies = false
    configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
    configuration.urlCache = nil
    configuration.timeoutIntervalForRequest = 60
    configuration.httpMaximumConnectionsPerHost = 6
    return URLSession(configuration: configuration)
}()

struct Fetched {
    let status: Int
    let data: Data
    let headers: [AnyHashable: Any]
    let tSent: UInt64
    let tFirstByte: UInt64
    let tDone: UInt64
}

/// 记下首字节时刻的取数：URLSessionDataDelegate 收到响应头即首字节。
final class TimedFetch: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private var buffer = Data()
    private var firstByte: UInt64 = 0
    private var response: HTTPURLResponse?
    private var continuation: CheckedContinuation<Fetched, Error>?
    private let tSent: UInt64

    init(tSent: UInt64) { self.tSent = tSent }

    static let session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.urlCache = nil
        configuration.timeoutIntervalForRequest = 60
        configuration.httpMaximumConnectionsPerHost = 6
        return URLSession(configuration: configuration, delegate: Router.shared, delegateQueue: nil)
    }()

    /// URLSession 的 delegate 是会话级的，按任务分发给各自的 TimedFetch。
    final class Router: NSObject, URLSessionDataDelegate, @unchecked Sendable {
        static let shared = Router()
        private let lock = NSLock()
        private var handlers: [Int: TimedFetch] = [:]

        func register(_ task: URLSessionTask, _ handler: TimedFetch) {
            lock.lock(); handlers[task.taskIdentifier] = handler; lock.unlock()
        }

        private func handler(_ task: URLSessionTask) -> TimedFetch? {
            lock.lock(); defer { lock.unlock() }
            return handlers[task.taskIdentifier]
        }

        func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
            handler(dataTask)?.receivedResponse(response)
            completionHandler(.allow)
        }

        func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
            handler(dataTask)?.receivedData(data)
        }

        func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
            let target = handler(task)
            lock.lock(); handlers.removeValue(forKey: task.taskIdentifier); lock.unlock()
            target?.finished(error)
        }
    }

    func receivedResponse(_ response: URLResponse) {
        lock.lock(); firstByte = now(); self.response = response as? HTTPURLResponse; lock.unlock()
    }

    func receivedData(_ data: Data) {
        lock.lock(); buffer.append(data); lock.unlock()
    }

    func finished(_ error: Error?) {
        lock.lock()
        let continuation = self.continuation
        self.continuation = nil
        let result = Fetched(
            status: response?.statusCode ?? 0,
            data: buffer,
            headers: response?.allHeaderFields ?? [:],
            tSent: tSent,
            tFirstByte: firstByte,
            tDone: now()
        )
        lock.unlock()
        if let error { continuation?.resume(throwing: error) } else { continuation?.resume(returning: result) }
    }

    static func get(_ request: URLRequest) async throws -> Fetched {
        let handler = TimedFetch(tSent: now())
        return try await withCheckedThrowingContinuation { continuation in
            handler.lock.lock(); handler.continuation = continuation; handler.lock.unlock()
            let task = session.dataTask(with: request)
            Router.shared.register(task, handler)
            task.resume()
        }
    }
}

func resolve(_ text: String, against base: URL) -> URL {
    URL(string: text, relativeTo: base)!.absoluteURL
}

// MARK: - 开会话

struct OpenedSession {
    let sessionID: String?
    let masterURL: URL?
    let streamURL: URL?
    let event: [String: Any]
}

func openSession(_ spec: RunSpec) async -> OpenedSession {
    var request = URLRequest(url: spec.base.appendingPathComponent("api/v1/playback/sessions"))
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    if !spec.cookie.isEmpty { request.setValue(spec.cookie, forHTTPHeaderField: "Cookie") }
    request.httpBody = try? JSONSerialization.data(withJSONObject: spec.body)
    var event: [String: Any] = ["id": spec.id, "ev": "session"]
    do {
        let fetched = try await TimedFetch.get(request)
        event["t_sent"] = fetched.tSent
        event["t_resp"] = fetched.tFirstByte
        event["t_done"] = fetched.tDone
        event["status"] = fetched.status
        event["server_timing"] = fetched.headers.first { ($0.key as? String)?.lowercased() == "server-timing" }?.value as? String ?? ""
        guard let json = try? JSONSerialization.jsonObject(with: fetched.data) as? [String: Any],
              let data = json["data"] as? [String: Any]
        else {
            event["error"] = String(data: fetched.data.prefix(400), encoding: .utf8) ?? "?"
            return OpenedSession(sessionID: nil, masterURL: nil, streamURL: nil, event: event)
        }
        let decision = data["decision"] as? [String: Any] ?? [:]
        event["session_id"] = data["session_id"] as? String ?? NSNull()
        event["master_url"] = data["master_url"] as? String ?? NSNull()
        event["tier"] = decision["tier"] ?? NSNull()
        event["outcome"] = decision["outcome"] ?? NSNull()
        event["hw_backend"] = data["hw_backend"] ?? NSNull()
        event["start_ms"] = data["start_ms"] ?? NSNull()
        event["video"] = decision["video"] ?? NSNull()
        event["audio"] = decision["audio"] ?? NSNull()
        if decision["outcome"] as? String != "plan" {
            event["error"] = "决策不是 plan：\(decision["outcome"] ?? "?") \(decision["reason"] ?? "")"
        }
        let master = (data["master_url"] as? String).map { resolve($0, against: spec.base) }
        let stream = (data["stream_url"] as? String).map { resolve($0, against: spec.base) }
        return OpenedSession(sessionID: data["session_id"] as? String, masterURL: master, streamURL: stream, event: event)
    } catch {
        event["error"] = "开会话失败：\(error.localizedDescription)"
        return OpenedSession(sessionID: nil, masterURL: nil, streamURL: nil, event: event)
    }
}

// MARK: - AVPlayer 模式

/// 盯一个 AVPlayer：首帧、开播、每次跳转的落点出画、卡顿。
///
/// 出画用两路信号互相印证：AVPlayerLayer.isReadyForDisplay（App 的起播首帧口径）与
/// AVPlayerItemVideoOutput 第一次给出像素缓冲（带出画那一帧的时间戳，跳转用它判断
/// 「是不是落点的帧」）。
final class PlayerWatch: NSObject, @unchecked Sendable {
    let player: AVPlayer
    let item: AVPlayerItem
    let layer: AVPlayerLayer
    let output: AVPlayerItemVideoOutput
    private let lock = NSLock()
    private var observations: [NSKeyValueObservation] = []
    private var timer: DispatchSourceTimer?
    private let id: String

    // 状态（lock 保护）
    private(set) var readyToPlayAt: UInt64 = 0
    private(set) var layerReadyAt: UInt64 = 0
    private(set) var firstPixelAt: UInt64 = 0
    private(set) var firstPixelPTS: Double = -1
    private(set) var firstPlayingAt: UInt64 = 0
    private(set) var failed: String?
    private var lastPixelPTS: Double = -1
    private var lastPixelAt: UInt64 = 0
    /// 正在等的跳转：目标、发起时刻；出画后记下
    private var seekTarget: Double?
    private var seekFrameAt: UInt64 = 0
    private var seekFramePTS: Double = -1
    private var seekPlayingAt: UInt64 = 0
    private var stalls: [[String: Any]] = []
    private var stallStart: UInt64 = 0
    private var stallReason = ""
    private var everPlaying = false
    private var seeking = false

    init(url: URL, id: String) {
        self.id = id
        let asset = AVURLAsset(url: url)
        item = AVPlayerItem(asset: asset)
        // 与 App 的 remoteBypass 一致：远程 HLS 用系统自适应的前向缓冲
        item.preferredForwardBufferDuration = 0
        output = AVPlayerItemVideoOutput(pixelBufferAttributes: nil)
        item.add(output)
        player = AVPlayer(playerItem: item)
        layer = AVPlayerLayer(player: player)
        super.init()
        // 状态一律在 2 毫秒的轮询里读：命令行进程里 AVPlayerItem.status / timeControlStatus 的
        // KVO 实测不来，isReadyForDisplay 的来——统一轮询，口径一致、精度 2 毫秒
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "probe.poll", qos: .userInteractive))
        timer.schedule(deadline: .now(), repeating: .milliseconds(2), leeway: .microseconds(500))
        timer.setEventHandler { [weak self] in self?.poll() }
        timer.resume()
        self.timer = timer
    }

    private func mark(_ body: () -> Void) {
        lock.lock(); body(); lock.unlock()
    }

    /// 只在轮询队列上读写（串行），不需要锁
    private var lastControlStatus: AVPlayer.TimeControlStatus = .paused

    private func statusChanged(_ status: AVPlayer.TimeControlStatus, reason: String, at t: UInt64) {
        mark {
            switch status {
            case .playing:
                if firstPlayingAt == 0 { firstPlayingAt = t }
                if seekTarget != nil, seekPlayingAt == 0, !seeking { seekPlayingAt = t }
                if stallStart != 0 {
                    stalls.append(["start": stallStart, "end": t, "reason": stallReason])
                    stallStart = 0
                }
                everPlaying = true
            case .waitingToPlayAtSpecifiedRate:
                // 起播前与跳转中的等待不算卡顿（与 App 的 QoE 口径一致）
                if everPlaying, !seeking, seekTarget == nil || seekFrameAt != 0, stallStart == 0 {
                    stallStart = t
                    stallReason = reason
                }
            default:
                break
            }
        }
    }

    private func poll() {
        let tPoll = now()
        let control = player.timeControlStatus
        if control != lastControlStatus {
            lastControlStatus = control
            statusChanged(control, reason: player.reasonForWaitingToPlay?.rawValue ?? "", at: tPoll)
        }
        switch item.status {
        case .readyToPlay:
            mark { if readyToPlayAt == 0 { readyToPlayAt = tPoll } }
        case .failed:
            let message = item.error.map { "\($0)" } ?? "AVPlayerItem 失败"
            mark { if failed == nil { failed = message } }
        default:
            break
        }
        if layer.isReadyForDisplay {
            mark { if layerReadyAt == 0 { layerReadyAt = tPoll } }
        }
        let hostTime = CACurrentMediaTime()
        let itemTime = output.itemTime(forHostTime: hostTime)
        guard output.hasNewPixelBuffer(forItemTime: itemTime) else { return }
        var display = CMTime.invalid
        guard output.copyPixelBuffer(forItemTime: itemTime, itemTimeForDisplay: &display) != nil else { return }
        let t = now()
        let pts = display.isValid ? display.seconds : itemTime.seconds
        mark {
            if firstPixelAt == 0 {
                firstPixelAt = t
                firstPixelPTS = pts
            }
            lastPixelAt = t
            lastPixelPTS = pts
            if let target = seekTarget, seekFrameAt == 0, !seeking || pts >= target - 0.25 {
                // 落点那一帧：时间戳落在目标附近（精确跳转从目标所在分片的关键帧解到目标）
                if pts >= target - 0.25, pts <= target + 3 {
                    seekFrameAt = t
                    seekFramePTS = pts
                }
            }
        }
    }

    func snapshot() -> [String: Any] {
        lock.lock(); defer { lock.unlock() }
        return [
            "ready_to_play": readyToPlayAt,
            "layer_ready": layerReadyAt,
            "first_pixel": firstPixelAt,
            "first_pixel_pts": firstPixelPTS,
            "first_playing": firstPlayingAt,
            "failed": failed ?? NSNull(),
        ]
    }

    var hasFirstFrame: Bool {
        lock.lock(); defer { lock.unlock() }
        return firstPixelAt != 0 || layerReadyAt != 0
    }

    var failure: String? {
        lock.lock(); defer { lock.unlock() }
        return failed
    }

    /// 发起一次精确跳转，等到落点出画（或超时），返回这次跳转的记录。
    func seek(to target: Double, label: String, timeout: Double) async -> [String: Any] {
        let tStart = now()
        mark {
            seekTarget = target
            seekFrameAt = 0
            seekFramePTS = -1
            seekPlayingAt = 0
            seeking = true
        }
        let tCompleted: UInt64 = await withCheckedContinuation { continuation in
            player.seek(
                to: CMTime(seconds: target, preferredTimescale: 600),
                toleranceBefore: .zero,
                toleranceAfter: .zero
            ) { _ in
                continuation.resume(returning: now())
            }
        }
        mark { seeking = false }
        if player.timeControlStatus != .playing { player.play() }
        let deadline = tStart + UInt64(timeout * 1e9)
        while now() < deadline {
            let done = lock.withLock { seekFrameAt != 0 }
            if done { break }
            try? await Task.sleep(nanoseconds: 2_000_000)
        }
        // 再给开播一点时间
        let playDeadline = now() + 3_000_000_000
        while now() < playDeadline {
            let playing = lock.withLock { seekPlayingAt != 0 }
            if playing { break }
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        return lock.withLock {
            [
                "label": label,
                "target": target,
                "t_start": tStart,
                "t_completed": tCompleted,
                "t_frame": seekFrameAt,
                "frame_pts": seekFramePTS,
                "t_playing": seekPlayingAt,
            ]
        }
    }

    func finish() -> [[String: Any]] {
        timer?.cancel()
        timer = nil
        lock.lock()
        if stallStart != 0 {
            stalls.append(["start": stallStart, "end": now(), "reason": stallReason, "open": true])
            stallStart = 0
        }
        let result = stalls
        lock.unlock()
        observations.removeAll()
        player.pause()
        player.replaceCurrentItem(with: nil)
        return result
    }

    func accessLog() -> [[String: Any]] {
        guard let events = item.accessLog()?.events else { return [] }
        return events.map { event in
            [
                "requests": event.numberOfMediaRequests,
                "bytes": event.numberOfBytesTransferred,
                "transfer_s": event.transferDuration,
                "observed_bitrate": event.observedBitrate,
                "indicated_bitrate": event.indicatedBitrate,
                "startup_s": event.startupTime,
                "stalls": event.numberOfStalls,
                "dropped": event.numberOfDroppedVideoFrames,
            ]
        }
    }
}

func runAVPlayer(_ spec: RunSpec, _ opened: OpenedSession) async -> [String: Any] {
    guard let url = opened.masterURL ?? opened.streamURL else { return ["error": "会话没有播放地址"] }
    let watch = PlayerWatch(url: url, id: spec.id)
    let tPlay = now()
    watch.player.play()
    var result: [String: Any] = ["t_play": tPlay]
    let deadline = tPlay + UInt64(spec.timeout * 1e9)
    while now() < deadline, !watch.hasFirstFrame, watch.failure == nil {
        try? await Task.sleep(nanoseconds: 2_000_000)
    }
    // isReadyForDisplay 与像素缓冲谁先到不一定，两个都等一下
    let settle = now() + 1_500_000_000
    while now() < settle {
        let snap = watch.snapshot()
        if (snap["first_pixel"] as? UInt64 ?? 0) != 0, (snap["layer_ready"] as? UInt64 ?? 0) != 0 { break }
        try? await Task.sleep(nanoseconds: 2_000_000)
    }
    result["start"] = watch.snapshot()
    var seeks: [[String: Any]] = []
    if watch.hasFirstFrame, watch.failure == nil {
        for step in spec.seeks {
            try? await Task.sleep(nanoseconds: UInt64(step.after * 1e9))
            seeks.append(await watch.seek(to: step.target, label: step.label, timeout: spec.timeout))
        }
        try? await Task.sleep(nanoseconds: UInt64(spec.tail * 1e9))
    }
    result["seeks"] = seeks
    result["access_log"] = watch.accessLog()
    if let error = watch.item.error { result["item_error"] = "\(error)" }
    result["stalls"] = watch.finish()
    result["t_end"] = now()
    return result
}

// MARK: - 分片模式（hls.js 式：整段到齐才算）

struct MediaPlaylist {
    let initURL: URL?
    let segments: [(url: URL, start: Double, duration: Double)]
    let startOffset: Double?

    func index(at position: Double) -> Int {
        var found = 0
        for (i, segment) in segments.enumerated() where segment.start <= position + 1e-6 {
            found = i
        }
        return found
    }
}

func parseMedia(_ text: String, base: URL) -> MediaPlaylist {
    var initURL: URL?
    var segments: [(URL, Double, Double)] = []
    var startOffset: Double?
    var pending: Double?
    var cursor = 0.0
    for raw in text.split(separator: "\n") {
        let line = raw.trimmingCharacters(in: .whitespaces)
        if line.hasPrefix("#EXT-X-MAP:"), let range = line.range(of: "URI=\"") {
            let rest = line[range.upperBound...]
            if let end = rest.firstIndex(of: "\"") { initURL = resolve(String(rest[..<end]), against: base) }
        } else if line.hasPrefix("#EXT-X-START:"), let range = line.range(of: "TIME-OFFSET=") {
            let rest = line[range.upperBound...].split(separator: ",").first.map(String.init) ?? ""
            startOffset = Double(rest)
        } else if line.hasPrefix("#EXTINF:") {
            pending = Double(line.dropFirst(8).split(separator: ",").first ?? "0")
        } else if !line.isEmpty, !line.hasPrefix("#"), let duration = pending {
            segments.append((resolve(line, against: base), cursor, duration))
            cursor += duration
            pending = nil
        }
    }
    return MediaPlaylist(initURL: initURL, segments: segments, startOffset: startOffset)
}

func timed(_ fetched: Fetched, _ name: String) -> [String: Any] {
    ["name": name, "status": fetched.status, "bytes": fetched.data.count,
     "t_sent": fetched.tSent, "t_first_byte": fetched.tFirstByte, "t_done": fetched.tDone]
}

func runSegments(_ spec: RunSpec, _ opened: OpenedSession) async -> [String: Any] {
    guard let master = opened.masterURL else { return ["error": "会话没有主播放列表"] }
    var requests: [[String: Any]] = []
    do {
        let tPlay = now()
        let masterFetched = try await TimedFetch.get(URLRequest(url: master))
        requests.append(timed(masterFetched, "master"))
        let masterText = String(data: masterFetched.data, encoding: .utf8) ?? ""
        guard let mediaLine = masterText.split(separator: "\n").map(String.init).first(where: { !$0.isEmpty && !$0.hasPrefix("#") })
        else { return ["error": "主播放列表里没有媒体列表", "requests": requests] }
        let mediaURL = resolve(mediaLine, against: master)
        let mediaFetched = try await TimedFetch.get(URLRequest(url: mediaURL))
        requests.append(timed(mediaFetched, "media"))
        let playlist = parseMedia(String(data: mediaFetched.data, encoding: .utf8) ?? "", base: mediaURL)
        guard !playlist.segments.isEmpty else { return ["error": "媒体列表为空", "requests": requests] }
        let startIndex = playlist.index(at: playlist.startOffset ?? 0)
        // hls.js 先要初始化段再要第一个分片；两者并行发出，关键路径是分片
        let initURL = playlist.initURL
        async let initResult: Fetched? = { () async throws -> Fetched? in
            guard let initURL else { return nil }
            return try await TimedFetch.get(URLRequest(url: initURL))
        }()
        async let segResult = TimedFetch.get(URLRequest(url: playlist.segments[startIndex].url))
        let (initFetched, segFetched) = try await (initResult, segResult)
        if let initFetched { requests.append(timed(initFetched, "init")) }
        requests.append(timed(segFetched, "seg\(startIndex)"))
        var result: [String: Any] = [
            "t_play": tPlay,
            "start_index": startIndex,
            "first_segment_done": max(segFetched.tDone, initFetched?.tDone ?? 0),
            "first_segment_bytes": segFetched.data.count,
            "first_segment_status": segFetched.status,
        ]
        var seeks: [[String: Any]] = []
        for step in spec.seeks {
            try? await Task.sleep(nanoseconds: UInt64(step.after * 1e9))
            let index = playlist.index(at: step.target)
            let fetched = try await TimedFetch.get(URLRequest(url: playlist.segments[index].url))
            requests.append(timed(fetched, "seg\(index)"))
            seeks.append([
                "label": step.label, "target": step.target, "index": index,
                "t_start": fetched.tSent, "t_frame": fetched.tDone, "status": fetched.status,
                "bytes": fetched.data.count,
            ])
        }
        result["seeks"] = seeks
        result["requests"] = requests
        result["t_end"] = now()
        return result
    } catch {
        return ["error": "取分片失败：\(error.localizedDescription)", "requests": requests]
    }
}

// MARK: - 主循环

func handle(_ line: String) async {
    guard let data = line.data(using: .utf8),
          let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let command = json["cmd"] as? String
    else {
        emit(["ev": "error", "error": "无法解析指令"])
        return
    }
    switch command {
    case "clock":
        emit(["ev": "clock", "t": now(), "echo": json["t"] ?? NSNull()])
    case "warm":
        // 与 App 的预连一致：先把到服务器的连接建好
        if let baseText = json["base"] as? String, let url = URL(string: baseText)?.appendingPathComponent("api/v1/health") {
            let t = now()
            _ = try? await TimedFetch.get(URLRequest(url: url))
            emit(["ev": "warm", "ms": Double(now() - t) / 1e6])
        }
    case "run":
        guard let spec = RunSpec(json) else {
            emit(["ev": "error", "error": "run 指令缺字段"])
            return
        }
        let opened: OpenedSession
        if let url = spec.directURL {
            opened = OpenedSession(
                sessionID: nil, masterURL: url, streamURL: url,
                event: ["id": spec.id, "ev": "session", "t_sent": now(), "t_resp": now(), "direct": true]
            )
        } else {
            opened = await openSession(spec)
        }
        emit(opened.event)
        var done: [String: Any] = ["id": spec.id, "ev": "done", "mode": spec.mode]
        if opened.event["error"] == nil {
            done["result"] = spec.mode == "segments"
                ? await runSegments(spec, opened)
                : await runAVPlayer(spec, opened)
        } else {
            done["result"] = ["error": opened.event["error"] ?? "?"]
        }
        emit(done)
    case "quit":
        exit(0)
    default:
        emit(["ev": "error", "error": "未知指令 \(command)"])
    }
}

emit(["ev": "hello", "t": now()])
Task.detached {
    while let line = readLine(strippingNewline: true) {
        await handle(line)
    }
    exit(0)
}
dispatchMain()
