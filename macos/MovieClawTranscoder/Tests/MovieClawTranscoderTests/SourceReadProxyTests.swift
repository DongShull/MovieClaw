import Network
import XCTest

@testable import MovieClawTranscoder

/// 取源代理：字节必须与直读一模一样（跨块、到文件尾、封口与不封口的 Range），
/// 读过的块不再向 NAS 要，NAS 的错误码原样转给 ffmpeg。
final class SourceReadProxyTests: XCTestCase {
    private let blockSize = SourceBlockCache.blockSize

    func testRangeReadsMatchTheSourceAcrossBlocksAndAtTheEnd() async throws {
        let payload = Self.payload(count: blockSize * 5 + 1234)
        let upstream = try await FakeSource(payload: payload)
        defer { upstream.stop() }
        let (proxy, local) = try await startProxy(upstream: upstream)
        defer { proxy.stop() }

        let cases: [(Int, Int?)] = [
            (0, nil),  // 从头读到尾（跨全部块）
            (blockSize - 10, blockSize + 10),  // 跨块边界的封口读
            (blockSize * 5 + 1000, nil),  // 最后一块（不满）
            (123_456, 123_456),  // 单字节
        ]
        for (start, end) in cases {
            let data = try await read(local, path: upstream.path, start: start, end: end)
            let last = end ?? payload.count - 1
            XCTAssertEqual(data, payload.subdata(in: start..<(last + 1)), "Range \(start)-\(end.map(String.init) ?? "")")
        }
    }

    func testBlocksAlreadyFetchedAreNotRequestedAgain() async throws {
        let payload = Self.payload(count: blockSize * 3)
        let upstream = try await FakeSource(payload: payload)
        defer { upstream.stop() }
        let cache = SourceBlockCache(probeBytes: 16 * 1024 * 1024, streamBytes: 16 * 1024 * 1024)
        let (proxy, local) = try await startProxy(upstream: upstream, cache: cache)
        defer { proxy.stop() }

        // 文件尾那串倒着读的小请求：第一次取回最后一块，后面几次都在这一块里
        let tail = payload.count - 1000
        _ = try await read(local, path: upstream.path, start: tail, end: payload.count - 1)
        let afterFirst = upstream.requestCount
        for offset in [tail + 10, tail + 500, payload.count - 200] {
            let data = try await read(local, path: upstream.path, start: offset, end: payload.count - 1)
            XCTAssertEqual(data, payload.subdata(in: offset..<payload.count))
        }
        XCTAssertEqual(upstream.requestCount, afterFirst, "尾部的块已经在缓存里")

        // 另一个任务（seek 重启后的新一轮）读同一路径，同样命中
        let second = try SourceReadProxy(jobID: "second", origin: upstream.origin, cache: cache)
        let secondLocal = try await second.start()
        defer { second.stop() }
        _ = try await read(secondLocal, path: upstream.path, start: tail, end: payload.count - 1)
        XCTAssertEqual(upstream.requestCount, afterFirst)
    }

    func testProbeAndStreamingBlocksHaveSeparateQuotas() async throws {
        // 起转跳读的块（文件头、文件尾）与主读的过路块各 4 块份额：过路块再多也挤不掉头尾，
        // 头尾攒满了也挤不掉刚取回的过路块（只有一个总量时主读一秒断一次）
        let cache = SourceBlockCache(probeBytes: blockSize * 4, streamBytes: blockSize * 4)
        let fetch = Self.zeroFetch(size: Int64(blockSize * 100))
        _ = try await cache.block("r", index: 0, count: 1, fetch: fetch)
        _ = try await cache.block("r", index: 99, count: 1, fetch: fetch)
        for index in stride(from: 10, to: 20, by: 2) {
            _ = try await cache.block("r", index: index, count: 2, transient: true, fetch: fetch)
        }
        let head = await cache.cached("r", 0)
        let tail = await cache.cached("r", 99)
        let oldestStreaming = await cache.cached("r", 10)
        let newestStreaming = await cache.cached("r", 19)
        XCTAssertNotNil(head, "文件头留着给 seek 重启")
        XCTAssertNotNil(tail, "文件尾留着给 seek 重启")
        XCTAssertNil(oldestStreaming, "过路块超了份额先淘汰自己最旧的")
        XCTAssertNotNil(newestStreaming)
    }

