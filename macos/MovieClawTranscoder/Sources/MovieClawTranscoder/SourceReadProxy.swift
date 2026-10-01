import Foundation
import Network

/// 一次向 NAS 取源（带 Range）的结果。
struct SourceFetchResult: Sendable {
    let data: Data
    /// 片源总长（Content-Range 的 `/N`）
    let size: Int64
    let contentType: String?
}

/// NAS 对取源请求回了非 206：原样转给 ffmpeg（404 会话已结束、403 令牌失效……），
/// ffmpeg 报出的错误与不经代理时一样，NAS 那边按 stderr 归因的逻辑不受影响。
struct SourceUpstreamStatus: Error, Sendable {
    let statusCode: Int
    let body: Data
    let contentType: String?
}

/// 取源块缓存：整个 Worker 共用，按片源路径（不含令牌）与块号记（docs/design/transcode-latency.md §6）。
///
/// 为什么跨任务共用：seek 时 NAS 停掉旧任务、开一轮新的 ffmpeg，新的一轮还要把文件头
/// （探测）和文件尾（MP4 的 moov、TS 找最后一个时间戳）再读一遍——这些块上一轮刚读过。
/// 按会话的取源路径记（`/sessions/{id}/source`、`/clips/{i}`），同一次播放里的每一轮都能命中；
/// 换一次播放会话就是另一条路径，不会串。
///
/// 同一块正在取时，后来的请求等同一个结果，不重复取。两类块各有份额、各自按最久未用淘汰，
/// 互不挤占：起转那几下跳读取的块（文件头、文件尾、索引——seek 重启的新一轮要的正是它们），
/// 与「过路」块（一个请求顺序读出十几 MB 之后取的，转码主读，只用一次）。只有一个总量时，
/// 前者攒满了会让后者刚取回就被挤掉，主读一秒断一次（2026-10-01 实测）。
actor SourceBlockCache {
    static let shared = SourceBlockCache(probeBytes: 128 * 1024 * 1024, streamBytes: 64 * 1024 * 1024)
    static let blockSize = 256 * 1024

    struct Key: Hashable, Sendable {
        let resource: String
        let index: Int
    }

    struct Info: Sendable {
        let size: Int64
        let contentType: String?
    }

    private struct Entry {
        let data: Data
        var lastUse: UInt64
        /// 过路块（转码主读顺序取的）
        let transient: Bool
    }

    /// 一次在途的取数：覆盖从 `first` 起的 `count` 块
    private struct Pending {
        let task: Task<SourceFetchResult, Error>
        let first: Int
        let count: Int
    }

    private let probeCapacity: Int
    private let streamCapacity: Int
    private var probeCount = 0
    private var streamCount = 0
    private var blocks: [Key: Entry] = [:]
    private var infos: [String: Info] = [:]
    private var inflight: [Key: Pending] = [:]
    private var tick: UInt64 = 0

    init(probeBytes: Int, streamBytes: Int) {
        probeCapacity = max(4, probeBytes / Self.blockSize)
        streamCapacity = max(4, streamBytes / Self.blockSize)
    }

    func info(_ resource: String) -> Info? {
        infos[resource]
    }

    /// 缓存里的一块（顺带记一次使用），不去取；没有返回 nil。文件最后一块可能不满。
    func cached(_ resource: String, _ index: Int) -> Data? {
        let key = Key(resource: resource, index: index)
        guard var entry = blocks[key] else { return nil }
        tick += 1
        entry.lastUse = tick
        blocks[key] = entry
        return entry.data
    }

    /// `index` 起往后（不含 `limit`）第一块既没缓存、也没在取的块号；都有了返回 nil。
    func firstMissing(_ resource: String, from index: Int, limit: Int) -> Int? {
        var cursor = index
        if let info = infos[resource] {
            let total = Int((info.size + Int64(Self.blockSize) - 1) / Int64(Self.blockSize))
            guard cursor < min(limit, total) else { return nil }
        }
        while cursor < limit {
            let key = Key(resource: resource, index: cursor)
            if blocks[key] == nil, inflight[key] == nil { return cursor }
            cursor += 1
        }
        return nil
    }

    /// 一次取数（覆盖从 `first` 起的若干块）。拿着它的人随时能从结果里切出要的块，不依赖缓存还留着：
    /// 容量紧时刚取回的块可能马上被挤掉，同一请求里接着要的块不该因此重取（曾一份数据取了六遍）。
    struct FetchRun: Sendable {
        let first: Int
        /// 要了几块（文件尾可能不满）
        let count: Int
        let task: Task<SourceFetchResult, Error>

        func covers(_ index: Int) -> Bool {
            index >= first && index < first + count
        }

        /// 结果里第 `index` 块；取失败或这一段没覆盖到返回 nil。
        func block(_ index: Int) async -> Data? {
            guard index >= first, let result = try? await task.value else { return nil }
            let offset = result.data.startIndex + (index - first) * SourceBlockCache.blockSize
            guard offset < result.data.endIndex else { return nil }
            return Data(result.data[offset..<min(offset + SourceBlockCache.blockSize, result.data.endIndex)])
        }
    }

    /// 覆盖第 `index` 块的那次取数：正在取就是那一次；没缓存也没在取就从它起取至多 `count` 块
    /// （碰到已缓存或在取的块、文件尾截断）；已经缓存了返回 nil（用 ``cached(_:_:)``）。
    func run(
        _ resource: String,
        index: Int,
        count: Int,
        transient: Bool = false,
        fetch: @escaping @Sendable (_ start: Int64, _ end: Int64) async throws -> SourceFetchResult
    ) -> FetchRun? {
        let key = Key(resource: resource, index: index)
        if blocks[key] != nil { return nil }
        let pending = inflight[key] ?? startFetch(resource, index: index, count: count, transient: transient, fetch: fetch)
        return FetchRun(first: pending.first, count: pending.count, task: pending.task)
    }

    /// 第 `index` 块的数据：缓存里有直接给；否则等覆盖它的那次取数（见 ``run``）。
    func block(
        _ resource: String,
        index: Int,
        count: Int,
        transient: Bool = false,
        fetch: @escaping @Sendable (_ start: Int64, _ end: Int64) async throws -> SourceFetchResult
    ) async throws -> Data {
        if let data = cached(resource, index) { return data }
        guard let run = run(resource, index: index, count: count, transient: transient, fetch: fetch) else {
            return try await block(resource, index: index, count: count, transient: transient, fetch: fetch)
        }
        _ = try await run.task.value
        guard let data = await run.block(index) else { throw URLError(.badServerResponse) }
        return data
    }

    private func startFetch(
        _ resource: String,
        index: Int,
        count: Int,
        transient: Bool,
        fetch: @escaping @Sendable (_ start: Int64, _ end: Int64) async throws -> SourceFetchResult
    ) -> Pending {
        var run = 1
        while run < count, firstMissing(resource, from: index + run, limit: index + run + 1) == index + run {
            run += 1
        }
        let blockSize = Int64(Self.blockSize)
        let start = Int64(index) * blockSize
        var end = start + Int64(run) * blockSize - 1
        if let size = infos[resource]?.size {
            end = min(end, size - 1)
        }
        let keys = (0..<run).map { Key(resource: resource, index: index + $0) }
        // 这个 Task 继承本 actor 的隔离：网络等待期间不占着 actor；取回后先落进缓存、清掉在取
        // 标记，再算完成——等同一块的请求醒来时缓存与在取表都已一致
        let task = Task<SourceFetchResult, Error> { [start, end] in
            defer { self.clearInflight(keys) }
            let result = try await fetch(start, end)
            self.store(result, resource: resource, first: index, transient: transient)
            return result
        }
        let pending = Pending(task: task, first: index, count: run)
        for item in keys { inflight[item] = pending }
        return pending
    }

    private func clearInflight(_ keys: [Key]) {
        for item in keys { inflight[item] = nil }
    }

    private func store(_ result: SourceFetchResult, resource: String, first: Int, transient: Bool) {
        infos[resource] = Info(size: result.size, contentType: result.contentType)
        let data = result.data
        var offset = data.startIndex
        var index = first
        while offset < data.endIndex {
            let end = min(offset + Self.blockSize, data.endIndex)
            let key = Key(resource: resource, index: index)
            if let old = blocks.removeValue(forKey: key) {
                count(old.transient, -1)
            }
            tick += 1
            blocks[key] = Entry(data: Data(data[offset..<end]), lastUse: tick, transient: transient)
            count(transient, 1)
            offset = end
            index += 1
        }
        evict(transient: transient)
    }

    private func count(_ transient: Bool, _ delta: Int) {
        if transient { streamCount += delta } else { probeCount += delta }
    }

    /// 这一类超了份额就按最久未用淘汰这一类的块。
    private func evict(transient: Bool) {
        let capacity = transient ? streamCapacity : probeCapacity
        while (transient ? streamCount : probeCount) > capacity {
            var oldest: (key: Key, lastUse: UInt64)?
            for (key, entry) in blocks where entry.transient == transient {
                if oldest == nil || entry.lastUse < oldest!.lastUse {
                    oldest = (key, entry.lastUse)
                }
            }
            guard let oldest else { return }
            blocks.removeValue(forKey: oldest.key)
            count(transient, -1)
        }
    }
}

