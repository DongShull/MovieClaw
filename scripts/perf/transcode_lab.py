#!/usr/bin/env python3
"""转码链路实验台：真 NAS + 真转码 Worker + 本机 AVPlayer，量转码起播与跳转的端到端时延。

docs/design/transcode-latency.md 的配套工具（指标口径见该文 §1，测法见 §3）。

为什么要它：转码链路横跨四台机器上的四段程序（客户端 → NAS → Worker 上的 ffmpeg →
NAS → 客户端），单看任何一端的日志都拼不出用户等的那几秒花在哪。这里在本机放一个
真客户端（transcode_probe.swift，系统 AVPlayer，与 iOS App 放服务端转码流同一套
AVFoundation），所有取流都经本机的记录代理，再从 NAS 的诊断接口与日志取服务端视角，
三方按同一次播放对齐。

子命令：

  corpus  从 NAS 片库（只读）按格式挑语料，写到 ~/.config/movieclaw/transcode-lab-corpus.json
          （个人片库条目，不入库）
  run     按场景逐部播放、跳转，每次播放一行 JSONL，连同代理逐请求记录与 NAS 日志
  report  汇总一批，或对照两批（中位 / p90 / 按片配对差值）

用法：

  MC_PASSWORD=... scripts/perf/transcode_lab.py corpus
  MC_PASSWORD=... scripts/perf/transcode_lab.py run --tag base --expect-worker Yi-Mac-mini
  scripts/perf/transcode_lab.py report ~/workspace/.mc-lab/transcode/base-*
  scripts/perf/transcode_lab.py report <A 批目录> <B 批目录>     # 对照

环境变量：

  MC_SERVER    服务器（默认 http://192.168.1.10:3000）
  MC_USER      登录账号（默认 yee）；MC_PASSWORD 密码（必填，不落盘）
  MC_SSH       NAS 的 ssh 主机名（默认 nas）：读片库、清本次语料的转码缓存、取日志

几条纪律（踩过才写下的）：

- **只在 Worker 空闲时开播**。Worker 只有一个并发槽，别人（家人、别的实验）正在转码时
  开会话会落到 NAS 本机软件转码（档 4），既测错了东西，又把 NAS 的 CPU 打满。每次开播前
  等目标 Worker 空闲；开出来不是「档 3 + videotoolbox + 目标 Worker」的当场作废、立即停掉。
- **每次播放前清掉这部片的转码缓存**（NAS 上 manifest 里 file_id 匹配的冷目录），否则
  第二轮会命中上一轮转好的分片，量到的是读文件而不是转码。
- **交替跑**：同一部片的各组轮流跑、每轮打乱顺序，抵消 NAS 页缓存的冷热差。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import http.cookiejar
import json
import os
import queue
import random
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

SERVER = os.environ.get("MC_SERVER", "http://192.168.1.10:3000").rstrip("/")
SSH_HOST = os.environ.get("MC_SSH", "nas")
DOCKER = "/usr/local/bin/docker"
CONTAINER = "movieclaw"
CORPUS_PATH = Path.home() / ".config/movieclaw/transcode-lab-corpus.json"
OUT_ROOT = Path.home() / "workspace/.mc-lab/transcode"
REPO = Path(__file__).resolve().parents[2]
PROBE_SRC = REPO / "scripts/perf/transcode_probe.swift"
NETEM = REPO / "scripts/perf/netem_proxy.py"
DEVICE_ID = "transcode-lab"

#: 与 iOS App 的 PlayerCapability.avPlayer() 一致：限画质时 App 按它申报，服务端据此转码
AVPLAYER_CAPABILITY: dict[str, Any] = {
    "video": [
        {"codec": "h264", "max_height": 2160, "smooth": True, "power_efficient": True},
        {"codec": "hevc", "max_height": 2160, "smooth": True, "power_efficient": True},
    ],
    "audio": [
        {"codec": "aac", "max_channels": 8},
        {"codec": "ac3", "max_channels": 6},
        {"codec": "eac3", "max_channels": 8},
        {"codec": "flac", "max_channels": 8},
        {"codec": "alac", "max_channels": 8},
        {"codec": "mp3", "max_channels": 2},
    ],
    "containers": ["mp4", "hls-fmp4"],
    "hdr_passthrough": False,
    "mse": "none",
    "is_mobile": True,
    "native_hls": True,
}

#: 慢线路档位（经 netem_proxy 限的是 NAS → 本机方向）。6 Mbit/s、60 毫秒是 playback-qoe.md
#: 第八轮复现「外网蜂窝看 4K 原片」用的同一档
LINKS: dict[str, dict[str, float]] = {
    "lan": {},
    "6m": {"rtt_ms": 60, "down_mbps": 6, "up_mbps": 2},
    "20m": {"rtt_ms": 40, "down_mbps": 20, "up_mbps": 5},
}


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


# ---------------------------------------------------------------------------
# 服务端接口
# ---------------------------------------------------------------------------


class Api:
    """带登录 Cookie 的最小接口客户端（只用标准库）。"""

    def __init__(self, server: str) -> None:
        self.server = server
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def login(self, user: str, password: str) -> None:
        self.call(
            "POST", "/api/v1/auth/login", {"username": user, "password": password, "remember": True}
        )

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.server + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            payload = error.read()
            try:
                return json.loads(payload)
            except ValueError:
                return {
                    "success": False,
                    "status": error.code,
                    "message": payload[:200].decode(errors="replace"),
                }

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)


def login_from_env() -> Api:
    password = os.environ.get("MC_PASSWORD")
    if not password:
        sys.exit("请用环境变量 MC_PASSWORD 提供登录密码（不落盘）")
    api = Api(SERVER)
    api.login(os.environ.get("MC_USER", "yee"), password)
    if not api.cookie_header():
        sys.exit("登录失败：没有拿到会话 Cookie")
    return api


# ---------------------------------------------------------------------------
# NAS 侧（ssh）：读片库、清转码缓存、取日志
# ---------------------------------------------------------------------------


def nas_python(code: str, *args: str, timeout: float = 60) -> str:
    """在 NAS 的应用容器里跑一段 Python（经 stdin 传入，免去层层转义）。"""
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        SSH_HOST,
        f"{DOCKER} exec -i {CONTAINER} python3 - " + " ".join(args),
    ]
    result = subprocess.run(command, input=code, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"NAS 脚本失败：{result.stderr.strip()[-400:]}")
    return result.stdout


def nas_logs(since_epoch: float) -> str:
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since_epoch)) + "Z"
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        SSH_HOST,
        f"{DOCKER} logs --since {since} {CONTAINER} 2>&1",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    return result.stdout


_CORPUS_QUERY = r"""
import json, sqlite3
c = sqlite3.connect("file:/app/data/movieclaw.db?mode=ro", uri=True)
rows = c.execute('''
  select f.id, f.media_item_id, f.season_number, f.episode_number, f.container,
         f.resolution, f.video_codec, coalesce(f.hdr, ''), f.bit_depth, f.bit_rate,
         f.duration_seconds, f.frame_rate, f.file_path,
         f.size_bytes, length(coalesce(f.subtitle_streams, ''))
  from library_file f join library l on l.id = f.library_id
  -- 只取电影 / 剧集库：实验会在活动页、播放记录里留下片名
  where l.kind in ('movie', 'tv')
    and f.video_codec is not null and f.media_item_id is not null and f.duration_seconds >= 1200
''').fetchall()
print(json.dumps(rows, ensure_ascii=False))
"""

_PURGE_CACHE = r"""
import json, os, shutil, sys
root = "/app/data/transcodes"
ids = {int(x) for x in sys.argv[1].split(",") if x}
removed = 0
for name in os.listdir(root):
    directory = os.path.join(root, name)
    try:
        with open(os.path.join(directory, "manifest.json")) as handle:
            components = json.load(handle).get("components", {})
    except (OSError, ValueError):
        continue
    if components.get("file_id") in ids:
        shutil.rmtree(directory, ignore_errors=True)
        removed += 1