    func testFetchedBlockIsReturnedEvenIfEvictedRightAway() async throws {
        // 一次取 8 块、份额只有 4 块：第一块落进缓存就被挤掉，请求它的人照样拿到数据
        let cache = SourceBlockCache(probeBytes: blockSize * 4, streamBytes: blockSize * 4)
        let fetch = Self.zeroFetch(size: Int64(blockSize * 100))
        let data = try await cache.block("r", index: 0, count: 8, transient: true, fetch: fetch)
        XCTAssertEqual(data.count, blockSize)
        let evicted = await cache.cached("r", 0)
        XCTAssertNil(evicted)
    }

    private static func zeroFetch(size: Int64) -> @Sendable (Int64, Int64) async throws -> SourceFetchResult {
        { start, end in
            SourceFetchResult(data: Data(count: Int(end - start + 1)), size: size, contentType: nil)
        }
    }

    func testLongSequentialReadFetchesEachBlockOnce() async throws {
        // 转码主读：一个请求一路顺序读到底，缓存（各 1 MiB）远小于文件（12 MiB）。
        // 每块只向 NAS 要一次，字节一个不差（曾经刚取回的块被挤掉、主读一秒断一次）
        let payload = Data((0..<(blockSize * 48)).map { UInt8(truncatingIfNeeded: $0 &* 31 &+ $0 >> 9) })
        let upstream = try await FakeSource(payload: payload)
        defer { upstream.stop() }
        let cache = SourceBlockCache(probeBytes: blockSize * 4, streamBytes: blockSize * 4)
        let proxy = try SourceReadProxy(
            jobID: "stream", origin: upstream.origin, cache: cache, transientAfterBytes: Int64(blockSize * 2)
        )
        let local = try await proxy.start()
        defer { proxy.stop() }

        let data = try await read(local, path: upstream.path, start: 0, end: nil)
        XCTAssertEqual(data, payload)
        XCTAssertLessThanOrEqual(proxy.currentStats.blocksFetched, 48)
    }

    func testUpstreamErrorsAreForwardedAsIs() async throws {
        let upstream = try await FakeSource(payload: Self.payload(count: 1000))
        defer { upstream.stop() }
        let (proxy, local) = try await startProxy(upstream: upstream)
        defer { proxy.stop() }

        var request = URLRequest(url: URL(string: "\(local.absoluteString)/missing?token=x")!)
        request.setValue("bytes=0-", forHTTPHeaderField: "Range")
        let (_, response) = try await URLSession(configuration: .ephemeral).data(for: request)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 404)
    }

    func testRequestsWithoutRangeArePassedThrough() async throws {
        // 原盘清单：普通 GET，整个转发
        let upstream = try await FakeSource(payload: Data("ffconcat version 1.0\nfile 'clips/0?token=x'\n".utf8))
        defer { upstream.stop() }
        let (proxy, local) = try await startProxy(upstream: upstream)
        defer { proxy.stop() }

        let url = URL(string: "\(local.absoluteString)\(upstream.path)")!
        let (data, response) = try await URLSession(configuration: .ephemeral).data(from: url)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        XCTAssertEqual(String(decoding: data, as: UTF8.self), "ffconcat version 1.0\nfile 'clips/0?token=x'\n")
    }

    func testRewritesOnlyInputsOnTheNAS() throws {
        let arguments = [
            "-ss", "120", "-i", "http://192.168.1.10:3000/api/v1/transcode-worker/sessions/S1/source?token=a%2Bb",
            "-i", "http://example.com/other.srt",
            "-f", "mp4", "http://192.168.1.10:3000/api/v1/transcode-worker/sessions/S1/artifacts/stream.mp4",
        ]
        let source = try XCTUnwrap(SourceReadProxy.remoteSourceURL(from: arguments))
        let origin = try XCTUnwrap(SourceReadProxy.origin(of: source))
        XCTAssertEqual(origin.absoluteString, "http://192.168.1.10:3000")
        let proxy = try SourceReadProxy(jobID: "rewrite", origin: origin)
        let rewritten = proxy.rewrite(arguments: arguments, localBaseURL: URL(string: "http://127.0.0.1:5555")!)
        XCTAssertEqual(rewritten[3], "http://127.0.0.1:5555/api/v1/transcode-worker/sessions/S1/source?token=a%2Bb")
        XCTAssertEqual(rewritten[5], "http://example.com/other.srt", "别的主机不动")
        XCTAssertEqual(rewritten[8], arguments[8], "输出地址不是 -i，不动")
    }

    // MARK: - 工具

    private func startProxy(
        upstream: FakeSource,
        cache: SourceBlockCache = SourceBlockCache(probeBytes: 16 * 1024 * 1024, streamBytes: 16 * 1024 * 1024)
    ) async throws -> (SourceReadProxy, URL) {
        let proxy = try SourceReadProxy(jobID: "test", origin: upstream.origin, cache: cache)
        return (proxy, try await proxy.start())
    }

    private func read(_ base: URL, path: String, start: Int, end: Int?) async throws -> Data {
        var request = URLRequest(url: URL(string: "\(base.absoluteString)\(path)?token=t")!)
        request.setValue("bytes=\(start)-\(end.map(String.init) ?? "")", forHTTPHeaderField: "Range")
        let (data, response) = try await URLSession(configuration: .ephemeral).data(for: request)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 206)
        return data
    }

    private static func payload(count: Int) -> Data {
        var generator = SystemRandomNumberGenerator()
        return Data((0..<count).map { _ in UInt8.random(in: 0...255, using: &generator) })
    }
}