/// Worker 侧取源代理（docs/design/transcode-latency.md §6）。
///
/// 为什么要它：ffmpeg 经 HTTP 读 NAS 上的片源，每次跳读都新开一条连接、发一个不封口的 Range
/// （`bytes=X-`）。起转时的探测、估时长、`-ss` 二分查找加起来要跳读二三十次（原盘、TS 最多），
/// 每次只要几 KB，却要付一次 TCP 握手和一次请求往返；NAS 那头还会一口气推出十几 MB 才发现
/// 对面已经走了（片源若在另一台存储的 NFS 上，这十几 MB 也要先从那边读过来）。Wi-Fi 下往返
/// 3～95 毫秒，二三十次串行跳读就是一两秒。
///
/// 代理在本机回环口接 ffmpeg 的请求，替它向 NAS 取：
/// - 连接复用：所有任务共用一条 URLSession，到 NAS 的连接 keep-alive，省掉每次的握手；
/// - 按块取、按需放大：先取一块（256 KiB），ffmpeg 一直顺序往下读才放大到 1、4、8 MiB，并提前
///   取下一段；读几 KB 就跳走的探测，NAS 只多送一两块；
/// - 块缓存（``SourceBlockCache``）：文件头尾那串小读、seek 重启后新一轮的再探测大多直接命中。
///
/// ffmpeg 看到的是一个普通的、支持 Range 的 HTTP 源。中途出错就断开这条连接，ffmpeg 自己的
/// 断线续读（`-reconnect`）照常接手；NAS 回的错误码原样转给它。
final class SourceReadProxy: @unchecked Sendable {
    /// 第一次取几块、之后每次放大几倍、最多几块（8 MiB）。第一次只取 1 块：取数整段到齐才交出，
    /// 首次取 1 MiB 时从头起播稳定慢 80 毫秒；原盘二分查找每步省下的那次等待，并行多取后几块
    /// 也拿不稳（冷读两路并发互相拖慢，p90 反而变差），实验台对照见设计文档 §6.4。
    static let firstRunBlocks = 1
    static let runGrowth = 4
    static let maxRunBlocks = 32
    /// 送出这么多块之后才开始预取。回环口两头的缓冲能吞下约两块（实测 512 KiB），「送出」不等于
    /// ffmpeg 读了；二分查找每一步要读 0.5～1 MB（凑够一帧视频），读完就跳走，过早预取的
    /// 4 + 8 MB 全白取，还跟下一步抢带宽。送出 6 块时 ffmpeg 至少读走了 1 MB，是真在顺序读。
    static let prefetchAfterBlocks = 6
    /// 一个请求顺序读过这么多之后取的块算「过路」（转码主读），记进过路块的份额
    static let defaultTransientAfterBytes: Int64 = 16 * 1024 * 1024

