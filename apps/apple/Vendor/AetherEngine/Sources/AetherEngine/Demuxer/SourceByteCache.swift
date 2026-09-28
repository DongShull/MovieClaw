import Darwin
import Foundation

/// [MovieClaw P22] 片源字节缓存：同一场播放里，同一个片源的每个字节只从源站下一次。
///
/// ## 为什么要有
/// 引擎里好几条路径会重读已经下过的字节，每条都要再找源站要一遍：
/// - 换音轨、回前台、出错恢复都是整场重建（`reloadWithAudioOverride`），攒好的前向缓冲整段作废；
///   MP4 还要把文件尾的索引（大片几十 MB）再下一遍；
/// - 往回跳进分片缓存的连续段、段后是空档时，生产者从跳转目标重启，已缓存那几段的源字节再读一遍；
/// - 软件通路往后跳会丢掉历史，跳回看过的位置重下；
/// - 画中画的字幕旁路是第二条连接，在 MKV / TS 上把音视频字节再下一遍。
/// 2026-09-28 模拟器实测：MKV 换一次音轨多下约 35 秒的量（61 MB），UHD 原盘往回跳多下 460 MB。
///
/// ## 怎么做
/// `AVIOReader` 从网络收到的每一块都按文件偏移写进这里（本机临时目录里的稀疏文件），读的时候窗口
/// 给不了就先查这里；重新打开时缓存里已有文件头就当预热数据接管、一个请求都不发，读到缓存没有的
/// 位置才按那个位置连源站（读取循环本来就这样做）。换音轨后从播放点重读的整段前向缓冲、MP4 索引、
/// 回跳重产的那几段，都直接从本机拿。
///
/// ## 键
/// 取流地址每次都带新令牌，不能拿地址当键。主机在装载时给一个稳定的键（`LoadOptions.sourceCacheKey`，
/// MovieClaw 用「文件 id + 大小」），引擎把本次地址登记到这个键上（`bind`），之后打开同一地址的所有
/// 读取者（探测、播放、重建、字幕旁路）都落到同一份缓存。连接报的文件大小与缓存记的对不上，说明源站
/// 上的文件换过了，整份作废。
///
/// ## 预算与清理
/// 全进程共用一个预算，默认 min(1 GiB, 临时目录可用空间的 1/8)，每来一个新片源按当时的可用空间重算。按 1 MiB
/// 的块记最近使用，超了先丢最久没用的块（在稀疏文件上打洞，空间立刻还回去），丢到预算的九成为止，块丢光的片源
/// 整条删掉。关掉播放器不清：退出再进同一部片、断线重连换了引擎实例，续播点附近都直接从本机起播；App 下次启动时
/// 由 `sweep` 清掉上次留下的文件。
final class SourceByteCache: @unchecked Sendable {

    static let shared = SourceByteCache()

    /// 记账与淘汰的粒度。每块只记一段连续覆盖（网络数据按顺序到，够用；零散写入不连续时以新的为准）
    static let blockSize: Int64 = 1 << 20

    static var directory: URL {
        URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("aether-bytecache", isDirectory: true)
    }

    private struct Block {
        var lo: Int64
        var hi: Int64
        var lastUse: UInt64
    }

    private final class Entry {
        let fd: Int32
        let path: String
        var contentLength: Int64?
        var blocks: [Int64: Block] = [:]

        init(fd: Int32, path: String) {
            self.fd = fd
            self.path = path
        }

        deinit {
            close(fd)
            unlink(path)
        }
    }

    private let lock = NSLock()
    private var entries: [String: Entry] = [:]
    /// 本次取流地址（含令牌）→ 键
    private var keysByURL: [String: String] = [:]
    private var useClock: UInt64 = 0
    private var totalBytes: Int64 = 0
    private var budgetBytes: Int64?
    /// 临时目录建不出来 / 写失败过：本进程不再缓存（照常走网络，不影响播放）
    private var disabled = false

    /// 测试给定的预算；nil = 按可用空间算（`budgetLocked`）
    private let fixedBudget: Int64?

    init(budgetBytes: Int64? = nil) {
        self.fixedBudget = budgetBytes
    }
    #if DEBUG
    /// 开发期统计：累计写入 / 从缓存供出的字节，每 5 秒打一行（核对换轨、回跳到底省了多少）
    private var debugWritten: Int64 = 0
    private var debugServed: Int64 = 0
    private var debugLastLog = Date.distantPast

    private func debugTallyLocked(written: Int64 = 0, served: Int64 = 0) {
        debugWritten += written
        debugServed += served
        guard Date().timeIntervalSince(debugLastLog) >= 5 else { return }
        debugLastLog = Date()
        EngineLog.emit("[SourceByteCache] [MovieClaw P22] written \(debugWritten >> 20) MB, "
                       + "served from cache \(debugServed >> 20) MB, resident \(totalBytes >> 20) MB",
                       category: .demux)
    }
    #endif