print(removed)
"""


def purge_cache(file_ids: list[int]) -> int:
    return int(nas_python(_PURGE_CACHE, ",".join(map(str, file_ids))).strip() or 0)


_EVICT_SOURCE = r"""
import os, sqlite3, sys
ids = [int(x) for x in sys.argv[1].split(",") if x]
db = sqlite3.connect("file:/app/data/movieclaw.db?mode=ro", uri=True)
marks = ",".join("?" * len(ids))
files = 0
for (path,) in db.execute(f"select file_path from library_file where id in ({marks})", ids):
    if os.path.isfile(path):
        targets = [path]
    else:
        targets = [os.path.join(d, f) for d, _, names in os.walk(path) for f in names]
    for target in targets:
        try:
            fd = os.open(target, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            files += 1
        finally:
            os.close(fd)
print(files)
"""


def evict_source(file_ids: list[int]) -> int:
    """把片源赶出 NAS 的页缓存（原盘整棵目录），每次都从冷的起转。

    不赶的话同一部片两组对照里先跑的那组读冷盘、后跑的读页缓存（实测文件尾的小读
    首字节 20～30 ms 对 2 ms），配对差里混进了先后顺序。片源若在 NFS 上，赶的是 NAS
    这一层 NFS 客户端的缓存，存储端自己的缓存管不到——对两组一样。"""
    return int(nas_python(_EVICT_SOURCE, ",".join(map(str, file_ids))).strip() or 0)


# ---------------------------------------------------------------------------
# 语料
# ---------------------------------------------------------------------------

#: (类别, 判据, 每类取几部)。按片库里实际多的格式排，覆盖转码链路的几条分支：
#: GPU 全链路（缩放 + 色调映射）、长 GOP 高码率、尾部 moov 的 MP4、字幕轨多的 MKV、原盘
CATEGORIES: list[tuple[str, Any]] = [
    (
        "4k-hdr10-mkv",
        lambda r: (
            r["container"] == "mkv"
            and r["resolution"] == "2160p"
            and r["codec"] == "hevc"
            and r["hdr"] == "HDR10"
        ),
    ),
    (
        "4k-dv-mkv",
        lambda r: (
            r["container"] == "mkv" and r["resolution"] == "2160p" and r["hdr"] == "Dolby Vision"
        ),
    ),
    (
        "4k-dv-mp4",
        lambda r: (
            r["container"] == "mp4" and r["resolution"] == "2160p" and r["hdr"] == "Dolby Vision"
        ),
    ),
    (
        "4k-sdr-mp4",
        lambda r: (
            r["container"] == "mp4"
            and r["resolution"] == "2160p"
            and r["codec"] == "hevc"
            and not r["hdr"]
        ),
    ),
    (
        "4k-h264-mkv",
        lambda r: r["container"] == "mkv" and r["resolution"] == "2160p" and r["codec"] == "h264",
    ),
    (
        "1080-h264-mkv",
        lambda r: r["container"] == "mkv" and r["resolution"] == "1080p" and r["codec"] == "h264",
    ),
    (
        "1080-h264-mp4",
        lambda r: r["container"] == "mp4" and r["resolution"] == "1080p" and r["codec"] == "h264",
    ),
    (
        "1080-hevc10-mkv",
        lambda r: (
            r["container"] == "mkv"
            and r["resolution"] == "1080p"
            and r["codec"] == "hevc"
            and r["bit_depth"] == 10
        ),
    ),
    ("uhd-bluray", lambda r: r["container"] == "bluray" and r["resolution"] == "2160p"),
    ("hlg-ts", lambda r: r["container"] == "ts" and r["hdr"] == "HLG"),
]


def build_corpus(args: argparse.Namespace) -> None:
    raw = json.loads(nas_python(_CORPUS_QUERY))
    keys = [
        "file_id",
        "media_item_id",
        "season",
        "episode",
        "container",
        "resolution",
        "codec",
        "hdr",
        "bit_depth",
        "bit_rate",
        "duration",
        "fps",
        "path",
        "size",
        "subtitle_json_len",
    ]
    rows = [dict(zip(keys, row, strict=True)) for row in raw]
    rng = random.Random(args.seed)
    picked: list[dict] = []
    for category, match, *_ in CATEGORIES:
        candidates = [r for r in rows if match(r)]
        rng.shuffle(candidates)
        seen_items: set[int] = set()
        chosen = []
        for row in candidates:
            if row["media_item_id"] in seen_items:
                continue
            seen_items.add(row["media_item_id"])
            chosen.append(row)
            if len(chosen) >= args.per_category:
                break
        for index, row in enumerate(chosen):
            height = 1080 if row["resolution"] == "2160p" else 720
            picked.append(
                {
                    **{k: row[k] for k in keys if k != "path"},
                    "name": Path(row["path"]).name,
                    "category": category,
                    "max_height": height,
                    "quick": index == 0,
                }
            )
        log(f"{category}: 候选 {len(candidates)} 部，取 {len(chosen)} 部")
    CORPUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CORPUS_PATH.write_text(json.dumps(picked, ensure_ascii=False, indent=1))
    log(f"语料 {len(picked)} 部 → {CORPUS_PATH}")


def load_corpus(selection: str) -> list[dict]:
    corpus = json.loads(CORPUS_PATH.read_text())
    if selection == "all":
        return corpus
    if selection == "quick":
        return [entry for entry in corpus if entry.get("quick")]
    wanted = set(selection.split(","))
    return [e for e in corpus if str(e["file_id"]) in wanted or e["category"] in wanted]


# ---------------------------------------------------------------------------
# 记录代理：客户端的所有请求经它转发到 NAS（或慢线路代理），逐请求记时刻
# ---------------------------------------------------------------------------


class LabProxy:
    """HTTP/1.1 中继：一条客户端连接对一条上游连接，按报文边界记每个请求的
    发出、首字节、收完三个时刻（time.monotonic_ns，与探针同一时钟）。

    逐块转发、不攒整段——分块传输（chunked）的响应按块原样送出，边产出边送的
    分片（若服务端支持）在客户端那头看到的也是边到边送。"""

    def __init__(self, port: int, upstream: tuple[str, int]) -> None:
        self.port = port
        self.upstream = upstream
        self.lock = threading.Lock()
        self.records: list[dict] = []
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("记录代理没起来")

    def drain(self) -> list[dict]:
        with self.lock:
            records, self.records = self.records, []
        return records

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)

        async def main() -> None:
            server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
            self.ready.set()
            async with server:
                await server.serve_forever()

        self.loop.run_until_complete(main())

    def _record(
        self,
        method: str,
        target: str,
        status: int,
        size: int,
        t_req: int,
        t_first: int,
        t_done: int,
        aborted: bool = False,
    ) -> None:
        with self.lock:
            self.records.append(
                {
                    "method": method,
                    "path": target.split("?", 1)[0],
                    "status": status,
                    "bytes": size,
                    "t_req": t_req,
                    "t_first": t_first,
                    "t_done": t_done,
                    "aborted": aborted,
                }
            )

    async def _handle(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        upstream_reader = upstream_writer = None
        prefix = b""

        def gone(task: asyncio.Future) -> bool:
            """客户端是否已断开（读到 EOF / 出错）；读到的是字节则是下一个请求提前到了。"""
            if not task.done() or task.cancelled():
                return False
            return task.exception() is not None or task.result() == b""

        try:
            while True:
                try:
                    head = prefix + await client_reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                prefix = b""
                t_req = time.monotonic_ns()
                request_line, headers = _parse_head(head)
                method, target = (request_line.split(" ") + ["", ""])[:2]
                body = b""
                if int(headers.get("content-length", "0") or 0):
                    body = await client_reader.readexactly(int(headers["content-length"]))
                if upstream_writer is None:
                    upstream_reader, upstream_writer = await asyncio.open_connection(*self.upstream)
                upstream_writer.write(head + body)
                await upstream_writer.drain()
                # 等响应期间盯着客户端：AVPlayer 跳转时会掐掉在途请求，这类「没等到就走了」
                # 的请求也要记下来（等了多久、要的是哪一段），否则时间线上会凭空缺一块
                eof_task = asyncio.ensure_future(client_reader.read(1))
                eof_task.add_done_callback(_swallow)
                head_task = asyncio.ensure_future(upstream_reader.readuntil(b"\r\n\r\n"))
                head_task.add_done_callback(_swallow)
                await asyncio.wait({head_task, eof_task}, return_when=asyncio.FIRST_COMPLETED)
                if not head_task.done() and gone(eof_task):
                    head_task.cancel()
                    self._record(method, target, 0, 0, t_req, 0, time.monotonic_ns(), aborted=True)
                    return
                response_head = await head_task
                t_first = time.monotonic_ns()
                status_line, response_headers = _parse_head(response_head)
                status = int(status_line.split(" ")[1]) if " " in status_line else 0
                client_writer.write(response_head)
                size = 0
                close_after = response_headers.get("connection", "").lower() == "close"
                if method == "HEAD" or status in (204, 304) or 100 <= status < 200:
                    pass
                elif "chunked" in response_headers.get("transfer-encoding", "").lower():
                    while True:
                        line = await upstream_reader.readuntil(b"\r\n")
                        length = int(line.split(b";")[0].strip(), 16)
                        client_writer.write(line)
                        if length == 0:
                            while True:
                                trailer = await upstream_reader.readuntil(b"\r\n")
                                client_writer.write(trailer)
                                if trailer == b"\r\n":
                                    break
                            break
                        chunk = await upstream_reader.readexactly(length + 2)
                        size += length
                        client_writer.write(chunk)
                        await client_writer.drain()
                        if gone(eof_task):
                            break
                elif "content-length" in response_headers:
                    remaining = int(response_headers["content-length"])
                    while remaining:
                        chunk = await upstream_reader.read(min(remaining, 256 * 1024))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        size += len(chunk)
                        client_writer.write(chunk)
                        await client_writer.drain()
                        if gone(eof_task):
                            break
                else:
                    while chunk := await upstream_reader.read(256 * 1024):
                        size += len(chunk)
                        client_writer.write(chunk)
                        await client_writer.drain()
                    close_after = True
                if gone(eof_task):
                    # 收到一半客户端走了：记成中途放弃
                    self._record(
                        method,
                        target,
                        status,
                        size,
                        t_req,
                        t_first,
                        time.monotonic_ns(),
                        aborted=True,
                    )
                    return
                if eof_task.done() and not eof_task.cancelled():
                    prefix = eof_task.result()  # 下一个请求的头一个字节已经被读走了
                else:
                    eof_task.cancel()
                    # 等它真的退出：StreamReader 同一时刻只许一个等待者，下一轮 readuntil 才不报错
                    await asyncio.gather(eof_task, return_exceptions=True)
                await client_writer.drain()
                self._record(method, target, status, size, t_req, t_first, time.monotonic_ns())
                if close_after:
                    return
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            return
        finally:
            for writer in (client_writer, upstream_writer):
                if writer is not None:
                    with contextlib.suppress(Exception):
                        writer.close()


def _swallow(task: asyncio.Future) -> None:
    """取走后台任务的异常（连接被对端重置之类），别让事件循环刷「never retrieved」。"""
    if not task.cancelled():
        task.exception()


def _parse_head(head: bytes) -> tuple[str, dict[str, str]]:  # noqa: D401
    lines = head.decode("latin-1").split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    return lines[0], headers


# ---------------------------------------------------------------------------
# 探针
# ---------------------------------------------------------------------------


class Probe:
    def __init__(self, binary: Path) -> None:
        self.process = subprocess.Popen(
            [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1
        )
        self.lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.expect("hello", 10)

    def _pump(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line)

    def send(self, command: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(command) + "\n")
        self.process.stdin.flush()

    def expect(self, event: str, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"探针 {timeout:.0f} 秒没等到 {event}")
            line = self.lines.get(timeout=remaining)
            payload = json.loads(line)
            if payload.get("ev") == event:
                return payload

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.send({"cmd": "quit"})
        with contextlib.suppress(Exception):
            self.process.wait(3)


def build_probe() -> Path:
    binary = OUT_ROOT / "bin/transcode_probe"
    if not binary.exists() or binary.stat().st_mtime < PROBE_SRC.stat().st_mtime:
        binary.parent.mkdir(parents=True, exist_ok=True)
        log("编译探针 …")
        subprocess.run(
            ["swiftc", "-O", str(PROBE_SRC), "-o", str(binary)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return binary


# ---------------------------------------------------------------------------
# 一次播放
# ---------------------------------------------------------------------------


def worker_status(api: Api) -> dict[str, dict]:
    data = api.call("GET", "/api/v1/transcode-worker/status").get("data") or {}
    return {w["worker_id"]: w for w in data.get("workers", [])}


def wait_idle(api: Api, expect: str, limit: float = 1800) -> float:
    """等目标 Worker 在线且空闲。返回等了多少秒。"""
    started = time.monotonic()
    announced = False
    while True:
        workers = worker_status(api)
        target = workers.get(expect)
        if target and target["online"] and not target["draining"] and target["active_jobs"] == 0:
            return time.monotonic() - started
        if not announced:
            state = "不在线" if not target else f"忙（{target['active_jobs']} 个任务）"
            log(f"  等 Worker {expect} 空闲：现在{state}")
            announced = True
        if time.monotonic() - started > limit:
            raise TimeoutError(f"Worker {expect} {limit:.0f} 秒内一直不空闲")
        time.sleep(2)


def wait_quiet(max_load: float, limit: float = 1800) -> float:
    """等本机安静下来。返回等了多少秒。

    探针（AVPlayer）总在本机跑，本机当 Worker 时 ffmpeg 也在本机：别的会话这时候编译或打包
    （实测 5 分钟负载 14）会让 Worker 起 ffmpeg、ffmpeg 发第一个请求、播放器要分片全都慢
    几百毫秒，两组对照里撞上的那一组就成了噪声。1 分钟负载有一分钟左右的滞后，宁可多等。"""
    started = time.monotonic()
    announced = False
    while os.getloadavg()[0] > max_load:
        if not announced:
            log(f"  本机负载 {os.getloadavg()[0]:.1f}（上限 {max_load}），等别的任务跑完")
            announced = True
        if time.monotonic() - started > limit:
            raise TimeoutError(f"本机负载 {limit:.0f} 秒内一直高于 {max_load}")
        time.sleep(5)
    return time.monotonic() - started


def scenario_plan(entry: dict, scenario: str) -> tuple[int, list[dict]]:
    """场景 → (起播毫秒, 跳转脚本)。位置取整秒，同一部片每轮相同，便于配对。"""
    duration = int(entry["duration"])
    if scenario == "head":
        # 从头播；6 秒后跳到 +12 秒（已转出区间内），再看 4 秒
        return 0, [{"label": "in", "after": 6, "to": 18.0}]
    # resume：续播点在 37% 处（真实使用七成是续播）；看 6 秒后往后跳 10 分钟（缓冲外），
    # 再往回跳到续播点前 4 分钟（转码头后面，也要重启）
    position = max(300, min(int(duration * 0.37), duration - 900))
    forward = min(position + 600, duration - 120)
    return position * 1000, [
        {"label": "fwd", "after": 6, "to": float(forward)},
        {"label": "back", "after": 5, "to": float(position - 240)},
    ]


def run_one(
    api: Api,
    probe: Probe,
    proxy: LabProxy,
    entry: dict,
    scenario: str,
    args: argparse.Namespace,
    base: str,
    round_index: int,
) -> dict:
    waited = wait_idle(api, args.expect_worker)
    if args.max_load > 0:
        waited += wait_quiet(args.max_load)
    load_before = round(os.getloadavg()[0], 2)
    purged = purge_cache([entry["file_id"]]) if not args.keep_cache else 0
    if args.cold:
        evict_source([entry["file_id"]])
    start_ms, seeks = scenario_plan(entry, scenario)
    body: dict[str, Any] = {
        "file_id": entry["file_id"],
        "media_item_id": entry["media_item_id"],
        "season_number": entry["season"],
        "episode_number": entry["episode"],
        "capability": AVPLAYER_CAPABILITY,
        "max_height": entry["max_height"],
        "start_ms": start_ms,
        "subtitle_track": "off",
        "device_id": DEVICE_ID,
    }
    if args.link != "lan":
        # 慢线路：App 弹「换低画质」之前已经量到了线路速度，开会话时带上
        body["downlink_bps"] = int(LINKS[args.link]["down_mbps"] * 1e6)
    run_id = uuid.uuid4().hex[:10]
    probe.send({"cmd": "warm", "base": base})
    probe.expect("warm", 30)
    proxy.drain()
    probe.send(
        {
            "cmd": "run",
            "id": run_id,
            "base": base,
            "cookie": api.cookie_header(),
            "body": body,
            "mode": args.mode,
            "seeks": seeks,
            "tail": 4,
            "timeout": args.timeout,
        }
    )
    session = probe.expect("session", 60)
    session_id = session.get("session_id")
    tier, backend = session.get("tier"), session.get("hw_backend")
    valid = session.get("error") is None and tier == 3 and backend == "videotoolbox"
    diagnostics: dict = {}
    if session_id and not valid:
        # 落到了别的执行方式（多半是 Worker 被别人占了）：立即停，别让 NAS 软件转码空烧
        api.call("DELETE", f"/api/v1/playback/sessions/{session_id}")
    done = probe.expect("done", args.timeout * 4 + 120)
    if session_id and valid:
        token = (session.get("master_url") or "").partition("token=")[2]
        diagnostics = (
            api.call(
                "GET", f"/api/v1/playback/sessions/{session_id}/diagnostics?token={token}"
            ).get("data")
            or {}
        )
        api.call("DELETE", f"/api/v1/playback/sessions/{session_id}")
        if diagnostics.get("worker_id") != args.expect_worker:
            valid = False
    requests = proxy.drain()
    record = {
        "run_id": run_id,
        "tag": args.tag,
        "round": round_index,
        "scenario": scenario,
        "mode": args.mode,
        "link": args.link,
        "file_id": entry["file_id"],
        "category": entry["category"],
        "name": entry["name"],
        "start_ms": start_ms,
        "waited_idle_s": round(waited, 1),
        # 本机 1 分钟负载（起播前、收尾后）：事后挑出被别的任务干扰的那几次
        "load": [load_before, round(os.getloadavg()[0], 2)],
        "purged_dirs": purged,
        "valid": valid,
        "session": session,
        "result": done.get("result", {}),
        "diagnostics": {
            k: diagnostics.get(k)
            for k in (
                "worker_id",
                "worker_version",
                "ffmpeg_version",
                "encoder",
                "cache_hit",
                "cached_segments",
                "job_speed",
                "job_state",
                "highest_produced_segment",
                "recent_uploads",
                "processing_mode",
                "timeline",
            )
        },
        "requests": requests,
    }
    record["metrics"] = compute_metrics(record)
    return record


def _ms(value: int | None, origin: int) -> float | None:
    if not value or not origin:
        return None
    return round((value - origin) / 1e6, 1)


def compute_metrics(record: dict) -> dict:
    """一次播放的核心指标（毫秒），口径见 transcode-latency.md §1。"""
    session = record["session"]
    result = record["result"] or {}
    t0 = session.get("t_sent") or 0
    metrics: dict[str, Any] = {"session_ms": _ms(session.get("t_resp"), t0)}
    timing = session.get("server_timing") or ""
    for name, value in re.findall(r"(\w+);dur=(\d+)", timing):
        metrics[f"server_{name}_ms"] = int(value)
    start = result.get("start") or {}
    frames = [v for v in (start.get("layer_ready"), start.get("first_pixel")) if v]
    if frames:
        metrics["first_frame_ms"] = _ms(min(frames), t0)
    if record["mode"] == "segments" and result.get("first_segment_done"):
        metrics["first_frame_ms"] = _ms(result["first_segment_done"], t0)
    metrics["playing_ms"] = _ms(start.get("first_playing"), t0)
    for seek in result.get("seeks") or []:
        label = seek.get("label", "seek")
        metrics[f"seek_{label}_ms"] = _ms(seek.get("t_frame"), seek.get("t_start"))
    stalls = result.get("stalls") or []
    metrics["stall_count"] = len(stalls)
    metrics["stall_ms"] = round(sum((s["end"] - s["start"]) / 1e6 for s in stalls), 1)
    # 客户端视角的分段：起播那一段分片何时要、等了多久首字节、何时收完
    requests = sorted(record["requests"], key=lambda r: r["t_req"])
    segment_requests = [
        r for r in requests if re.search(r"/seg\d+\.(m4s|ts)$", r["path"]) and not r.get("aborted")
    ]
    if segment_requests:
        first = segment_requests[0]
        metrics["first_seg_req_ms"] = _ms(first["t_req"], t0)
        metrics["first_seg_ttfb_ms"] = round((first["t_first"] - first["t_req"]) / 1e6, 1)
        metrics["first_seg_done_ms"] = _ms(first["t_done"], t0)
        metrics["first_seg_bytes"] = first["bytes"]
    init = next((r for r in requests if r["path"].endswith("/init.mp4")), None)
    if init:
        metrics["init_ttfb_ms"] = round((init["t_first"] - init["t_req"]) / 1e6, 1)
    metrics.update(server_breakdown(record["diagnostics"].get("timeline") or []))
    return metrics


def server_breakdown(timeline: list[dict]) -> dict[str, Any]:
    """服务端时间线里起播那一轮（第一次 dispatch 到第一次交付）的各段，毫秒距会话创建。"""
    first: dict[str, int] = {}
    for entry in sorted(timeline, key=lambda e: e["t"]):
        name = entry["ev"]
        if name in ("landed", "put", "w_recv", "w_up", "w_seg_open") and entry.get(
            "name", ""
        ).startswith("seg"):
            name = f"{name}_seg"
        first.setdefault(name, entry["t"])
    keys = {
        "dispatch": "t_dispatch",
        "accepted": "t_accepted",
        "src": "t_src",
        "src_first": "t_src_first",
        "w_ffmpeg": "t_w_ffmpeg",
        "w_input": "t_w_input",
        "w_init_open": "t_w_init",
        "w_seg_open_seg": "t_w_seg",
        "put_seg": "t_put_seg",
        "landed_seg": "t_landed_seg",
        "served": "t_served",
    }
    return {label: first[event] for event, label in keys.items() if event in first}


_BLOCKER_QUERY = r"""
import json, sqlite3, sys
excluded = {int(x) for x in sys.argv[1].split(",") if x}
c = sqlite3.connect("file:/app/data/movieclaw.db?mode=ro", uri=True)
rows = c.execute('''
  select f.id, f.media_item_id, f.season_number, f.episode_number, f.duration_seconds
  from library_file f join library l on l.id = f.library_id
  where l.kind in ('movie', 'tv') and f.resolution = '1080p' and f.video_codec = 'h264'
    and f.container = 'mkv' and f.duration_seconds >= 2400 and f.media_item_id is not null
  order by f.id limit 50
''').fetchall()
print(json.dumps([r for r in rows if r[0] not in excluded][0]))
"""


class WorkerBlocker:
    """占住某台 Worker 的唯一并发槽：让本批实验的任务全部落到另一台（开发版）Worker 上。

    做法是开一个转码会话、让它落在要占的那台上，之后只发心跳不取分片——领先 120 秒后
    转码被节流挂起，几乎不耗资源，但槽位一直占着。实验结束即停。挑的是语料之外的片子：
    清缓存只清语料的转码目录，不会误删它。"""

    def __init__(self, api: Api, worker_id: str, excluded: list[int]) -> None:
        self.api = api
        self.worker_id = worker_id
        row = json.loads(nas_python(_BLOCKER_QUERY, ",".join(map(str, excluded))))
        self.file = {
            "file_id": row[0],
            "media_item_id": row[1],
            "season": row[2],
            "episode": row[3],
        }
        self.session_id: str | None = None
        self.stop_event = threading.Event()

    def start(self) -> None:
        for _ in range(3):
            wait_idle(self.api, self.worker_id)
            # 上一次占位转出的分片还在缓存里的话，会话直接读文件、根本不派任务，占不住
            purge_cache([self.file["file_id"]])
            response = self.api.call(
                "POST",
                "/api/v1/playback/sessions",
                {
                    "file_id": self.file["file_id"],
                    "media_item_id": self.file["media_item_id"],
                    "season_number": self.file["season"],
                    "episode_number": self.file["episode"],
                    "capability": AVPLAYER_CAPABILITY,
                    "max_height": 720,
                    "start_ms": 60_000,
                    "subtitle_track": "off",
                    "device_id": f"{DEVICE_ID}-blocker",
                },
            )
            data = response.get("data") or {}
            self.session_id = data.get("session_id")
            token = (data.get("master_url") or "").partition("token=")[2]
            diagnostics = (
                self.api.call(
                    "GET", f"/api/v1/playback/sessions/{self.session_id}/diagnostics?token={token}"
                ).get("data")
                or {}
            )
            if diagnostics.get("worker_id") == self.worker_id:
                log(f"已占住 Worker {self.worker_id}（会话 {self.session_id}）")
                threading.Thread(target=self._keepalive, daemon=True).start()
                return
            self.stop()
            time.sleep(3)
        raise RuntimeError(f"没能占住 Worker {self.worker_id}")

    def _keepalive(self) -> None:
        while not self.stop_event.wait(30):
            self.api.call("POST", f"/api/v1/playback/sessions/{self.session_id}/ping")

    def stop(self) -> None:
        self.stop_event.set()
        if self.session_id:
            self.api.call("DELETE", f"/api/v1/playback/sessions/{self.session_id}")
            self.session_id = None


class LocalWorker:
    """本机的开发版 Worker（无界面模式）：对照实验时按组切换启动参数（``--lab-flags``）。

    两组在同一台 Mac、同一个 ffmpeg 上交替跑，差别只在开关——绝对数字与生产 Worker
    （Mac mini）不同，但两组之间的差值成立（docs/design/transcode-latency.md §3）。"""

    def __init__(self, args: argparse.Namespace, api: Api) -> None:
        self.args = args
        self.api = api
        self.process: subprocess.Popen | None = None
        self.flags: str | None = None

    def ensure(self, flags: str) -> None:
        if self.process is not None and self.process.poll() is None and self.flags == flags:
            return
        self.stop()
        token = Path(self.args.worker_token_file).read_text().strip()
        command = [
            self.args.worker_bin,
            "--headless",
            "--nas-url",
            SERVER,
            "--token",
            token,
            "--worker-id",
            self.args.expect_worker,
            "--ffmpeg",
            self.args.worker_ffmpeg,
            "--max-jobs",
            "1",
        ]
        if flags:
            command += ["--lab-flags", flags]
        self.process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self.flags = flags
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(0.5)
            worker = worker_status(self.api).get(self.args.expect_worker)
            # 刚重连的连接 last_seen 很小；旧连接断开前可能还挂着「在线」
            if worker and worker["online"] and worker["last_seen_seconds"] < 2:
                time.sleep(0.5)
                return
        raise RuntimeError("本机 Worker 30 秒内没有连上 NAS")

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(5)
        self.process = None


def parse_arms(text: str) -> list[tuple[str, str]]:
    """「名字=开关,名字=开关」→ [(名字, 开关)]；开关为空即默认行为（多个开关用 + 连）。"""
    arms = []
    for item in text.split(","):
        name, _, flags = item.partition("=")
        if name:
            arms.append((name.strip(), flags.strip().replace("+", ",")))
    return arms


def run_batch(args: argparse.Namespace) -> None:
    api = login_from_env()
    entries = load_corpus(args.files)
    if not entries:
        sys.exit("语料为空：先跑 corpus，或检查 --files")
    stamp = time.strftime("%m%d-%H%M%S")
    out = OUT_ROOT / f"{args.tag}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    health = api.call("GET", "/api/v1/health")
    workers = worker_status(api)
    meta = {
        "tag": args.tag,
        "started": time.time(),
        "server": SERVER,
        "link": args.link,
        "mode": args.mode,
        "scenarios": args.scenarios,
        "rounds": args.rounds,
        "expect_worker": args.expect_worker,
        "spec_hash": health.get("spec_hash"),
        "worker": workers.get(args.expect_worker),
        "files": [e["file_id"] for e in entries],
        "note": args.note,
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    upstream_host, upstream_port = (
        SERVER.split("//", 1)[1].split(":")[0],
        int(SERVER.rsplit(":", 1)[1]),
    )
    netem = None
    if args.link != "lan":
        link = LINKS[args.link]
        netem_port = args.proxy_port + 1
        netem = subprocess.Popen(
            [
                sys.executable,
                str(NETEM),
                "--listen",
                f"127.0.0.1:{netem_port}",
                "--upstream",
                f"{upstream_host}:{upstream_port}",
                "--rtt-ms",
                str(link["rtt_ms"]),
                "--down-mbps",
                str(link["down_mbps"]),
                "--up-mbps",
                str(link["up_mbps"]),
            ],
            stdout=(out / "netem.log").open("w"),
            stderr=subprocess.STDOUT,
        )
        time.sleep(1)
        upstream = ("127.0.0.1", netem_port)
    else:
        upstream = (upstream_host, upstream_port)
    proxy = LabProxy(args.proxy_port, upstream)
    proxy.start()
    base = f"http://127.0.0.1:{args.proxy_port}"
    probe = Probe(build_probe())
    arms = parse_arms(args.arms) if args.arms else [(args.tag, "")]
    local_worker = LocalWorker(args, api) if args.worker_bin else None
    blocker = None
    if args.block_worker:
        blocker = WorkerBlocker(api, args.block_worker, [e["file_id"] for e in load_corpus("all")])
        blocker.start()
    scenarios = args.scenarios.split(",")
    results_path = out / "results.jsonl"
    log(
        f"批次 {out.name}：{len(entries)} 部 × {len(scenarios)} 场景 × {args.rounds} 轮，"
        f"线路 {args.link}"
    )
    session_ids: list[str] = []
    try:
        for round_index in range(args.rounds):
            order = list(entries)
            random.Random(f"{args.seed}-{round_index}").shuffle(order)
            for entry_index, entry in enumerate(order):
                # 每部片各组都跑一遍，先后轮换（AB / BA），抵消 NAS 页缓存的冷热差
                turn = arms if (round_index + entry_index) % 2 == 0 else arms[::-1]
                for (arm, flags), scenario in [(a, sc) for a in turn for sc in scenarios]:
                    if local_worker is not None:
                        local_worker.ensure(flags)
                    for _attempt in range(3):
                        try:
                            record = run_one(
                                api, probe, proxy, entry, scenario, args, base, round_index
                            )
                            record["arm"] = arm
                        except (TimeoutError, queue.Empty) as error:
                            log(f"  {entry['name'][:40]} {scenario}: 超时（{error}），重启探针")
                            probe.close()
                            probe = Probe(build_probe())
                            continue
                        if record["session"].get("session_id"):
                            session_ids.append(record["session"]["session_id"])
                        with results_path.open("a") as handle:
                            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                        print_line(record)
                        if record["valid"]:
                            break
                        log("  本次无效（不是档 3 + 目标 Worker），重来")
                        time.sleep(5)
                    time.sleep(args.gap)
    finally:
        probe.close()
        if blocker:
            blocker.stop()
        if local_worker is not None:
            local_worker.stop()
        if netem:
            netem.terminate()
        logs = nas_logs(meta["started"] - 5)
        wanted = [
            line
            for line in logs.splitlines()
            if any(sid in line for sid in session_ids) or "远程" in line or "Worker" in line
        ]
        (out / "nas.log").write_text("\n".join(wanted))
        log(f"结果 → {out}")


def print_line(record: dict) -> None:
    m = record["metrics"]
    parts = [
        f"r{record['round']}",
        record.get("arm", ""),
        record["scenario"],
        f"{record['category']:<15}",
        record["name"][:28],
    ]
    for key in ("first_frame_ms", "seek_fwd_ms", "seek_back_ms", "seek_in_ms"):
        if m.get(key) is not None:
            parts.append(f"{key.replace('_ms', '')}={m[key]:.0f}")
    if m.get("first_seg_ttfb_ms") is not None:
        parts.append(f"seg_ttfb={m['first_seg_ttfb_ms']:.0f}")
    if m.get("stall_count"):
        parts.append(f"stall={m['stall_count']}/{m['stall_ms']:.0f}ms")
    if not record["valid"]:
        parts.append(
            f"无效(tier={record['session'].get('tier')} "
            f"worker={record['diagnostics'].get('worker_id')})"
        )
    log("  " + " ".join(parts))


# ---------------------------------------------------------------------------
# 汇总与对照
# ---------------------------------------------------------------------------

REPORT_METRICS = [
    ("first_frame_ms", "起播首帧"),
    ("seek_fwd_ms", "前跳出画"),
    ("seek_back_ms", "回跳出画"),
    ("seek_in_ms", "缓冲内跳"),
    ("session_ms", "开会话"),
    ("first_seg_ttfb_ms", "首片等待"),
    ("first_seg_done_ms", "首片收完"),
]


def load_results(directory: Path) -> list[dict]:
    path = directory / "results.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r["valid"]]


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def summarize(rows: list[dict]) -> dict[str, dict[str, float]]:
    table: dict[str, dict[str, float]] = {}
    for key, _ in REPORT_METRICS:
        for scenario in sorted({r["scenario"] for r in rows}):
            values = [
                r["metrics"][key]
                for r in rows
                if r["scenario"] == scenario and r["metrics"].get(key) is not None
            ]
            if values:
                table[f"{scenario}.{key}"] = {
                    "n": len(values),
                    "p50": statistics.median(values),
                    "p90": pct(values, 0.9),
                    "max": max(values),
                }
    return table


def report(args: argparse.Namespace) -> None:
    directories = [Path(d) for d in args.dirs]
    batches = [(d.name, load_results(d)) for d in directories]
    if len(batches) == 1:
        # 一批里有多个对照组：按组拆开，当成几批来对照（顺序按首次出现）
        name, rows = batches[0]
        arms: list[str] = []
        for row in rows:
            if row.get("arm", row["tag"]) not in arms:
                arms.append(row.get("arm", row["tag"]))
        if len(arms) > 1:
            batches = [
                (f"{name}:{arm}", [r for r in rows if r.get("arm", r["tag"]) == arm])
                for arm in arms
            ]
    for name, rows in batches:
        print(f"\n== {name}（有效 {len(rows)} 次）")
        for key, value in summarize(rows).items():
            label = dict(REPORT_METRICS)[key.split(".", 1)[1]]
            print(
                f"  {key.split('.')[0]:<7}{label:<6} n={value['n']:<3} p50={value['p50']:>7.0f}  "
                f"p90={value['p90']:>7.0f}  max={value['max']:>7.0f}"
            )
        if args.by_category:
            for category in sorted({r["category"] for r in rows}):
                subset = [r for r in rows if r["category"] == category]
                cells = []
                for key in ("first_frame_ms", "seek_fwd_ms", "seek_back_ms"):
                    values = [
                        r["metrics"][key] for r in subset if r["metrics"].get(key) is not None
                    ]
                    if values:
                        cells.append(
                            f"{'first' if key.startswith('first') else key[5:-3]}="
                            f"{statistics.median(values):.0f}"
                        )
                print(f"    {category:<16} " + " ".join(cells))
    if len(batches) >= 2:
        (name_a, rows_a), (name_b, rows_b) = batches[0], batches[1]
        print(f"\n== 对照 {name_a} → {name_b}（按 文件×场景×轮 配对）")
        index_a = {(r["file_id"], r["scenario"], r["round"]): r for r in rows_a}
        for key, label in REPORT_METRICS[:4]:
            diffs = []
            for row in rows_b:
                other = index_a.get((row["file_id"], row["scenario"], row["round"]))
                if (
                    other
                    and row["metrics"].get(key) is not None
                    and other["metrics"].get(key) is not None
                ):
                    diffs.append(row["metrics"][key] - other["metrics"][key])
            if diffs:
                faster = sum(1 for d in diffs if d < 0)
                print(
                    f"  {label:<6} 配对 {len(diffs):<3} "
                    f"差值中位 {statistics.median(diffs):+7.0f} 毫秒  "
                    f"变快 {faster}/{len(diffs)}"
                )


# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    corpus = sub.add_parser("corpus", help="从 NAS 片库挑语料")
    corpus.add_argument("--per-category", type=int, default=2)
    corpus.add_argument("--seed", type=int, default=20261001)
    run = sub.add_parser("run", help="跑一批")
    run.add_argument("--tag", required=True, help="这一批的组名（对照时区分各组）")
    run.add_argument(
        "--expect-worker", required=True, help="应当接单的 Worker ID；不是它接的单作废"
    )
    run.add_argument("--files", default="all", help="all / quick / 逗号分隔的 file_id 或类别")
    run.add_argument("--scenarios", default="resume,head")
    run.add_argument("--rounds", type=int, default=1)
    run.add_argument("--mode", choices=["avplayer", "segments"], default="avplayer")
    run.add_argument("--link", choices=sorted(LINKS), default="lan")
    run.add_argument("--timeout", type=float, default=30, help="等首帧 / 单次跳转出画的上限（秒）")
    run.add_argument("--gap", type=float, default=1.5, help="两次播放之间歇几秒")
    run.add_argument(
        "--max-load", type=float, default=6.0, help="本机 1 分钟负载高于此值时先等（0 = 不等）"
    )
    run.add_argument(
        "--cold",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="每次起播前把片源赶出 NAS 页缓存（默认开；--no-cold 量热缓存）",
    )
    run.add_argument(
        "--keep-cache", action="store_true", help="不清 NAS 上的转码缓存（量缓存命中）"
    )
    run.add_argument("--proxy-port", type=int, default=18700)
    run.add_argument("--seed", default="lab")
    run.add_argument("--note", default="")
    run.add_argument(
        "--block-worker", default="", help="实验期间占住这台 Worker，让任务落到另一台上"
    )
    run.add_argument(
        "--arms", default="", help="对照组：名字=开关,名字=开关（开关传给本机 Worker）"
    )
    run.add_argument("--worker-bin", default="", help="本机开发版 Worker 可执行文件（按组重启）")
    run.add_argument("--worker-token-file", default="")
    run.add_argument("--worker-ffmpeg", default="")
    rep = sub.add_parser("report", help="汇总 / 对照")
    rep.add_argument("dirs", nargs="+")
    rep.add_argument("--by-category", action="store_true")
    args = parser.parse_args()
    if args.command == "corpus":
        build_corpus(args)
    elif args.command == "run":
        run_batch(args)
    else:
        report(args)


if __name__ == "__main__":
    main()