    /// 所有任务共用：seek 重启的新一轮直接用上一轮留下的长连接。
    static let session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 120
        configuration.httpShouldSetCookies = false
        configuration.httpCookieStorage = nil
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.httpMaximumConnectionsPerHost = 6
        return URLSession(configuration: configuration)
    }()

    struct Stats: Sendable {
        var requests = 0
        var blocksServed = 0
        var blocksFetched = 0
    }

    enum ProxyError: Error, CustomStringConvertible {
        case listenerFailed(String)
        case stopped

        var description: String {
            switch self {
            case let .listenerFailed(message): return "取源代理无法监听本机端口：\(message)"
            case .stopped: return "取源代理已停止"
            }
        }
    }

    let jobID: String
    /// NAS 的 scheme://host:port（ffmpeg 原本要连的那个）
    let origin: URL
    let cache: SourceBlockCache
    let transientAfterBytes: Int64
    /// 逐请求记日志（实验开关 source-log）：起点、交给 ffmpeg 多少、等了网络多久
    let logRequests: Bool
    private let queue: DispatchQueue
    private let listener: NWListener
    private let lock = NSLock()
    private var startContinuation: CheckedContinuation<URL, Error>?
    private var started = false
    private var stopped = false
    private var connections: [ObjectIdentifier: SourceConnection] = [:]
    private var stats = Stats()

    init(
        jobID: String,
        origin: URL,
        cache: SourceBlockCache = .shared,
        transientAfterBytes: Int64 = SourceReadProxy.defaultTransientAfterBytes,
        logRequests: Bool = false
    ) throws {
        self.jobID = jobID
        self.origin = origin
        self.cache = cache
        self.transientAfterBytes = transientAfterBytes
        self.logRequests = logRequests
        queue = DispatchQueue(label: "com.movieclaw.transcoder.source.\(jobID)")
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = NWEndpoint.hostPort(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: 0)!)
        listener = try NWListener(using: parameters)
    }

    /// ffmpeg 参数里指向 NAS 取源接口的输入（`-i` 后面那个地址）；没有返回 nil。
    /// 单文件是 `.../sessions/{id}/source`，原盘是 `.../sessions/{id}/source.ffconcat`
    /// （清单里的剪辑是相对地址，跟着清单一起走代理）。
    static func remoteSourceURL(from arguments: [String]) -> URL? {
        for (index, argument) in arguments.enumerated() where argument == "-i" && index + 1 < arguments.count {
            guard let url = URL(string: arguments[index + 1]),
                  let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https",
                  url.path.contains("/transcode-worker/sessions/")
            else { continue }
            return url
        }
        return nil
    }

    /// 地址的 scheme://host:port 部分（代理向 NAS 取数用的根）。
    static func origin(of url: URL) -> URL? {
        var components = URLComponents()
        components.scheme = url.scheme
        components.host = url.host
        components.port = url.port
        return components.url
    }

    /// 把 `-i` 里指向 NAS 的地址换成本机代理（路径与查询原样保留）。
    func rewrite(arguments: [String], localBaseURL: URL) -> [String] {
        var rewritten = arguments
        for index in rewritten.indices where index > 0 && rewritten[index - 1] == "-i" {
            guard var components = URLComponents(string: rewritten[index]),
                  components.host == origin.host, components.port == origin.port,
                  components.scheme == origin.scheme
            else { continue }
            components.scheme = localBaseURL.scheme
            components.host = localBaseURL.host
            components.port = localBaseURL.port
            if let url = components.string {
                rewritten[index] = url
            }
        }
        return rewritten
    }

    /// 启动回环 HTTP 服务，返回给 ffmpeg 用的根地址（`http://127.0.0.1:端口`）。
    func start() async throws -> URL {
        try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            if stopped || started || startContinuation != nil {
                lock.unlock()
                continuation.resume(throwing: stopped ? ProxyError.stopped : ProxyError.listenerFailed("重复启动"))
                return
            }
            startContinuation = continuation
            lock.unlock()
            listener.stateUpdateHandler = { [weak self] state in
                self?.handleListenerState(state)
            }
            listener.newConnectionHandler = { [weak self] connection in
                self?.accept(connection)
            }
            listener.start(queue: queue)
        }
    }

    func stop() {
        lock.lock()
        guard !stopped else {
            lock.unlock()
            return
        }
        stopped = true
        let active = Array(connections.values)
        connections.removeAll()
        let summary = stats
        lock.unlock()
        listener.cancel()
        active.forEach { $0.stop() }
        if summary.requests > 0 {
            let fetchedMB = Double(summary.blocksFetched * SourceBlockCache.blockSize) / 1_048_576
            AppLogger.shared.info(
                "取源代理收尾：job=\(jobID) 请求 \(summary.requests) 次，交给 ffmpeg \(summary.blocksServed) 块，" +
                    String(format: "向 NAS 取了 %.1f MB", fetchedMB)
            )
        }
    }

    var currentStats: Stats {
        lock.lock()
        defer { lock.unlock() }
        return stats
    }

    fileprivate func record(_ update: (inout Stats) -> Void) {
        lock.lock()
        update(&stats)
        lock.unlock()
    }

    private func handleListenerState(_ state: NWListener.State) {
        switch state {
        case .ready:
            lock.lock()
            started = true
            let continuation = startContinuation
            startContinuation = nil
            lock.unlock()
            guard let port = listener.port else {
                continuation?.resume(throwing: ProxyError.listenerFailed("系统未分配监听端口"))
                return
            }
            continuation?.resume(returning: URL(string: "http://127.0.0.1:\(port.rawValue)")!)
        case let .failed(error):
            failStart(String(describing: error))
        case .cancelled:
            failStart(ProxyError.stopped.description)
        default:
            break
        }
    }

    private func failStart(_ message: String) {
        lock.lock()
        let continuation = startContinuation
        startContinuation = nil
        lock.unlock()
        continuation?.resume(throwing: ProxyError.listenerFailed(message))
    }

    private func accept(_ connection: NWConnection) {
        lock.lock()
        guard !stopped else {
            lock.unlock()
            connection.cancel()
            return
        }
        let handler = SourceConnection(connection: connection, proxy: self, queue: queue)
        connections[ObjectIdentifier(handler)] = handler
        lock.unlock()
        handler.start()
    }

    fileprivate func connectionDidFinish(_ handler: SourceConnection) {
        lock.lock()
        connections.removeValue(forKey: ObjectIdentifier(handler))
        lock.unlock()
    }

    /// 回环请求的目标（路径 + 查询）对应的 NAS 地址。
    fileprivate func upstreamURL(for target: String) -> URL? {
        guard target.hasPrefix("/") else { return nil }
        return URL(string: target, relativeTo: origin)?.absoluteURL
    }

    /// 向 NAS 取 `[start, end]` 这一段（闭区间）。
    static func fetch(url: URL, start: Int64, end: Int64) async throws -> SourceFetchResult {
        var request = URLRequest(url: url)
        request.setValue("bytes=\(start)-\(end)", forHTTPHeaderField: "Range")
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }
        let contentType = http.value(forHTTPHeaderField: "Content-Type")
        guard http.statusCode == 206 else {
            throw SourceUpstreamStatus(statusCode: http.statusCode, body: data, contentType: contentType)
        }
        guard let range = http.value(forHTTPHeaderField: "Content-Range"),
              let total = range.split(separator: "/").last.flatMap({ Int64($0) })
        else {
            throw URLError(.badServerResponse)
        }
        return SourceFetchResult(data: data, size: total, contentType: contentType)
    }

    /// 不带 Range 的请求（原盘清单）：整个取回原样转发。
    static func fetchWhole(url: URL) async throws -> (status: Int, body: Data, contentType: String?) {
        let (data, response) = try await session.data(for: URLRequest(url: url))
        guard let http = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }
        return (http.statusCode, data, http.value(forHTTPHeaderField: "Content-Type"))
    }
}