    // MARK: 键

    /// 把本次地址登记到稳定的键上；键为 nil 时撤销登记（这个地址不缓存）。
    /// 同一个键以前登记过的旧地址（旧令牌）一并忘掉：已经打开的读取者在初始化时就取走了键，不受影响
    func bind(url: URL, key: String?) {
        lock.lock(); defer { lock.unlock() }
        if let key {
            for (old, bound) in keysByURL where bound == key && old != url.absoluteString {
                keysByURL.removeValue(forKey: old)
            }
        }
        keysByURL[url.absoluteString] = key
    }

    func key(for url: URL) -> String? {
        lock.lock(); defer { lock.unlock() }
        return keysByURL[url.absoluteString]
    }

    // MARK: 读写

    /// 网络收到的一块，按它在文件里的偏移记下
    func write(key: String, offset: Int64, data: Data) {
        guard !data.isEmpty, offset >= 0 else { return }
        lock.lock(); defer { lock.unlock() }
        guard !disabled, let entry = entryLocked(key) else { return }
        let written = data.withUnsafeBytes { raw -> Int in
            guard let base = raw.baseAddress else { return -1 }
            return pwrite(entry.fd, base, data.count, off_t(offset))
        }
        guard written == data.count else {
            // 多半是磁盘满了：这份作废，本进程不再缓存
            EngineLog.emit("[SourceByteCache] [MovieClaw P22] write failed (errno=\(errno)); caching off",
                           category: .demux)
            disabled = true
            dropLocked(key)
            return
        }
        useClock += 1
        #if DEBUG
        debugTallyLocked(written: Int64(data.count))
        #endif
        var cursor = offset
        let end = offset + Int64(data.count)
        while cursor < end {
            let index = cursor / Self.blockSize
            let blockEnd = min(end, (index + 1) * Self.blockSize)
            let before = entry.blocks[index].map { $0.hi - $0.lo } ?? 0
            if var block = entry.blocks[index], cursor <= block.hi, blockEnd >= block.lo {
                block.lo = min(block.lo, cursor)
                block.hi = max(block.hi, blockEnd)
                block.lastUse = useClock
                entry.blocks[index] = block
            } else if (entry.blocks[index].map { blockEnd - cursor >= $0.hi - $0.lo } ?? true) {
                // 与已有覆盖不相连：留长的那段
                entry.blocks[index] = Block(lo: cursor, hi: blockEnd, lastUse: useClock)
            }
            totalBytes += (entry.blocks[index].map { $0.hi - $0.lo } ?? 0) - before
            cursor = blockEnd
        }
        enforceBudgetLocked()
    }

    /// 从 `offset` 起有多少连续缓存就读多少（至多 `maxLen`），返回读到的字节数；0 = 没有
    func read(key: String, offset: Int64, into dst: UnsafeMutablePointer<UInt8>, maxLen: Int) -> Int {
        guard maxLen > 0, offset >= 0 else { return 0 }
        lock.lock(); defer { lock.unlock() }
        guard let entry = entries[key] else { return 0 }
        let available = contiguousEndLocked(entry, from: offset) - offset
        guard available > 0 else { return 0 }
        let count = Int(min(Int64(maxLen), available))
        let got = pread(entry.fd, dst, count, off_t(offset))
        guard got > 0 else { return 0 }
        useClock += 1
        #if DEBUG
        debugTallyLocked(served: Int64(got))
        #endif
        var index = offset / Self.blockSize
        while index * Self.blockSize < offset + Int64(got) {
            entry.blocks[index]?.lastUse = useClock
            index += 1
        }
        return got
    }

    /// 从 `offset` 起连续缓存到哪里（不含）；没有缓存时就是 `offset`
    func contiguousEnd(key: String, from offset: Int64) -> Int64 {
        lock.lock(); defer { lock.unlock() }
        guard let entry = entries[key] else { return offset }
        return contiguousEndLocked(entry, from: offset)
    }

    /// 缓存里 [offset, offset + length) 完整时复制出来（打开时当预热的文件头 / 文件尾用）
    func copy(key: String, offset: Int64, length: Int) -> Data? {
        guard length > 0 else { return nil }
        lock.lock(); defer { lock.unlock() }
        guard let entry = entries[key], contiguousEndLocked(entry, from: offset) >= offset + Int64(length) else {
            return nil
        }
        var data = Data(count: length)
        let got = data.withUnsafeMutableBytes { raw -> Int in
            guard let base = raw.baseAddress else { return -1 }
            return pread(entry.fd, base, length, off_t(offset))
        }
        return got == length ? data : nil
    }

    // MARK: 文件大小（校验缓存还是不是同一个文件）

