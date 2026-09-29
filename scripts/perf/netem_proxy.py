#!/usr/bin/env python3
"""链路模拟代理：在 App 与后端之间放一条「手机的无线链路」（netem 的用户态替身）。

为什么需要
----------
iOS 模拟器与后端跑在同一台 Mac 上，回环网络的 RTT 是几十微秒、带宽几十 Gbps——
在这样的网络上测「页面打开速度」，量到的只是 CPU，请求瀑布、连接数、图片体积这些
真正决定手机体感的因素全被抹平了。macOS 上的系统级限速（dummynet / Network Link
Conditioner）要 root 且会影响整机，本机还跑着别人的服务，不能碰。于是在用户态放
一个 TCP 代理：App 连代理端口，代理按设定的时延与带宽把字节转给后端。

链路模型
--------
- **单向时延 = RTT/2**，逐块施加，每个方向一条 FIFO，交付顺序与到达顺序一致；
- **新连接多付一个 RTT**：真实 TCP 要等 SYN/SYN-ACK 往返完才能发第一个请求，
  这里实现为「该连接的第一块上行数据额外晚到 ``--setup-rtts`` × RTT」（默认 1；
  想模拟 HTTPS 握手可设 2~3）。本机 connect() 本身瞬间完成，所以这段握手成本在
  curl 的 ``time_connect`` 里看不到，体现在 ``time_starttransfer`` 里；
- **带宽**：每个方向一个**所有连接共享**的令牌桶——手机只有一条无线链路，App 并发
  取的六张海报是在抢同一份下行带宽，而不是每条连接各有 25 Mbps。数据切成不超过
  桶深（默认 16 KB）的小片依次取令牌，多条连接的数据在链路上交错排队；
- **按路径的额外延迟（可选）**：``--slow /api/v1/discover=1500`` 让命中前缀的请求
  晚到 1500 ms——等价于「这个接口在服务端慢了 1.5 秒」。只在上行块开头廉价地认一下
  HTTP 请求行（``GET /path HTTP/1.1``），不解析请求体；同一连接上后面的请求自然排在
  它之后（HTTP/1.1 本来就不并行）。

每个方向的流水线：reader 协程从源端读块 → 切片、取令牌（带宽）→ 打上交付时刻
（取到令牌的时刻 + 单向时延 [+ 握手 / 慢路径附加]）→ 入队；writer 协程按序出队、
睡到交付时刻、写给目的端。reader 卡在令牌桶上即对源端形成背压（TCP 流控），队列里
最多积压「带宽 × 时延」量级的数据。

预设
----
=======  ==========  =========  ============  ==========
profile  监听端口     RTT        下行          上行
=======  ==========  =========  ============  ==========
lan      18601       6 ms       200 Mbps      50 Mbps
wan      18602       70 ms      25 Mbps       8 Mbps
=======  ==========  =========  ============  ==========

用法::

    python scripts/perf/netem_proxy.py --profile wan            # 127.0.0.1:18602 → 127.0.0.1:18600
    python scripts/perf/netem_proxy.py --profile lan --slow /api/v1/discover=1500
    python scripts/perf/netem_proxy.py --listen 127.0.0.1:18605 \\
        --rtt-ms 150 --down-mbps 5 --up-mbps 1
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import sys
import time

PROFILES = {
    "lan": {"listen": "127.0.0.1:18601", "rtt_ms": 6.0, "down_mbps": 200.0, "up_mbps": 50.0},
    "wan": {"listen": "127.0.0.1:18602", "rtt_ms": 70.0, "down_mbps": 25.0, "up_mbps": 8.0},
}
_METHODS = (b"GET ", b"POST ", b"PUT ", b"PATCH ", b"DELETE ", b"HEAD ", b"OPTIONS ")
_READ_SIZE = 64 * 1024


def _log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


class TokenBucket:
    """一个方向的共享带宽：GCRA（虚拟完成时刻）形式的令牌桶。

    链路上的数据片首尾相接地「发送」：每片的虚拟发送完成时刻 =
    max(上一片的完成时刻, 现在) + 片长 / 速率；只要完成时刻领先现在不超过桶深
    对应的时长（突发容忍 tau）就放行，否则睡到「完成时刻 - tau」。与「补令牌、
    扣令牌」的写法等价，但 sleep 的调度误差（macOS 上每次约 0.5~1 ms）不会累积
    成吞吐损失——完成时刻按理论值推进，而不是按实际醒来的时刻（实测朴素写法在
    25 Mbps 下只能跑到 21 Mbps 左右）。

    asyncio.Lock 按先来后到唤醒等待者，所以多条连接取令牌是公平排队的。
    """

    def __init__(self, rate_bytes: float, burst_bytes: int) -> None:
        self.rate = rate_bytes
        self.tau = burst_bytes / rate_bytes
        self.finish = 0.0
        self.lock = asyncio.Lock()

    async def take(self, size: int) -> None:
        async with self.lock:
            now = time.monotonic()
            self.finish = max(self.finish, now) + size / self.rate
            wait = self.finish - self.tau - now
            if wait > 0:
                await asyncio.sleep(wait)


class Link:
    """一条模拟链路：两个方向的共享令牌桶 + 时延参数 + 慢路径规则。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.one_way = args.rtt_ms / 2000
        self.setup = args.rtt_ms / 1000 * args.setup_rtts
        burst = int(args.burst_kb * 1024)
        self.piece = burst
        self.down = TokenBucket(args.down_mbps * 1e6 / 8, burst) if args.down_mbps > 0 else None
        self.up = TokenBucket(args.up_mbps * 1e6 / 8, burst) if args.up_mbps > 0 else None
        self.slow: list[tuple[bytes, float]] = []
        for rule in args.slow or []:
            prefix, _, ms = rule.rpartition("=")
            self.slow.append((prefix.encode(), float(ms) / 1000))
        host, _, port = args.upstream.rpartition(":")
        self.upstream = (host, int(port))
        self.ids = itertools.count(1)

    def slow_extra(self, chunk: bytes) -> float:
        """上行块若以 HTTP 请求行开头且命中慢路径前缀，返回附加延迟（秒）。"""
        if not self.slow or not chunk.startswith(_METHODS):
            return 0.0
        target = chunk.split(b" ", 2)[1] if chunk.count(b" ") >= 2 else b""
        return max((ms for prefix, ms in self.slow if target.startswith(prefix)), default=0.0)

    async def pump(
        self,
        src: asyncio.StreamReader,
        dst: asyncio.StreamWriter,
        bucket: TokenBucket | None,
        *,
        upstream_bound: bool,
        stats: list[int],
        index: int,
    ) -> None:
        """一个方向的流水线：读 → 取令牌 → 定交付时刻 → 按序睡到点再写。"""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[float, bytes] | None] = asyncio.Queue()

        async def read_side() -> None:
            first = upstream_bound
            try:
                while chunk := await src.read(_READ_SIZE):
                    extra = 0.0
                    if first:
                        extra += self.setup  # TCP 握手：首个请求要等一个往返才能发出
                        first = False
                    if upstream_bound:
                        extra += self.slow_extra(chunk)
                    for offset in range(0, len(chunk), self.piece):
                        piece = chunk[offset : offset + self.piece]
                        if bucket is not None:
                            await bucket.take(len(piece))
                        # 附加延迟只挂在第一片上：后面的片交付时刻更早，但 FIFO 保证
                        # 它们仍排在第一片之后，整块一起晚到
                        await queue.put((loop.time() + self.one_way + extra, piece))
                        extra = 0.0
            except (ConnectionError, OSError):
                pass
            finally:
                await queue.put(None)

        async def write_side() -> None:
            try:
                while (item := await queue.get()) is not None:
                    due, piece = item
                    delay = due - loop.time()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    dst.write(piece)
                    stats[index] += len(piece)
                    await dst.drain()
                if dst.can_write_eof():
                    dst.write_eof()  # 半关闭：把对端的 EOF 如实转过去
            except (ConnectionError, OSError):
                pass

        await asyncio.gather(read_side(), write_side())

    async def handle(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        conn_id = next(self.ids)
        started = time.perf_counter()
        try:
            server_reader, server_writer = await asyncio.open_connection(*self.upstream)
        except OSError as exc:
            _log(f"conn#{conn_id} 连不上后端 {self.upstream[0]}:{self.upstream[1]}：{exc}")
            client_writer.close()
            return
        stats = [0, 0]  # 上行字节、下行字节
        try:
            await asyncio.gather(
                self.pump(
                    client_reader, server_writer, self.up, upstream_bound=True, stats=stats, index=0
                ),
                self.pump(
                    server_reader,
                    client_writer,
                    self.down,
                    upstream_bound=False,
                    stats=stats,
                    index=1,
                ),
            )
        finally:
            server_writer.close()
            client_writer.close()
            _log(
                f"conn#{conn_id} 关闭 上行 {stats[0] / 1024:.1f}KB 下行 {stats[1] / 1024:.1f}KB "
                f"存活 {time.perf_counter() - started:.2f}s"
            )


async def serve(args: argparse.Namespace) -> None:
    link = Link(args)
    host, _, port = args.listen.rpartition(":")
    server = await asyncio.start_server(link.handle, host, int(port))
    slow = "，".join(args.slow) if args.slow else "无"
    _log(
        f"链路模拟代理 [{args.profile or 'custom'}] 已监听 {args.listen} → {args.upstream}："
        f"RTT {args.rtt_ms}ms（单向 {args.rtt_ms / 2}ms，新连接另加 {args.setup_rtts}×RTT），"
        f"下行 {args.down_mbps or '不限'} Mbps / 上行 {args.up_mbps or '不限'} Mbps"
        f"（全连接共享，桶深 {args.burst_kb}KB），慢路径：{slow}"
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", choices=sorted(PROFILES), help="预设：lan / wan")
    parser.add_argument("--listen", help="监听地址（默认取预设）")
    parser.add_argument("--upstream", default="127.0.0.1:18600", help="后端地址")
    parser.add_argument("--rtt-ms", type=float, help="往返时延（毫秒）")
    parser.add_argument("--down-mbps", type=float, help="下行带宽（Mbps），0=不限")
    parser.add_argument("--up-mbps", type=float, help="上行带宽（Mbps），0=不限")
    parser.add_argument("--burst-kb", type=float, default=16.0, help="令牌桶深度（KB）")
    parser.add_argument(
        "--setup-rtts",
        type=float,
        default=1.0,
        help="新连接的建连成本（几个 RTT）：TCP=1，模拟 TLS 可设 2~3",
    )
    parser.add_argument(
        "--slow",
        action="append",
        metavar="PREFIX=MS",
        help="按路径前缀给请求加延迟，可多次给，如 /api/v1/discover=1500",
    )
    args = parser.parse_args()
    preset = PROFILES.get(args.profile or "", {})
    for key, value in preset.items():
        if getattr(args, key) is None:
            setattr(args, key, value)
    missing = [k for k in ("listen", "rtt_ms", "down_mbps", "up_mbps") if getattr(args, k) is None]
    if missing:
        parser.error(f"未给 --profile 时必须显式指定：{', '.join(missing)}")
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