/// 一条 ffmpeg 来的回环连接：读一个请求、回一个响应，然后关掉（`Connection: close`，
/// ffmpeg 下一次跳读自己重连回环口，几乎不花时间）。
private final class SourceConnection: @unchecked Sendable {
    private struct Request {
        let method: String
        let target: String
        let rangeStart: Int64?
        let rangeEnd: Int64?
    }

    private static let headerTerminator = Data([13, 10, 13, 10])
    private static let maxHeaderBytes = 16 * 1024

    private let connection: NWConnection
    private weak var proxy: SourceReadProxy?
    private let queue: DispatchQueue
    private var header = Data()
    private var task: Task<Void, Never>?
    private let lock = NSLock()
    private var finished = false

    init(connection: NWConnection, proxy: SourceReadProxy, queue: DispatchQueue) {
        self.connection = connection
        self.proxy = proxy
        self.queue = queue
    }

    func start() {
        connection.stateUpdateHandler = { [weak self] state in
            switch state {
            case .failed, .cancelled:
                self?.stop()
            default:
                break
            }
        }
        connection.start(queue: queue)
        receiveHeader()
    }

    func stop() {
        lock.lock()
        guard !finished else {
            lock.unlock()
            return
        }
        finished = true
        let pending = task
        lock.unlock()
        pending?.cancel()
        connection.cancel()
        proxy?.connectionDidFinish(self)
    }