    /// 连接报了文件大小：与缓存记的对不上就整份作废（源站上的文件换过了），然后记下这个大小
    func noteContentLength(key: String, length: Int64) {
        guard length > 0 else { return }
        lock.lock(); defer { lock.unlock() }
        if let known = entries[key]?.contentLength, known != length {
            EngineLog.emit("[SourceByteCache] [MovieClaw P22] \(key): size \(known)B -> \(length)B, "
                           + "the source changed; dropping its cached bytes", category: .demux)
            dropLocked(key)
        }
        guard !disabled, let entry = entryLocked(key) else { return }
        entry.contentLength = length
    }

    func contentLength(key: String) -> Int64? {
        lock.lock(); defer { lock.unlock() }
        return entries[key]?.contentLength
    }

    // MARK: 清理

    /// 播放器关了：这一场的字节都不要了
    func purgeAll() {
        lock.lock(); defer { lock.unlock() }
        entries.removeAll()
        keysByURL.removeAll()
        totalBytes = 0
    }

    /// App 启动时调：删掉死会话留下的缓存文件。清扫在后台线程跑，可能晚于刚开始的播放，
    /// 所以本进程正开着的文件跳过（删了它，这一场的缓存就成了看不见的孤儿文件）
    static func sweep() {
        let live = shared.livePaths()
        let files = (try? FileManager.default.contentsOfDirectory(atPath: directory.path)) ?? []
        for name in files {
            let path = directory.appendingPathComponent(name).path
            if !live.contains(path) { unlink(path) }
        }
    }

    private func livePaths() -> Set<String> {
        lock.lock(); defer { lock.unlock() }
        return Set(entries.values.map(\.path))
    }

    var cachedBytes: Int64 {
        lock.lock(); defer { lock.unlock() }
        return totalBytes
    }

    // MARK: - 内部（调用方已持锁）

    private func entryLocked(_ key: String) -> Entry? {
        if let entry = entries[key] { return entry }
        budgetBytes = nil   // 新片源：按现在的可用空间重算预算
        let dir = Self.directory
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let path = dir.appendingPathComponent(UUID().uuidString).path
        let fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0o600)
        guard fd >= 0 else {
            disabled = true
            return nil
        }
        let entry = Entry(fd: fd, path: path)
        entries[key] = entry
        return entry
    }

    private func dropLocked(_ key: String) {
        guard let entry = entries.removeValue(forKey: key) else { return }
        totalBytes -= entry.blocks.values.reduce(0) { $0 + ($1.hi - $1.lo) }
    }

    private func contiguousEndLocked(_ entry: Entry, from offset: Int64) -> Int64 {
        var cursor = offset
        while let block = entry.blocks[cursor / Self.blockSize], block.lo <= cursor, cursor < block.hi {
            cursor = block.hi
            // 这块没写满到块尾：连续覆盖到此为止
            if cursor % Self.blockSize != 0 { break }
        }
        return cursor
    }

    private func budgetLocked() -> Int64 {
        if let fixedBudget { return fixedBudget }
        if let budgetBytes { return budgetBytes }
        // [MovieClaw P25] 经统一入口读（测试可覆盖）
        #if os(macOS)
        let available = AetherEngine.temporaryVolumeAvailableBytes(importantUsage: false)
        #else
        let available = AetherEngine.temporaryVolumeAvailableBytes(importantUsage: true)
        #endif
        let budget = min(Int64(1) << 30, (available ?? 0) / 8)
        budgetBytes = budget
        return budget
    }

    /// 超了预算：按最近使用从旧到新丢块，丢到预算的九成（成批丢，免得每写一块都扫一遍）
    private func enforceBudgetLocked() {
        let budget = budgetLocked()
        guard totalBytes > budget else { return }
        var candidates: [(key: String, index: Int64, lastUse: UInt64)] = []
        for (key, entry) in entries {
            for (index, block) in entry.blocks { candidates.append((key, index, block.lastUse)) }
        }
        candidates.sort { $0.lastUse < $1.lastUse }
        let target = budget / 10 * 9
        for candidate in candidates where totalBytes > target {
            guard let entry = entries[candidate.key], let block = entry.blocks.removeValue(forKey: candidate.index) else {
                continue
            }
            totalBytes -= block.hi - block.lo
            var hole = fpunchhole_t(fp_flags: 0, reserved: 0,
                                    fp_offset: off_t(candidate.index * Self.blockSize),
                                    fp_length: off_t(Self.blockSize))
            _ = fcntl(entry.fd, F_PUNCHHOLE, &hole)
        }
        // 块丢光的片源整条删（关文件、删文件），免得看过的片子多了文件句柄越攒越多
        for (key, entry) in entries where entry.blocks.isEmpty {
            entries.removeValue(forKey: key)
        }
    }
}