/// 测试用的 NAS 取源接口：支持 Range 与 keep-alive，只认一个路径，别的回 404；记请求次数。
private final class FakeSource: @unchecked Sendable {
    let path = "/api/v1/transcode-worker/sessions/S1/source"
    let payload: Data
    private let listener: NWListener
    private let queue = DispatchQueue(label: "fake-source")
    private let lock = NSLock()
    private var requests = 0
    private(set) var origin: URL!

    var requestCount: Int {
        lock.lock()
        defer { lock.unlock() }
        return requests
    }

    init(payload: Data) async throws {
        self.payload = payload
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = NWEndpoint.hostPort(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: 0)!)
        listener = try NWListener(using: parameters)
        listener.newConnectionHandler = { [weak self] connection in
            guard let self else { return }
            connection.start(queue: self.queue)
            self.receive(connection, buffer: Data())
        }
        let port: UInt16 = try await withCheckedThrowingContinuation { continuation in
            // 状态回调都在同一个串行队列上，只会有一次 ready / failed 真正生效
            listener.stateUpdateHandler = { [listener] state in
                switch state {
                case .ready:
                    listener.stateUpdateHandler = nil
                    continuation.resume(returning: listener.port!.rawValue)
                case let .failed(error):
                    listener.stateUpdateHandler = nil
                    continuation.resume(throwing: error)
                default:
                    break
                }
            }
            listener.start(queue: queue)
        }
        origin = URL(string: "http://127.0.0.1:\(port)")!
    }

    func stop() {
        listener.cancel()
    }

    private func receive(_ connection: NWConnection, buffer: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, isComplete, error in
            guard let self else { return }
            var buffer = buffer
            if let data { buffer.append(data) }
            while let end = buffer.range(of: Data([13, 10, 13, 10])) {
                let head = String(decoding: buffer[buffer.startIndex..<end.lowerBound], as: UTF8.self)
                buffer.removeSubrange(buffer.startIndex..<end.upperBound)
                self.respond(to: head, on: connection)
            }
            if isComplete || error != nil {
                connection.cancel()
                return
            }
            self.receive(connection, buffer: buffer)
        }
    }

    private func respond(to head: String, on connection: NWConnection) {
        lock.lock()
        requests += 1
        lock.unlock()
        let lines = head.components(separatedBy: "\r\n")
        let target = lines.first?.split(separator: " ").dropFirst().first.map(String.init) ?? ""
        guard target.split(separator: "?").first.map(String.init) == path else {
            let body = Data("not found".utf8)
            connection.send(content: Data("HTTP/1.1 404 Not Found\r\nContent-Length: \(body.count)\r\n\r\n".utf8) + body,
                            completion: .idempotent)
            return
        }
        let range = lines.first { $0.lowercased().hasPrefix("range:") }
        guard let range else {
            connection.send(content: Data("HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: \(payload.count)\r\n\r\n".utf8) + payload,
                            completion: .idempotent)
            return
        }
        let bounds = range.split(separator: "=")[1].split(separator: "-", omittingEmptySubsequences: false)
        let start = Int(bounds[0])!
        let end = min(bounds[1].isEmpty ? payload.count - 1 : Int(bounds[1])!, payload.count - 1)
        let body = payload.subdata(in: start..<(end + 1))
        var response = "HTTP/1.1 206 Partial Content\r\nContent-Type: video/mp2t\r\n"
        response += "Content-Range: bytes \(start)-\(end)/\(payload.count)\r\nContent-Length: \(body.count)\r\n\r\n"
        connection.send(content: Data(response.utf8) + body, completion: .idempotent)
    }
}