    private var isFinished: Bool {
        lock.lock()
        defer { lock.unlock() }
        return finished
    }

    private func receiveHeader() {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 16 * 1024) { [weak self] data, _, isComplete, error in
            guard let self, !self.isFinished else { return }
            if let data { self.header.append(data) }
            if let end = self.header.range(of: Self.headerTerminator) {
                let request = self.parse(Data(self.header[..<end.lowerBound]))
                self.header.removeAll()
                self.watchForClose()
                let work = Task { [weak self] in
                    guard let self else { return }
                    if let request {
                        await self.serve(request)
                    } else {
                        try? await self.respond(status: 400, reason: "Bad Request")
                    }
                    self.stop()
                }
                self.lock.lock()
                self.task = work
                self.lock.unlock()
                return
            }
            if self.header.count > Self.maxHeaderBytes || isComplete || error != nil {
                self.stop()
                return
            }
            self.receiveHeader()
        }
    }

    /// ffmpeg 跳读时直接关掉这条连接：一读到 EOF 就停下手里的活（不再替它向 NAS 取）。
    private func watchForClose() {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { [weak self] _, _, isComplete, error in
            guard let self, !self.isFinished else { return }
            if isComplete || error != nil {
                self.stop()
            } else {
                self.watchForClose()
            }
        }
    }

    private func parse(_ data: Data) -> Request? {
        guard let text = String(data: data, encoding: .utf8) else { return nil }
        let lines = text.components(separatedBy: "\r\n")
        let parts = lines.first?.split(separator: " ", maxSplits: 2) ?? []
        guard parts.count == 3 else { return nil }
        var start: Int64?
        var end: Int64?
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":"),
                  line[..<colon].trimmingCharacters(in: .whitespaces).lowercased() == "range"
            else { continue }
            // 只认单段 `bytes=X-` / `bytes=X-Y`（ffmpeg 只发这两种）
            let value = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
            guard value.hasPrefix("bytes=") else { return nil }
            let bounds = value.dropFirst(6).split(separator: "-", maxSplits: 1, omittingEmptySubsequences: false)
            guard bounds.count == 2, let first = Int64(bounds[0]) else { return nil }
            start = first
            end = bounds[1].isEmpty ? nil : Int64(bounds[1])
            if let last = end, last < first { return nil }
        }
        return Request(method: String(parts[0]).uppercased(), target: String(parts[1]), rangeStart: start, rangeEnd: end)
    }

    private func serve(_ request: Request) async {
        guard let proxy, let url = proxy.upstreamURL(for: request.target) else {
            try? await respond(status: 400, reason: "Bad Request")
            return
        }
        guard request.method == "GET" else {
            try? await respond(status: 405, reason: "Method Not Allowed")
            return
        }
        proxy.record { $0.requests += 1 }
        let began = Date()
        var outcome = "完成"
        defer {
            if proxy.logRequests {
                let offset = request.rangeStart.map { String(format: "%.1f MB", Double($0) / 1_048_576) } ?? "整个"
                AppLogger.shared.info(
                    "取源请求：job=\(proxy.jobID) 起点=\(offset) 送出=\(trace.sentBytes / 1024) KB " +
                        "用时=\(Int(Date().timeIntervalSince(began) * 1000)) ms 等网络=\(trace.waitedMS) ms（\(trace.waits) 次）" +
                        " 命中=\(trace.hits) 块 预取=\(trace.prefetches) 次 结束=\(outcome)"
                )
            }
        }
        do {
            if let start = request.rangeStart {
                try await serveRange(proxy: proxy, url: url, start: start, end: request.rangeEnd)
            } else {
                let whole = try await SourceReadProxy.fetchWhole(url: url)
                try await respond(
                    status: whole.status,
                    reason: HTTPURLResponse.localizedString(forStatusCode: whole.status),
                    body: whole.body,
                    contentType: whole.contentType
                )
            }
        } catch let upstream as SourceUpstreamStatus {
            try? await respond(
                status: upstream.statusCode,
                reason: HTTPURLResponse.localizedString(forStatusCode: upstream.statusCode),
                body: upstream.body,
                contentType: upstream.contentType
            )
        } catch is ClientGone {
            // ffmpeg 跳读走了（关了连接），正常情况
            outcome = "对方断开"
        } catch is CancellationError {
            outcome = "对方断开"
        } catch {
            outcome = "出错"
            // 响应头还没发就回 502；已经在送了就直接断开，ffmpeg 按断线续读从断点重新要
            AppLogger.shared.warning("取源代理向 NAS 取数失败：\(error.localizedDescription)")
            try? await respond(status: 502, reason: "Bad Gateway")
        }
    }

    /// 回写 ffmpeg 失败：它已经关了这条连接。
    private struct ClientGone: Error {}

    /// 一个请求的经过（实验开关 source-log 时写日志）
    private struct Trace {
        var sentBytes: Int64 = 0
        var waits = 0
        var waitedMS = 0
        var hits = 0
        var prefetches = 0
    }

    private var trace = Trace()

    private var headerSent = false

    private func serveRange(proxy: SourceReadProxy, url: URL, start: Int64, end: Int64?) async throws {
        let cache = proxy.cache
        let resource = url.path
        let blockSize = Int64(SourceBlockCache.blockSize)
        let fetch: @Sendable (Int64, Int64) async throws -> SourceFetchResult = { from, to in
            let result = try await SourceReadProxy.fetch(url: url, start: from, end: to)
            proxy.record { $0.blocksFetched += (result.data.count + SourceBlockCache.blockSize - 1) / SourceBlockCache.blockSize }
            return result
        }
        var run = SourceReadProxy.firstRunBlocks
        func grow() {
            run = min(run * SourceReadProxy.runGrowth, SourceReadProxy.maxRunBlocks)
        }
        /// 一个请求顺序读过 16 MiB 之后的块算过路块（转码主读）
        func isTransient(_ index: Int) -> Bool {
            Int64(index) * blockSize - start >= proxy.transientAfterBytes
        }
        /// 这个请求手里攥着的取数（当前段与预取段）：按块读时先从它们里切，再看缓存，最后才去取
        var held: [SourceBlockCache.FetchRun] = []
        /// 取第 `index` 块：手里有、缓存有直接给；要等网络的才把下一次的取量放大
        func load(_ index: Int) async throws -> Data {
            held.removeAll { $0.first + $0.count <= index }
            for piece in held where piece.covers(index) {
                if let data = await piece.block(index) {
                    trace.hits += 1
                    return data
                }
            }
            if let data = await cache.cached(resource, index) {
                trace.hits += 1
                return data
            }
            let transient = isTransient(index)
            guard let piece = await cache.run(resource, index: index, count: run, transient: transient, fetch: fetch) else {
                return try await load(index)
            }
            held.append(piece)
            grow()
            let waitStarted = Date()
            _ = try await piece.task.value
            trace.waits += 1
            trace.waitedMS += Int(Date().timeIntervalSince(waitStarted) * 1000)
            guard let data = await piece.block(index) else { throw URLError(.badServerResponse) }
            return data
        }

        /// `from` 起第一块：手里没有、缓存里没有、也没人在取（预取从这里开始）
        func nextMissing(from: Int, limit: Int) async -> Int? {
            var cursor = from
            while cursor < limit {
                if let covering = held.first(where: { $0.covers(cursor) }) {
                    cursor = covering.first + covering.count
                    continue
                }
                guard let missing = await cache.firstMissing(resource, from: cursor, limit: limit) else { return nil }
                if held.contains(where: { $0.covers(missing) }) {
                    cursor = missing
                    continue
                }
                return missing
            }
            return nil
        }

        let firstIndex = Int(start / blockSize)
        var data = try await load(firstIndex)
        guard let info = await cache.info(resource) else { throw URLError(.badServerResponse) }
        guard start < info.size else {
            try await respond(status: 416, reason: "Range Not Satisfiable", extraHeaders: ["Content-Range: bytes */\(info.size)"])
            return
        }
        let last = min(end ?? info.size - 1, info.size - 1)
        var head = "HTTP/1.1 206 Partial Content\r\n"
        head += "Content-Type: \(info.contentType ?? "application/octet-stream")\r\n"
        head += "Accept-Ranges: bytes\r\n"
        head += "Content-Range: bytes \(start)-\(last)/\(info.size)\r\n"
        head += "Content-Length: \(last - start + 1)\r\n"
        head += "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
        try await send(Data(head.utf8))
        headerSent = true

        let lastBlock = Int(last / blockSize)
        var position = start
        var index = firstIndex
        while true {
            try Task.checkCancellation()
            // 预取：ffmpeg 确实在顺序往下读（见 prefetchAfterBlocks），下一个缺的块在这一段之内就提前去取
            if index >= firstIndex + SourceReadProxy.prefetchAfterBlocks,
               let next = await nextMissing(from: index + 1, limit: min(lastBlock + 1, index + 1 + run))
            {
                if let ahead = await cache.run(resource, index: next, count: run, transient: isTransient(next), fetch: fetch) {
                    held.append(ahead)
                    trace.prefetches += 1
                }
                grow()
            }
            let blockStart = Int64(index) * blockSize
            let from = data.startIndex + Int(position - blockStart)
            let to = min(data.endIndex, data.startIndex + Int(last - blockStart) + 1)
            guard from < to else { throw URLError(.badServerResponse) }
            try await send(Data(data[from..<to]))
            trace.sentBytes += Int64(to - from)
            proxy.record { $0.blocksServed += 1 }
            position += Int64(to - from)
            guard position <= last else { return }
            index += 1
            data = try await load(index)
        }
    }

    private func respond(
        status: Int,
        reason: String,
        body: Data = Data(),
        contentType: String? = nil,
        extraHeaders: [String] = []
    ) async throws {
        guard !headerSent else { return }
        headerSent = true
        var head = "HTTP/1.1 \(status) \(reason)\r\n"
        if let contentType { head += "Content-Type: \(contentType)\r\n" }
        for line in extraHeaders { head += line + "\r\n" }
        head += "Content-Length: \(body.count)\r\nConnection: close\r\n\r\n"
        var payload = Data(head.utf8)
        payload.append(body)
        try await send(payload)
    }

    /// 写回给 ffmpeg，等系统收下再返回（回环口的背压：ffmpeg 不读，这里就停在这儿）。
    private func send(_ data: Data) async throws {
        guard !isFinished else { throw ClientGone() }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            connection.send(content: data, completion: .contentProcessed { error in
                if error != nil {
                    continuation.resume(throwing: ClientGone())
                } else {
                    continuation.resume()
                }
            })
        }
    }
}
