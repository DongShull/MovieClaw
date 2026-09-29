#!/usr/bin/env python3
"""离线假图床：TMDB 图片 CDN 的替身，兼作 iOS 性能实验室的「出网代理」。

为什么需要
----------
iOS 性能实验室（``ios_lab.sh``）要在**离线、可复现**的条件下测「订阅首页」与
「媒体库首页」的打开速度。订阅页的海报 / 剧照 / 片名 Logo 全部来自 TMDB 图床，
经后端 ``/api/v1/images/proxy`` 回源——真去打 image.tmdb.org 既不可复现（CDN 抖动、
国内还要翻墙），也不该在压测里出网。本脚本按 TMDB 的路径形态
（``/t/p/<档位>/<文件名>``）**现场生成**尺寸、体积都贴近真实的图片：

==========================  ==========================  ======================
类型 / 档位                 像素                        体积（实测，见 --build-pool）
==========================  ==========================  ======================
海报 w500                   500×750 JPEG                40~90 KB
剧照 w1280                  1280×720 JPEG               150~300 KB
剧照 original               3840×2160（约 1/3 为 1920×1080）  0.8~2 MB
分集剧照 w300 / w780         300×169 / 780×439 JPEG      10~20 KB / 60~110 KB
片名 Logo w500              约 500×(125~250) 透明 PNG   20~60 KB
==========================  ==========================  ======================

画面是「渐变 + 低频色块 + 中频纹理 + 细颗粒噪声 + 几何描边」，不是纯色——纯色图
JPEG 压出来只有几 KB，测不出真实的传输与解码成本。

怎么接进后端（不改 ``src/``）
-----------------------------
后端所有远程图片都经「出网层」（``movieclaw_net.egress``，服务标签 ``image``）回源。
实验室在「设置 → 网络」里配手动代理 ``http://127.0.0.1:18603``、走代理的服务为
``image`` 与 ``tmdb``，并用环境变量把 ``TMDB_IMAGE_BASE_URL`` 设为
**http**://image.tmdb.org/t/p：

- 图片请求以正向代理的绝对 URL 形态到达这里
  （``GET http://image.tmdb.org/t/p/w500/x.jpg HTTP/1.1``），不需要 CONNECT / TLS，
  本脚本直接生成图片回给后端；
- 配了出网代理后，后端图片代理的 SSRF 防护会跳过本地 DNS 校验（连接由代理发起，
  见 ``services/image_proxy.py``），域名不必真的解析；
- TMDB 接口（https://api.themoviedb.org）以 CONNECT 隧道到达，转给上游代理
  （``--upstream-proxy``，如本机 Surge / Clash 的 HTTP 代理；不给则直连）——发现页因此仍能拿到
  真数据。隧道只放行 ``--tunnel-host`` 白名单（默认只有 api.themoviedb.org）：
  image.tmdb.org 的 CONNECT 一律拒绝，保证图片永远不会绕过替身真的出网。

图池
----
同一 (类型, 档位) 只生成 K 张互不相同的底图（``--build-pool`` 预生成到磁盘，服务时
按需读进内存），文件名按哈希落到其中一张。后端与 App 都按 URL 缓存：不同 URL 仍是
各自的一次回源、一次解码——复用底图只省磁盘与生成时间，不会作弊掉网络与解码开销
（同 ``seed_poster_assets.py`` 的硬链接思路）。图池预生成后，回源响应是稳定的
「读内存 + 写 socket」，冷缓存测量不会被现场生成图片的 CPU 时间污染；需要模拟 CDN
的首字节延迟与带宽时用 ``--delay-ms`` / ``--rate-mbps``（默认都不加）。

假 qBittorrent（可选，``--qbt-listen``）
-------------------------------------
订阅首页 Hero 的「下载中 62% / 整理中」与任务中心（``/downloaders/tasks``）读的是下载器
的实时任务。同一进程可顺带起一个 qBittorrent WebUI API v2 的最小替身（登录、版本、
任务列表等几个端点），任务清单由 ``seed_subscriptions_dataset.py --qbt-json`` 按在途
工单的 infohash 生成；实验室把它登记成默认下载器 ``http://127.0.0.1:18604``。

文件名约定（决定生成哪一类图）
------------------------------
实验室的种子脚本给路径带上类型前缀：``/poster_<hex>.jpg``、``/backdrop_<hex>.jpg``、
``/still_<hex>.jpg``、``/logo_<hex>.png``；``seed_library_dataset.py`` 的老式路径
（``/p0001poster.jpg``、``/b0001back.jpg``、``/still12.jpg``）按关键字兼容。
``.png`` 一律按 Logo（透明底）生成，其余未知名字按竖版海报生成。

用法::

    # 预生成图池并打印各档体积（ios_lab.sh reset 会调用）
    python scripts/perf/fake_image_origin.py --pool-dir <lab>/origin-pool --build-pool
    # 启动（ios_lab.sh start 会调用）
    python scripts/perf/fake_image_origin.py --pool-dir <lab>/origin-pool \\
        --listen 127.0.0.1:18603 --upstream-proxy http://127.0.0.1:8888
    # 连同假 qBittorrent 一起起
    python scripts/perf/fake_image_origin.py --pool-dir <lab>/origin-pool \\
        --qbt-listen 127.0.0.1:18604 --qbt-torrents <lab>/origin/qbt-torrents.json
    # 直接取一张图（普通 GET 也能用，便于肉眼检查）
    curl -o /tmp/p.jpg http://127.0.0.1:18603/t/p/w500/poster_0123abcd.jpg
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import numpy as np
from PIL import Image, ImageDraw

# 只有这些主机的图片由替身生成；别的主机一律不代理（保证离线）
IMAGE_HOSTS = frozenset({"image.tmdb.org"})

# TMDB 官方的全部尺寸档位（/configuration 接口）。档位白名单同时防止有人拿
# w99999 之类的路径让替身生成巨图
_SIZES = frozenset(
    {"w45", "w92", "w154", "w185", "w300", "w342", "w500", "w780", "w1280", "h632", "original"}
)

# 每个 (类型, 档位) 的底图张数。original 剧照单张 1~2 MB，张数少一些控制磁盘
_POOL_SLOTS = {"poster": 48, "backdrop": 32, "still": 32, "logo": 32}
_ORIGINAL_BACKDROP_SLOTS = 24

# --build-pool 预生成的组合：覆盖实验室两页实际会请求的全部档位
_PREBUILD = [
    ("poster", "w500"),
    ("poster", "w342"),
    ("backdrop", "w780"),
    ("backdrop", "w1280"),
    ("backdrop", "original"),
    ("still", "w300"),
    ("still", "w500"),
    ("still", "w780"),
    ("still", "w1280"),
    ("logo", "w500"),
]

_TMDB_PATH = re.compile(r"^/t/p/([a-z0-9]+)/([A-Za-z0-9_.\-]+)$")


@dataclass(frozen=True)
class ImageSpec:
    """一张图的生成参数：类型、档位、像素、编码与底图槽位。"""

    kind: str
    size: str
    width: int
    height: int
    fmt: str  # "JPEG" / "PNG"
    slot: int
    grain: float  # JPEG 细颗粒噪声强度（决定体积）；PNG 为噪声带占比
    quality: int

    @property
    def ext(self) -> str:
        return "png" if self.fmt == "PNG" else "jpg"

    @property
    def content_type(self) -> str:
        return "image/png" if self.fmt == "PNG" else "image/jpeg"


def classify(filename: str) -> str:
    """文件名 → 图片类型（poster / backdrop / still / logo）。"""
    name = filename.lower()
    if name.endswith(".png") or name.endswith(".svg") or name.startswith("logo_"):
        return "logo"
    if name.startswith("backdrop_") or "back" in name:
        return "backdrop"
    if name.startswith("still_") or "still" in name:
        return "still"
    return "poster"


def _slots(kind: str, size: str) -> int:
    return (
        _ORIGINAL_BACKDROP_SLOTS if (kind, size) == ("backdrop", "original") else _POOL_SLOTS[kind]
    )


def _lerp(lo: float, hi: float, slot: int, slots: int) -> float:
    """按槽位在区间内均匀取值：同一档位的底图体积铺满目标区间，而不是扎堆。"""
    return lo + (hi - lo) * (slot / max(1, slots - 1))


def spec_for_slot(kind: str, size: str, slot: int) -> ImageSpec:
    """(类型, 档位, 槽位) → 生成参数。纯函数：同一槽位永远是同一张图。"""
    slots = _slots(kind, size)
    if kind == "logo":
        # Logo 是扁长条：宽高比 2:1 ~ 4:1；w 档位是宽度，original 按 1000 宽
        width = int(size[1:]) if size.startswith("w") else 1000
        aspect = (2.0, 2.5, 3.0, 4.0)[slot % 4]
        return ImageSpec(
            kind,
            size,
            width,
            max(8, round(width / aspect)),
            "PNG",
            slot,
            _lerp(0.05, 0.35, slot, slots),
            0,
        )
    if size == "original":
        if kind == "poster":
            return ImageSpec(kind, size, 2000, 3000, "JPEG", slot, _lerp(2.0, 6.0, slot, slots), 85)
        if kind == "backdrop" and slot % 3 != 0:
            return ImageSpec(kind, size, 3840, 2160, "JPEG", slot, _lerp(2.0, 7.0, slot, slots), 85)
        # 约三分之一的原图是 1920×1080：TMDB 上老片的背景图多是这个尺寸，
        # 画质参数调高，让体积仍落在 0.8 MB 以上（原图本来就压得少）
        return ImageSpec(kind, size, 1920, 1080, "JPEG", slot, _lerp(10.0, 16.0, slot, slots), 92)
    if size.startswith("h"):  # h632：人像档位按高度给
        height = int(size[1:])
        return ImageSpec(
            kind, size, round(height / 1.5), height, "JPEG", slot, _lerp(2.0, 6.0, slot, slots), 85
        )
    width = int(size[1:])
    if kind == "poster":
        return ImageSpec(
            kind, size, width, round(width * 1.5), "JPEG", slot, _lerp(2.0, 6.0, slot, slots), 85
        )
    grain = (4.0, 9.0) if kind == "backdrop" else (3.0, 7.0)
    return ImageSpec(
        kind, size, width, round(width * 9 / 16), "JPEG", slot, _lerp(*grain, slot, slots), 85
    )


def spec_for(size: str, filename: str) -> ImageSpec | None:
    """TMDB 档位 + 文件名 → 生成参数；档位不认识时返回 None（按 404 处理）。"""
    if size not in _SIZES:
        return None
    kind = classify(filename)
    digest = hashlib.sha1(filename.encode("utf-8")).hexdigest()
    return spec_for_slot(kind, size, int(digest[:8], 16) % _slots(kind, size))


# ---------------------------------------------------------------------------
# 图片生成（纯 CPU，服务时放线程里跑）
# ---------------------------------------------------------------------------


def _seed(spec: ImageSpec) -> int:
    digest = hashlib.sha1(f"{spec.kind}/{spec.size}/{spec.slot}".encode()).hexdigest()
    return int(digest[:12], 16)


def _field(rng: np.random.Generator, w: int, h: int, cell: int) -> np.ndarray:
    """低分辨率随机场双三次放大：给画面「大块结构」，放大本身几乎不花时间。"""
    small = rng.integers(0, 256, size=(max(2, h // cell), max(2, w // cell), 3), dtype=np.uint8)
    return np.asarray(Image.fromarray(small).resize((w, h), Image.BICUBIC), dtype=np.float32)


def _render_photo(spec: ImageSpec) -> bytes:
    seed = _seed(spec)
    rng = np.random.default_rng(seed)
    w, h = spec.width, spec.height
    c1, c2, c3 = (rng.integers(0, 256, 3).astype(np.float32) for _ in range(3))
    y = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    x = np.linspace(0, 1, w, dtype=np.float32)[None, :, None]
    base = (c1 * (1 - y) + c2 * y) * (1 - 0.5 * x) + c3 * (0.5 * x)
    img = base * 0.45 + _field(rng, w, h, max(w, h) // 6) * 0.35 + _field(rng, w, h, 24) * 0.20
    img += rng.normal(0, spec.grain, size=(h, w, 1)).astype(np.float32)
    image = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
    # 几何描边：人物轮廓 / 片名块的替身，给画面一些硬边（JPEG 的真实负担之一）
    draw = ImageDraw.Draw(image)
    shapes = random.Random(seed)
    for _ in range(12):
        x0, y0 = shapes.randrange(w), shapes.randrange(h)
        x1 = x0 + shapes.randrange(w // 12 + 1, w // 3 + 2)
        y1 = y0 + shapes.randrange(h // 12 + 1, h // 3 + 2)
        color = tuple(shapes.randrange(256) for _ in range(3))
        shape = draw.ellipse if shapes.random() < 0.5 else draw.rectangle
        shape([x0, y0, x1, y1], outline=color, width=max(2, w // 300))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=spec.quality)
    return buffer.getvalue()


def _render_logo(spec: ImageSpec) -> bytes:
    """透明底片名字标：一排「字形」（圆角块 / 椭圆 / 三角，带字腔），2 倍超采样抗锯齿。

    全透明像素的 RGB 清零——否则 PNG 要为看不见的颜色付体积；体积靠字形中段一条
    轻噪声带调节（带越宽越大），落在真实 Logo 的 20~60 KB。
    """
    seed = _seed(spec)
    shapes, rng = random.Random(seed), np.random.default_rng(seed)
    w, h = spec.width, spec.height
    big_w, big_h = w * 2, h * 2
    mask = Image.new("L", (big_w, big_h), 0)
    draw = ImageDraw.Draw(mask)
    count = shapes.randint(4, 9)
    x = shapes.randint(0, big_w // 20)
    gap = big_w // 60
    glyph = max(4, (big_w - 2 * x - gap * (count - 1)) // count)
    for _ in range(count):
        top, bottom = shapes.randint(0, big_h // 6), big_h - shapes.randint(0, big_h // 6)
        box = [x, top, x + glyph, bottom]
        pick = shapes.random()
        if pick < 0.4:
            draw.rounded_rectangle(box, radius=glyph // 4, fill=255)
        elif pick < 0.7:
            draw.ellipse(box, fill=255)
        else:
            draw.polygon([(x, bottom), (x + glyph // 2, top), (x + glyph, bottom)], fill=255)
        span = bottom - top
        draw.ellipse(
            [x + glyph // 4, top + span // 3, x + 3 * glyph // 4, top + 2 * span // 3], fill=0
        )
        x += glyph + gap
    alpha = np.asarray(mask.resize((w, h), Image.LANCZOS))
    c1 = rng.integers(120, 256, 3).astype(np.float32)
    c2 = rng.integers(0, 200, 3).astype(np.float32)
    y = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    rgb = np.broadcast_to(c1 * (1 - y) + c2 * y, (h, w, 3)).copy()
    band = int(h * spec.grain)
    start = (h - band) // 2
    rgb[start : start + band] += rng.normal(0, 2.0, size=(band, w, 1)).astype(np.float32)
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    rgb[alpha == 0] = 0
    buffer = io.BytesIO()
    Image.fromarray(np.dstack([rgb, alpha])).save(buffer, "PNG")
    return buffer.getvalue()


def render(spec: ImageSpec) -> bytes:
    return _render_logo(spec) if spec.fmt == "PNG" else _render_photo(spec)


class ImagePool:
    """底图池：磁盘上一张图一个文件，服务时读进内存常驻（全部加起来约 50 MB）。"""

    def __init__(self, pool_dir: Path) -> None:
        self._dir = pool_dir
        self._memory: dict[tuple[str, str, int], bytes] = {}
        self._locks: dict[tuple[str, str, int], asyncio.Lock] = {}

    def path_of(self, spec: ImageSpec) -> Path:
        return self._dir / spec.kind / spec.size / f"{spec.slot:03d}.{spec.ext}"

    def load_or_render(self, spec: ImageSpec) -> bytes:
        """同步版本（预生成与线程里用）：有文件读文件，没有就现场生成并原子落盘。"""
        path = self.path_of(spec)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            pass
        data = render(spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return data

    async def get(self, spec: ImageSpec) -> bytes:
        key = (spec.kind, spec.size, spec.slot)
        data = self._memory.get(key)
        if data is not None:
            return data
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:  # 同一张底图并发首取只生成一次
            data = self._memory.get(key)
            if data is None:
                data = await asyncio.to_thread(self.load_or_render, spec)
                self._memory[key] = data
        return data


def build_pool(pool_dir: Path) -> None:
    """预生成常用档位的全部底图并打印体积分布（reset 时跑一次，约 15 秒）。"""
    pool = ImagePool(pool_dir)
    started = time.perf_counter()
    print(f"{'类型/档位':<20}{'张数':>6}{'像素':>22}{'最小':>9}{'中位':>9}{'最大':>9}")
    for kind, size in _PREBUILD:
        specs = [spec_for_slot(kind, size, slot) for slot in range(_slots(kind, size))]
        sizes = sorted(len(pool.load_or_render(spec)) for spec in specs)
        dims = sorted({f"{spec.width}x{spec.height}" for spec in specs})
        label = "/".join(dims) if len(dims) <= 2 else f"{dims[0]} 等 {len(dims)} 种"
        print(
            f"{kind + ' ' + size:<20}{len(specs):>6}{label:>22}{sizes[0] / 1024:>8.0f}K"
            f"{sizes[len(sizes) // 2] / 1024:>8.0f}K{sizes[-1] / 1024:>8.0f}K"
        )
    total = sum(p.stat().st_size for p in pool_dir.rglob("*") if p.is_file())
    print(
        f"图池就绪：{pool_dir}，共 {total / 1024 / 1024:.1f} MB，"
        f"耗时 {time.perf_counter() - started:.1f}s"
    )


# ---------------------------------------------------------------------------
# 极简 HTTP/1.1（两个替身共用）：读一个请求、写一个响应，支持 keep-alive
# ---------------------------------------------------------------------------


def _log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


@dataclass
class Request:
    method: str
    target: str
    version: str
    headers: dict[str, str]
    body: bytes

    @property
    def keep_alive(self) -> bool:
        connection = self.headers.get("connection", "").lower()
        return connection != "close" if self.version == "HTTP/1.1" else connection == "keep-alive"


async def _read_request(reader: asyncio.StreamReader, *, with_body: bool = True) -> Request | None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        return None
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        return None
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length") or 0)
    body = await reader.readexactly(length) if (length and with_body) else b""
    return Request(parts[0], parts[1], parts[2], headers, body)


_REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 502: "Bad Gateway"}


async def _respond(
    writer: asyncio.StreamWriter,
    status: int,
    content_type: str,
    body: bytes,
    keep_alive: bool,
    *,
    send_body: bool = True,
    extra: str = "",
    rate: float = 0.0,
) -> None:
    head = (
        f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}\r\nServer: mc-fake-origin\r\n"
        f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\n{extra}"
        f"Connection: {'keep-alive' if keep_alive else 'close'}\r\n\r\n"
    )
    writer.write(head.encode("latin-1"))
    if send_body and body:
        if rate:
            # 可选的回源限速：按块写、按速率睡，模拟 CDN 到服务器这一段的带宽
            for offset in range(0, len(body), 64 * 1024):
                chunk = body[offset : offset + 64 * 1024]
                writer.write(chunk)
                await writer.drain()
                await asyncio.sleep(len(chunk) / rate)
        else:
            writer.write(body)
    await writer.drain()


# ---------------------------------------------------------------------------
# 假图床：正向代理（绝对 URL）+ 普通 GET + CONNECT 隧道
# ---------------------------------------------------------------------------


class FakeOrigin:
    """一个连接一个协程；HTTP/1.1 keep-alive，后端的 httpx 连接池会复用连接。"""

    def __init__(
        self,
        pool: ImagePool,
        *,
        upstream_proxy: str | None,
        tunnel_hosts: set[str],
        delay_ms: float,
        rate_mbps: float,
    ) -> None:
        self.pool = pool
        self.upstream = urlsplit(upstream_proxy) if upstream_proxy else None
        self.tunnel_hosts = tunnel_hosts
        self.delay = delay_ms / 1000
        self.rate = rate_mbps * 1_000_000 / 8 if rate_mbps > 0 else 0.0
        self.served = 0
        self.served_bytes = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while (request := await _read_request(reader)) is not None:
                if request.method == "CONNECT":
                    await self._tunnel(reader, writer, request.target)
                    return
                await self._serve(writer, request)
                if not request.keep_alive:
                    return
        except ConnectionError:
            return
        finally:
            writer.close()

    async def _serve(self, writer: asyncio.StreamWriter, request: Request) -> None:
        started = time.perf_counter()
        method, target, keep_alive = request.method, request.target, request.keep_alive
        if target.startswith("http://") or target.startswith("https://"):
            parts = urlsplit(target)  # 正向代理的绝对 URL 形态
            host, path = (parts.hostname or "").lower(), parts.path
        else:
            host, path = "", urlsplit(target).path  # 普通 GET：直接当图床用
        if path == "/healthz":
            body = json.dumps(
                {"ok": True, "served": self.served, "served_bytes": self.served_bytes}
            ).encode()
            await _respond(writer, 200, "application/json", body, keep_alive)
            return
        if host and host not in IMAGE_HOSTS:
            _log(f"{method} {target} 502 拒绝：离线假图床只替身 {sorted(IMAGE_HOSTS)}")
            await _respond(
                writer,
                502,
                "text/plain; charset=utf-8",
                "离线假图床不代理该主机".encode(),
                keep_alive,
            )
            return
        match = _TMDB_PATH.match(path)
        spec = spec_for(match.group(1), match.group(2)) if match else None
        if method not in ("GET", "HEAD") or spec is None:
            _log(f"{method} {host}{path} 404")
            await _respond(writer, 404, "text/plain", b"not found", keep_alive)
            return
        data = await self.pool.get(spec)
        if self.delay:
            await asyncio.sleep(self.delay)
        await _respond(
            writer,
            200,
            spec.content_type,
            data,
            keep_alive,
            send_body=method == "GET",
            extra="Cache-Control: public, max-age=31536000\r\n",
            rate=self.rate,
        )
        self.served += 1
        self.served_bytes += len(data)
        _log(
            f"{method} {host or '-'}{path} 200 {spec.width}x{spec.height} "
            f"{len(data) / 1024:.1f}KB {(time.perf_counter() - started) * 1000:.1f}ms"
        )

    async def _tunnel(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: str
    ) -> None:
        """CONNECT 隧道：只为白名单主机（TMDB 接口）开，转上游代理或直连。"""
        host, _, port_text = target.rpartition(":")
        host = host.strip("[]").lower()
        port = int(port_text or 443)
        if host in IMAGE_HOSTS or host not in self.tunnel_hosts:
            _log(f"CONNECT {target} 403 拒绝（不在隧道白名单 {sorted(self.tunnel_hosts)}）")
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return
        started = time.perf_counter()
        try:
            if self.upstream is not None:
                up_reader, up_writer = await asyncio.open_connection(
                    self.upstream.hostname, self.upstream.port or 80
                )
                up_writer.write(
                    f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode(
                        "latin-1"
                    )
                )
                await up_writer.drain()
                reply = await up_reader.readuntil(b"\r\n\r\n")
                if b" 200" not in reply.split(b"\r\n", 1)[0]:
                    raise ConnectionError(reply.split(b"\r\n", 1)[0].decode("latin-1"))
            else:
                up_reader, up_writer = await asyncio.open_connection(host, port)
        except (OSError, ConnectionError, asyncio.IncompleteReadError) as exc:
            _log(f"CONNECT {target} 502 上游不可达：{exc}")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        counters = [0, 0]

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter, index: int) -> None:
            try:
                while chunk := await src.read(65536):
                    counters[index] += len(chunk)
                    dst.write(chunk)
                    await dst.drain()
            except ConnectionError:
                pass
            finally:
                dst.close()

        await asyncio.gather(pipe(reader, up_writer, 0), pipe(up_reader, writer, 1))
        _log(
            f"CONNECT {target} 隧道关闭 上行 {counters[0]}B 下行 {counters[1]}B "
            f"{(time.perf_counter() - started) * 1000:.0f}ms"
        )


# ---------------------------------------------------------------------------
# 假 qBittorrent（可选）：让订阅首页的「下载中 / 整理中」与任务中心有真实数据
# ---------------------------------------------------------------------------


class FakeQbittorrent:
    """qBittorrent WebUI API v2 的最小替身，只实现 MovieClaw 读任务要用的几个端点。

    任务清单来自 ``seed_subscriptions_dataset.py --qbt-json``（与在途工单同一批
    infohash），每次请求都重新读文件——reset 后不必重启本进程。进度是静态快照：
    首页轮询看到的是稳定的「下载中 62%」，测速不受任务推进干扰。
    """

    def __init__(self, torrents_path: Path) -> None:
        self.path = torrents_path

    def torrents(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while (request := await _read_request(reader)) is not None:
                await self._serve(writer, request)
                if not request.keep_alive:
                    return
        except ConnectionError:
            return
        finally:
            writer.close()

    async def _serve(self, writer: asyncio.StreamWriter, request: Request) -> None:
        parts = urlsplit(request.target)
        params = dict(parse_qsl(parts.query))
        params.update(parse_qsl(request.body.decode("utf-8", "replace")))
        path, keep = parts.path, request.keep_alive
        text_plain = "text/plain; charset=UTF-8"
        if path == "/api/v2/auth/login":
            await _respond(
                writer,
                200,
                text_plain,
                b"Ok.",
                keep,
                extra="Set-Cookie: SID=mcperflab; HttpOnly; path=/\r\n",
            )
        elif path == "/api/v2/auth/logout":
            await _respond(writer, 200, text_plain, b"", keep)
        elif path == "/api/v2/app/version":
            await _respond(writer, 200, text_plain, b"v4.6.7", keep)
        elif path == "/api/v2/app/webapiVersion":
            await _respond(writer, 200, text_plain, b"2.9.3", keep)
        elif path == "/api/v2/app/buildInfo":
            await _respond(
                writer,
                200,
                "application/json",
                json.dumps(
                    {
                        "qt": "6.5.3",
                        "libtorrent": "2.0.10.0",
                        "boost": "1.83.0",
                        "openssl": "3.1.4",
                        "zlib": "1.3",
                        "bitness": 64,
                    }
                ).encode(),
                keep,
            )
        elif path == "/api/v2/app/preferences":
            await _respond(
                writer,
                200,
                "application/json",
                json.dumps(
                    {
                        "save_path": "/downloads",
                        "dl_limit": 0,
                        "up_limit": 0,
                        "max_active_downloads": 5,
                        "max_active_torrents": 10,
                    }
                ).encode(),
                keep,
            )
        elif path == "/api/v2/transfer/info":
            active = [t for t in self.torrents() if t.get("state") == "downloading"]
            await _respond(
                writer,
                200,
                "application/json",
                json.dumps(
                    {
                        "dl_info_speed": sum(t.get("dlspeed", 0) for t in active),
                        "up_info_speed": sum(t.get("upspeed", 0) for t in self.torrents()),
                        "dl_info_data": 0,
                        "up_info_data": 0,
                        "connection_status": "connected",
                    }
                ).encode(),
                keep,
            )
        elif path == "/api/v2/torrents/info":
            torrents = self.torrents()
            if params.get("hashes"):
                wanted = {h.lower() for h in params["hashes"].split("|")}
                torrents = [t for t in torrents if t["hash"] in wanted]
            await _respond(writer, 200, "application/json", json.dumps(torrents).encode(), keep)
        elif path in ("/api/v2/torrents/properties", "/api/v2/torrents/files"):
            torrent = next(
                (t for t in self.torrents() if t["hash"] == params.get("hash", "").lower()), None
            )
            if torrent is None:
                await _respond(writer, 404, text_plain, b"Not Found", keep)
                return
            body = (
                {"save_path": torrent["save_path"], "total_size": torrent["size"]}
                if path.endswith("properties")
                else [
                    {
                        "index": 0,
                        "name": torrent["name"] + ".mkv",
                        "size": torrent["size"],
                        "progress": torrent["progress"],
                        "priority": 1,
                    }
                ]
            )
            await _respond(writer, 200, "application/json", json.dumps(body).encode(), keep)
        else:
            _log(f"假 qBittorrent：未实现的端点 {request.method} {path}（返回 404）")
            await _respond(writer, 404, text_plain, b"Not Found", keep)


async def serve(args: argparse.Namespace) -> None:
    host, _, port = args.listen.rpartition(":")
    origin = FakeOrigin(
        ImagePool(Path(args.pool_dir)),
        upstream_proxy=args.upstream_proxy or None,
        tunnel_hosts=set(args.tunnel_host or ["api.themoviedb.org"]),
        delay_ms=args.delay_ms,
        rate_mbps=args.rate_mbps,
    )
    servers = [await asyncio.start_server(origin.handle, host, int(port), limit=64 * 1024)]
    _log(
        f"离线假图床已监听 {args.listen}（替身 {sorted(IMAGE_HOSTS)}；隧道白名单 "
        f"{sorted(origin.tunnel_hosts)}；上游代理 {args.upstream_proxy or '直连'}；"
        f"首字节延迟 {args.delay_ms}ms；限速 {args.rate_mbps or '不限'} Mbps）"
    )
    if args.qbt_listen:
        qbt = FakeQbittorrent(Path(args.qbt_torrents))
        qhost, _, qport = args.qbt_listen.rpartition(":")
        servers.append(await asyncio.start_server(qbt.handle, qhost, int(qport)))
        _log(
            f"假 qBittorrent 已监听 {args.qbt_listen}（任务清单 {args.qbt_torrents}，"
            f"当前 {len(qbt.torrents())} 个任务）"
        )
    await asyncio.gather(*(server.serve_forever() for server in servers))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--pool-dir", required=True, help="底图池目录（放在实验室数据目录里）")
    parser.add_argument("--build-pool", action="store_true", help="预生成常用档位的底图后退出")
    parser.add_argument("--listen", default="127.0.0.1:18603")
    parser.add_argument(
        "--upstream-proxy",
        default="",
        help="CONNECT 隧道的上游 HTTP 代理（如 http://127.0.0.1:8888）；空=直连",
    )
    parser.add_argument(
        "--tunnel-host",
        action="append",
        help="允许 CONNECT 的主机（可多次给），默认只有 api.themoviedb.org",
    )
    parser.add_argument(
        "--delay-ms", type=float, default=0.0, help="图片响应的首字节延迟（模拟 CDN）"
    )
    parser.add_argument("--rate-mbps", type=float, default=0.0, help="图片响应限速（Mbps），0=不限")
    parser.add_argument(
        "--qbt-listen", default="", help="同时起假 qBittorrent 的监听地址，如 127.0.0.1:18604"
    )
    parser.add_argument("--qbt-torrents", default="", help="假 qBittorrent 的任务清单 JSON")
    args = parser.parse_args()
    if args.build_pool:
        build_pool(Path(args.pool_dir))
        return
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
