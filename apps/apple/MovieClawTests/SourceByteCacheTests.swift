import Foundation
import Testing
@testable import AetherEngine

/// 片源字节缓存（引擎补丁 P22，`SourceByteCache`）：按偏移存、按连续覆盖取、大小对不上作废、超预算按最近使用淘汰。
/// 缓存给出的字节必须与写进去的逐字节一致——错一个字节就是花屏或解码失败。
struct SourceByteCacheTests {
    private let block = Int(SourceByteCache.blockSize)

    private func bytes(_ count: Int, seed: UInt8) -> Data {
        Data((0 ..< count).map { UInt8(truncatingIfNeeded: $0 &* 31 &+ Int(seed)) })
    }

    private func read(_ cache: SourceByteCache, _ key: String, at offset: Int64, max: Int) -> Data {
        var buffer = [UInt8](repeating: 0, count: max)
        let n = buffer.withUnsafeMutableBufferPointer { cache.read(key: key, offset: offset, into: $0.baseAddress!, maxLen: max) }
        return Data(buffer.prefix(n))
    }

    @Test func servesExactlyWhatWasWrittenAcrossBlocks() {
        let cache = SourceByteCache(budgetBytes: 64 << 20)
        let key = "t-\(UUID())"
        // 从块中间开始、跨过两个块边界的一段
        let start = Int64(block / 2)
        let data = bytes(block * 2 + 1000, seed: 7)
        cache.write(key: key, offset: start, data: data)
        #expect(cache.contiguousEnd(key: key, from: start) == start + Int64(data.count))
        #expect(read(cache, key, at: start, max: data.count) == data)
        // 从中间任意位置读：给出的就是对应的那几个字节
        let mid = start + 12345
        #expect(read(cache, key, at: mid, max: 4096) == data.subdata(in: 12345 ..< 12345 + 4096))
        // 覆盖之外：不给（读取器转去问源站）
        #expect(read(cache, key, at: start - 1, max: 10).isEmpty)
        #expect(read(cache, key, at: start + Int64(data.count), max: 10).isEmpty)
        cache.purgeAll()
    }

    @Test func sequentialChunksMergeIntoOneRun() {
        // 网络按顺序一块块到：相连的写入合并成连续覆盖，读的时候一次给到尾
        let cache = SourceByteCache(budgetBytes: 64 << 20)
        let key = "t-\(UUID())"
        let whole = bytes(block + 50_000, seed: 3)
        var offset = 0
        for size in [16_384, 65_536, 300_000, whole.count - 16_384 - 65_536 - 300_000] {
            cache.write(key: key, offset: Int64(offset), data: whole.subdata(in: offset ..< offset + size))
            offset += size
        }
        #expect(cache.contiguousEnd(key: key, from: 0) == Int64(whole.count))
        #expect(read(cache, key, at: 0, max: whole.count) == whole)
        cache.purgeAll()
    }

    @Test func gapStopsTheRun() {
        // 中间有洞：连续覆盖到洞前为止，洞后面的另算
        let cache = SourceByteCache(budgetBytes: 64 << 20)
        let key = "t-\(UUID())"
        cache.write(key: key, offset: 0, data: bytes(100_000, seed: 1))
        cache.write(key: key, offset: Int64(block) * 3, data: bytes(100_000, seed: 2))
        #expect(cache.contiguousEnd(key: key, from: 0) == 100_000)
        #expect(read(cache, key, at: 50_000, max: 200_000).count == 50_000)
        #expect(read(cache, key, at: Int64(block) * 3, max: 10) == bytes(10, seed: 2))
        cache.purgeAll()
    }

    @Test func headAndTailCopiesForReopen() {
        let cache = SourceByteCache(budgetBytes: 64 << 20)
        let key = "t-\(UUID())"
        let head = bytes(200_000, seed: 9)
        cache.write(key: key, offset: 0, data: head)
        #expect(cache.copy(key: key, offset: 0, length: 100_000) == head.prefix(100_000))
        // 要的比缓存里有的长：不给半截
        #expect(cache.copy(key: key, offset: 0, length: 300_000) == nil)
        cache.purgeAll()
    }

    @Test func sizeMismatchDropsTheSource() {
        // 连接报的文件大小与缓存记的对不上：源站上的文件换过了，已缓存的字节整份作废
        let cache = SourceByteCache(budgetBytes: 64 << 20)
        let key = "t-\(UUID())"
        cache.noteContentLength(key: key, length: 1_000_000)
        cache.write(key: key, offset: 0, data: bytes(4096, seed: 4))
        cache.noteContentLength(key: key, length: 1_000_000)
        #expect(cache.contiguousEnd(key: key, from: 0) == 4096)
        cache.noteContentLength(key: key, length: 2_000_000)
        #expect(cache.contiguousEnd(key: key, from: 0) == 0)
        #expect(cache.contentLength(key: key) == 2_000_000)
        cache.purgeAll()
    }

    @Test func evictsLeastRecentlyUsedBlocksOverBudget() {
        // 预算 4 块：写满 6 块后丢最久没用的，只剩预算九成以内
        let cache = SourceByteCache(budgetBytes: Int64(block) * 4)
        let key = "t-\(UUID())"
        for index in 0 ..< 6 {
            cache.write(key: key, offset: Int64(block * index), data: bytes(block, seed: UInt8(index)))
        }
        #expect(cache.cachedBytes <= Int64(block) * 4)
        // 最早写的块没了，最新写的还在、内容不变
        #expect(read(cache, key, at: 0, max: 10).isEmpty)
        #expect(read(cache, key, at: Int64(block * 5), max: block) == bytes(block, seed: 5))
        cache.purgeAll()
    }

    @Test func emptiedSourcesAreDropped() {
        // 预算 3 块：旧片源的 2 块被新片源的 2 块挤光后，整条删掉（不留空条目占着文件句柄）
        let cache = SourceByteCache(budgetBytes: Int64(block) * 3)
        let old = "t-\(UUID())", new = "t-\(UUID())"
        cache.noteContentLength(key: old, length: 10 << 20)
        cache.write(key: old, offset: 0, data: bytes(block * 2, seed: 1))
        cache.write(key: new, offset: 0, data: bytes(block * 2, seed: 2))
        #expect(cache.contentLength(key: old) == nil)
        #expect(read(cache, new, at: 0, max: block * 2) == bytes(block * 2, seed: 2))
        cache.purgeAll()
    }

    @Test func rebindingAKeyForgetsTheOldToken() {
        let cache = SourceByteCache(budgetBytes: 64 << 20)
        let first = URL(string: "http://nas:3000/api/v1/playback/files/7/stream?token=a")!
        let second = URL(string: "http://nas:3000/api/v1/playback/files/7/stream?token=b")!
        cache.bind(url: first, key: "file-7-100")
        cache.bind(url: second, key: "file-7-100")
        #expect(cache.key(for: second) == "file-7-100")
        #expect(cache.key(for: first) == nil)
        cache.purgeAll()
    }

    @Test func keysBindPerURL() {
        // 每个地址各自登记到自己的键上（原盘目录每个文件一个）
        let cache = SourceByteCache(budgetBytes: 64 << 20)
        let first = URL(string: "http://nas:3000/api/v1/playback/files/7/stream?token=a")!
        let second = URL(string: "http://nas:3000/api/v1/playback/files/7/stream?token=b")!
        let disc = URL(string: "http://nas:3000/api/v1/playback/files/9/disc/BDMV/STREAM/00001.m2ts?token=a")!
        cache.bind(url: first, key: "file-7-100")
        cache.bind(url: disc, key: "file-9-200/BDMV/STREAM/00001.m2ts")
        #expect(cache.key(for: first) == "file-7-100")
        #expect(cache.key(for: disc) == "file-9-200/BDMV/STREAM/00001.m2ts")
        #expect(cache.key(for: second) == nil)
        #expect(cache.key(for: URL(string: "http://nas:3000/other")!) == nil)
        cache.purgeAll()
        #expect(cache.key(for: first) == nil)
    }
}
