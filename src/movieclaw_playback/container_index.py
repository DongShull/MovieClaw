"""容器索引读取：不解码、不读正文，只读文件头和索引，拿到「片子长什么样」。

刷片（docs/design/reels.md）要为每部片挑一段 30～60 秒的精彩片段，并告诉 App
这一段在文件里的字节位置好提前预取。这些信息全都能从容器自带的索引里得到，
不必解码画面、也不必把几十 GB 的正片读一遍：

- **视频关键帧的时间与字节位置**：相邻两个关键帧之间的字节数 ÷ 时间差 ≈ 那一段
  的码率。编码器给运动大、剪辑快、画面复杂的镜头分更多字节，码率曲线因此能粗略
  反映「激烈程度」；字节位置本身就是 App 预取「从某秒起播」所需数据的范围。
- **字幕事件的时间**：mkvmerge 默认给字幕轨的每一条事件都写索引点（PGS 图形字幕
  也一样），不用读正文就知道哪里在说话、哪里是两句对白之间的空隙——片段的起止
  卡在空隙上，才不会切在半句话中间。
- **章节**：原盘作者或片源标的章节，通常落在场景开头。
- **文件头与索引自身的字节范围**：引擎打开文件时要读文件头（Matroska 到第一个
  Cluster 为止，MP4 是 moov），跳到起点时要读索引（Cues / moov），这两段也要预取。

支持两类容器：
- **Matroska（mkv/webm）**：SeekHead 定位 Info / Tracks / Chapters / Cues，全部按
  元素自身长度精确读取，整个过程通常只读几百 KB～1 MB。
- **MP4 / MOV**：顶层 box 逐个跳过 mdat 找到 moov，解析 stbl 的各张表
  （stts / stss / stsz / stsc / stco / co64）。**不能用 ffprobe 列包**：ffprobe
  列包会顺带读出每个包的数据，等于把整个 mdat 通读一遍（2026-09-29 NAS 实测
  15.7 GB 的 MP4 读了 60 秒、5.7 GB 仍未读完）。

其余容器（TS / M2TS / AVI / 原盘目录 / 镜像）返回 None，由调用方跳过。
所有入口都是同步阻塞 IO，调用方负责放进线程池。

**文件尾被截掉的 Matroska**（下载或复制中断，Segment 声明的长度比文件长）：mkvmerge 把
Cues、Tags 和可能有的第二个 SeekHead 写在文件尾，截断后最先丢的就是它们。Cues 还完整
就照常解析——文件尾的 SeekHead 只是目录，读不出就跳过；Cues 已不在文件里则按合同返回
None，日志写明「文件不完整」，而不是报一句看不懂的越界。
"""

from __future__ import annotations

import logging
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("movieclaw_playback.container_index")

# 轨道类型（与容器无关的统一叫法）
KIND_VIDEO = "video"
KIND_AUDIO = "audio"
KIND_SUBTITLE = "subtitle"
KIND_OTHER = "other"


@dataclass(frozen=True)
class KeyframePoint:
    """一个视频关键帧：时间（秒）与起播时要从哪个字节开始读。

    ``offset`` 是「从这里开始读就能解出这个关键帧」的绝对文件偏移：Matroska 取
    关键帧所在 Cluster 的起点（Cluster 里音视频交错，从头读才完整），MP4 取这个
    关键帧样本本身的位置。
    """

    time_s: float
    offset: int


@dataclass(frozen=True)
class TrackInfo:
    """容器里的一条轨道。``number`` 是容器自己的轨号（Matroska TrackNumber /
    MP4 track_ID），``order`` 是同类轨里的先后序号（从 0 起，与 ffprobe 按类型
    排列的顺序一致，用来和台账里的 audio_streams / subtitle_streams 对上）。"""

    number: int
    kind: str
    codec: str
    order: int
    language: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class ContainerIndex:
    """一个文件的索引摘要。所有时间都是秒，所有偏移都是绝对文件偏移。"""

    container: str  # "matroska" / "mp4"
    file_size: int
    duration_s: float
    keyframes: tuple[KeyframePoint, ...]
    tracks: tuple[TrackInfo, ...]
    #: 字幕轨号 → 这条轨上每个事件（显示或清屏）的时间，升序
    subtitle_events: dict[int, tuple[float, ...]] = field(default_factory=dict)
    #: (起点秒, 标题) 升序
    chapters: tuple[tuple[float, str | None], ...] = ()
    #: 引擎打开文件要读的文件头：[0, head_end)
    head_end: int = 0
    #: 索引自身的字节范围 [start, end)；已含在文件头里时为 None
    index_range: tuple[int, int] | None = None

    def tracks_of(self, kind: str) -> list[TrackInfo]:
        return [t for t in self.tracks if t.kind == kind]


#: (路径, mtime_ns, 大小) → 索引。解析一部片要零点几秒到一两秒（Cues 大的原盘
#: remux 有六万个索引点），同一次刷片里同一文件可能被反复用到。满了整体清空——
#: 纯加速缓存（keyframes.py 同款取舍）。
_cache: dict[tuple[str, int, int], ContainerIndex] = {}
_CACHE_MAX = 128

_MATROSKA_SUFFIXES = frozenset({".mkv", ".webm", ".mka"})
_MP4_SUFFIXES = frozenset({".mp4", ".m4v", ".mov"})
#: EBML 头的元素 ID，所有 Matroska / WebM 文件的开头 4 字节
_EBML_MAGIC = b"\x1a\x45\xdf\xa3"


def read_container_index(path: str | Path) -> ContainerIndex | None:
    """读取文件的容器索引；不支持的容器或解析失败返回 None。"""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    suffix = path.suffix.lower()
    try:
        if suffix not in _MATROSKA_SUFFIXES | _MP4_SUFFIXES:
            return None
        # 以文件头魔数为准、后缀只是兜底：实际有 Matroska 内容却被命名成 .mp4 的片源
        # （ffmpeg 靠内容嗅探照常能播），只看后缀会按 MP4 解析，报「找不到 moov」。
        with path.open("rb") as f:
            magic = f.read(4)
        if magic == _EBML_MAGIC or suffix in _MATROSKA_SUFFIXES:
            index = _read_matroska(path, stat.st_size)
        else:
            index = _read_mp4(path, stat.st_size)
    # ValueError 是解析器自己抛的（结构不对、缺索引），IndexError / struct.error
    # 是截断或损坏的文件让解析读越了界。这里是「按合同失败返回 None」的唯一出口。
    except (OSError, ValueError, IndexError, struct.error) as exc:
        logger.warning("读取容器索引失败：%s（%s）", path, exc)
        return None
    if index is None or not index.keyframes:
        return None
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = index
    return index


# --- Matroska -----------------------------------------------------------------

_EBML_HEADER = 0x1A45DFA3
_SEGMENT = 0x18538067
_SEEK_HEAD = 0x114D9B74
_SEEK = 0x4DBB
_SEEK_ID = 0x53AB
_SEEK_POSITION = 0x53AC
_INFO = 0x1549A966
_TIMESTAMP_SCALE = 0x2AD7B1
_DURATION = 0x4489
_TRACKS = 0x1654AE6B
_TRACK_ENTRY = 0xAE
_TRACK_NUMBER = 0xD7
_TRACK_TYPE = 0x83
_CODEC_ID = 0x86
_LANGUAGE = 0x22B59C
_LANGUAGE_BCP47 = 0x22B59D
_NAME = 0x536E
_CHAPTERS = 0x1043A770
_EDITION_ENTRY = 0x45B9
_CHAPTER_ATOM = 0xB6
_CHAPTER_TIME_START = 0x91
_CHAPTER_FLAG_HIDDEN = 0x98
_CHAPTER_DISPLAY = 0x80
_CHAP_STRING = 0x85
_CUES = 0x1C53BB6B
_CUE_POINT = 0xBB
_CUE_TIME = 0xB3
_CUE_TRACK_POSITIONS = 0xB7
_CUE_TRACK = 0xF7
_CUE_CLUSTER_POSITION = 0xF1
_CLUSTER = 0x1F43B675

_MKV_TRACK_KIND = {1: KIND_VIDEO, 2: KIND_AUDIO, 17: KIND_SUBTITLE}

#: 单个索引类元素的读取上限。Cues 最大见过 1.4 MB（三小时 UHD remux、23 条字幕轨），
#: 超过 64 MB 的只可能是损坏文件，不值得读进内存。
_MAX_ELEMENT_BYTES = 64 << 20
#: 找第一个 Cluster 时最多跳过的顶层元素个数（SeekHead / Void / Info / Tracks /
#: Chapters / Attachments / Tags / Cues 加起来不过十来个）
_MAX_TOP_LEVEL_SCAN = 64


def _vint(data: bytes, pos: int, *, keep_marker: bool) -> tuple[int, int, bool]:
    """读一个 EBML 变长整数，返回 (值, 新位置, 是否为「未知长度」)。"""
    first = data[pos]
    length = 1
    mask = 0x80
    while mask and not (first & mask):
        length += 1
        mask >>= 1
    if not mask:
        raise ValueError("非法的 EBML 变长整数")
    value = first if keep_marker else first & (mask - 1)
    for i in range(1, length):
        value = (value << 8) | data[pos + i]
    unknown = (not keep_marker) and value == (1 << (7 * length)) - 1
    return value, pos + length, unknown


def _children(data: bytes, start: int, end: int):
    """遍历 [start, end) 内的子元素，产出 (id, body_start, body_end)。"""
    pos = start
    while pos < end:
        element_id, pos, _ = _vint(data, pos, keep_marker=True)
        size, pos, unknown = _vint(data, pos, keep_marker=False)
        body_end = end if unknown else min(pos + size, end)
        yield element_id, pos, body_end
        pos = body_end


def _uint(data: bytes, start: int, end: int) -> int:
    return int.from_bytes(data[start:end], "big")


def _float(data: bytes, start: int, end: int) -> float:
    raw = data[start:end]
    if len(raw) == 4:
        return struct.unpack(">f", raw)[0]
    if len(raw) == 8:
        return struct.unpack(">d", raw)[0]
    return 0.0


def _text(data: bytes, start: int, end: int) -> str:
    return data[start:end].split(b"\x00", 1)[0].decode("utf-8", "replace")


def _read_element_at(f, offset: int, expected_id: int) -> tuple[bytes, int]:
    """读出 offset 处的整个元素，返回 (元素字节, 正文在其中的起点)。"""
    f.seek(offset)
    header = f.read(16)
    if not header:
        # SeekHead 记的位置在文件末尾之外：文件尾被截掉了（NAS 上见过只剩 64% 的 remux，
        # Cues 在第 186 亿字节、文件只有 118 亿字节）。不拦的话下面解析空字节会越界
        raise ValueError(
            f"元素 0x{expected_id:X} 应在第 {offset} 字节，文件却只有 "
            f"{os.fstat(f.fileno()).st_size} 字节：文件不完整，可能下载或复制时中断了"
        )
    element_id, p, _ = _vint(header, 0, keep_marker=True)
    if element_id != expected_id:
        raise ValueError(f"位置 {offset} 上不是期望的元素 0x{expected_id:X}")
    size, p, unknown = _vint(header, p, keep_marker=False)
    if unknown or size > _MAX_ELEMENT_BYTES:
        raise ValueError(f"元素 0x{expected_id:X} 长度异常")
    f.seek(offset)
    data = f.read(p + size)
    if len(data) < p + size:
        raise ValueError(f"元素 0x{expected_id:X} 被截断")
    return data, p


def _matroska_layout(f, file_size: int, path: Path) -> tuple[int, dict[int, int], int | None]:
    """走一遍 Matroska 的顶层结构。

    返回 (Segment 正文起点, 各顶层元素的绝对位置, 第一个 Cluster 的位置)。顶层元素逐个
    跳过直到第一个 Cluster，顺路收集 SeekHead；SeekHead 指向的位置（包括写在文件尾的
    第二个 SeekHead）再跟一层。只读元素头与 SeekHead 正文，不读别的元素正文。
    """
    f.seek(0)
    head = f.read(64 * 1024)
    element_id, pos, _ = _vint(head, 0, keep_marker=True)
    if element_id != _EBML_HEADER:
        raise ValueError("不是 Matroska 文件")
    size, pos, _ = _vint(head, pos, keep_marker=False)
    pos += size
    element_id, pos, _ = _vint(head, pos, keep_marker=True)
    if element_id != _SEGMENT:
        raise ValueError("缺少 Segment 元素")
    _, pos, _ = _vint(head, pos, keep_marker=False)
    segment_start = pos  # SeekHead 里的位置都相对这里

    # 顶层元素逐个跳过，直到第一个 Cluster：顺路收集 SeekHead 与各元素位置。
    # 每个元素只读 16 字节的头，正文按长度跳过。
    positions: dict[int, int] = {}
    seek_heads: list[int] = []
    first_cluster = None
    cursor = segment_start
    for _ in range(_MAX_TOP_LEVEL_SCAN):
        if cursor >= file_size:
            break
        f.seek(cursor)
        header = f.read(16)
        if len(header) < 2:
            break
        element_id, p, _ = _vint(header, 0, keep_marker=True)
        size, p, unknown = _vint(header, p, keep_marker=False)
        if element_id == _CLUSTER:
            first_cluster = cursor
            break
        if unknown:
            break
        if element_id == _SEEK_HEAD:
            seek_heads.append(cursor)
        elif element_id in (_INFO, _TRACKS, _CHAPTERS, _CUES):
            positions.setdefault(element_id, cursor)
        cursor += p + size

    # SeekHead 可能指向另一个 SeekHead（写在文件尾的第二索引），跟一层。
    # SeekHead 只是目录：文件尾被截掉时第二个 SeekHead 会读不全（NAS 上见过缺尾 900 KB
    # 的 UHD remux，Cues 完好、第二个 SeekHead 只剩半截），跳过它照样能用前面找到的
    # Cues；Cues 真找不到或读不了，由调用方报错
    visited: set[int] = set()
    while seek_heads:
        at = seek_heads.pop(0)
        if at in visited:
            continue
        visited.add(at)
        try:
            data, body = _read_element_at(f, at, _SEEK_HEAD)
        except ValueError as exc:
            logger.info("Matroska 元素目录（SeekHead）读不出，跳过它继续：%s（%s）", path, exc)
            continue
        for child_id, c_start, c_end in _children(data, body, len(data)):
            if child_id != _SEEK:
                continue
            target_id = target_pos = None
            for g_id, g_start, g_end in _children(data, c_start, c_end):
                if g_id == _SEEK_ID:
                    target_id = _uint(data, g_start, g_end)
                elif g_id == _SEEK_POSITION:
                    target_pos = _uint(data, g_start, g_end)
            if target_id is None or target_pos is None:
                continue
            absolute = segment_start + target_pos
            if target_id == _SEEK_HEAD:
                seek_heads.append(absolute)
            elif target_id in (_INFO, _TRACKS, _CHAPTERS, _CUES):
                positions.setdefault(target_id, absolute)
    return segment_start, positions, first_cluster


def _read_matroska(path: Path, file_size: int) -> ContainerIndex:
    with path.open("rb") as f:
        segment_start, positions, first_cluster = _matroska_layout(f, file_size, path)
        if _CUES not in positions:
            raise ValueError("找不到 Cues（此文件缺关键帧索引）")

        # Info：时间刻度与总时长
        timestamp_scale = 1_000_000
        duration_ticks = 0.0
        if _INFO in positions:
            data, body = _read_element_at(f, positions[_INFO], _INFO)
            for child_id, c_start, c_end in _children(data, body, len(data)):
                if child_id == _TIMESTAMP_SCALE:
                    timestamp_scale = _uint(data, c_start, c_end) or 1_000_000
                elif child_id == _DURATION:
                    duration_ticks = _float(data, c_start, c_end)
        tick_s = timestamp_scale / 1_000_000_000

        # Tracks：轨号 → 类型 / 编码 / 语言 / 名称
        tracks: list[TrackInfo] = []
        if _TRACKS in positions:
            data, body = _read_element_at(f, positions[_TRACKS], _TRACKS)
            order_by_kind: dict[str, int] = {}
            for child_id, c_start, c_end in _children(data, body, len(data)):
                if child_id != _TRACK_ENTRY:
                    continue
                number = kind_code = None
                codec = ""
                language = bcp47 = name = None
                for g_id, g_start, g_end in _children(data, c_start, c_end):
                    if g_id == _TRACK_NUMBER:
                        number = _uint(data, g_start, g_end)
                    elif g_id == _TRACK_TYPE:
                        kind_code = _uint(data, g_start, g_end)
                    elif g_id == _CODEC_ID:
                        codec = _text(data, g_start, g_end)
                    elif g_id == _LANGUAGE:
                        language = _text(data, g_start, g_end)
                    elif g_id == _LANGUAGE_BCP47:
                        bcp47 = _text(data, g_start, g_end)
                    elif g_id == _NAME:
                        name = _text(data, g_start, g_end)
                if number is None:
                    continue
                kind = _MKV_TRACK_KIND.get(kind_code or 0, KIND_OTHER)
                order = order_by_kind.get(kind, 0)
                order_by_kind[kind] = order + 1
                tracks.append(
                    TrackInfo(
                        number=number,
                        kind=kind,
                        codec=codec,
                        order=order,
                        language=bcp47 or language,
                        name=name or None,
                    )
                )
        video_numbers = {t.number for t in tracks if t.kind == KIND_VIDEO}
        subtitle_numbers = {t.number for t in tracks if t.kind == KIND_SUBTITLE}

        # Chapters：取第一个版本里不隐藏的章节
        chapters: list[tuple[float, str | None]] = []
        if _CHAPTERS in positions:
            data, body = _read_element_at(f, positions[_CHAPTERS], _CHAPTERS)
            for child_id, c_start, c_end in _children(data, body, len(data)):
                if child_id != _EDITION_ENTRY:
                    continue
                for atom_id, a_start, a_end in _children(data, c_start, c_end):
                    if atom_id != _CHAPTER_ATOM:
                        continue
                    start_ns = None
                    hidden = False
                    title = None
                    for g_id, g_start, g_end in _children(data, a_start, a_end):
                        if g_id == _CHAPTER_TIME_START:
                            start_ns = _uint(data, g_start, g_end)
                        elif g_id == _CHAPTER_FLAG_HIDDEN:
                            hidden = bool(_uint(data, g_start, g_end))
                        elif g_id == _CHAPTER_DISPLAY and title is None:
                            for d_id, d_start, d_end in _children(data, g_start, g_end):
                                if d_id == _CHAP_STRING:
                                    title = _text(data, d_start, d_end) or None
                                    break
                    if start_ns is not None and not hidden:
                        chapters.append((start_ns / 1_000_000_000, title))
                break

        # Cues：视频关键帧（时间 + Cluster 位置）与字幕事件时间
        cues_offset = positions[_CUES]
        data, body = _read_element_at(f, cues_offset, _CUES)
        cues_end = cues_offset + len(data)
        keyframes: dict[float, int] = {}
        subtitle_events: dict[int, list[float]] = {n: [] for n in subtitle_numbers}
        for child_id, c_start, c_end in _children(data, body, len(data)):
            if child_id != _CUE_POINT:
                continue
            cue_time = None
            positions_in_point: list[tuple[int, int]] = []
            for g_id, g_start, g_end in _children(data, c_start, c_end):
                if g_id == _CUE_TIME:
                    cue_time = _uint(data, g_start, g_end)
                elif g_id == _CUE_TRACK_POSITIONS:
                    track = cluster = None
                    for h_id, h_start, h_end in _children(data, g_start, g_end):
                        if h_id == _CUE_TRACK:
                            track = _uint(data, h_start, h_end)
                        elif h_id == _CUE_CLUSTER_POSITION:
                            cluster = _uint(data, h_start, h_end)
                    if track is not None and cluster is not None:
                        positions_in_point.append((track, cluster))
            if cue_time is None:
                continue
            time_s = cue_time * tick_s
            for track, cluster in positions_in_point:
                # Tracks 解析失败时视频轨号未知：只好把所有索引点都当视频
                if track in video_numbers or not video_numbers:
                    keyframes.setdefault(time_s, segment_start + cluster)
                elif track in subtitle_events:
                    subtitle_events[track].append(time_s)

    duration_s = duration_ticks * tick_s
    points = tuple(KeyframePoint(t, keyframes[t]) for t in sorted(keyframes))
    if not duration_s and points:
        duration_s = points[-1].time_s
    head_end = first_cluster if first_cluster is not None else min(file_size, 1 << 20)
    index_range = None if cues_end <= head_end else (cues_offset, cues_end)
    return ContainerIndex(
        container="matroska",
        file_size=file_size,
        duration_s=duration_s,
        keyframes=points,
        tracks=tuple(tracks),
        subtitle_events={k: tuple(sorted(v)) for k, v in subtitle_events.items()},
        chapters=tuple(sorted(chapters, key=lambda c: c[0])),
        head_end=head_end,
        index_range=index_range,
    )


# --- Matroska 精简索引（播放起播用） -------------------------------------------


@dataclass(frozen=True)
class MatroskaVideoCues:
    """只含视频轨索引点的 Matroska Cues 元素（docs/design/playback-qoe.md §9.12，引擎补丁 P58）。

    App 的播放引擎打开 MKV 时要先拿到整个 Cues 才能规划分片、定位续播点。mkvmerge
    给每条字幕轨的每个事件都写索引点，字幕轨多的片子 Cues 因此很大（片库抽样：中位
    68 KB、九成在 560 KB 以内、最大 4.2 MB；62 条字幕轨的那部 4K 有 1.25 MB），慢线路
    上要单独下好几秒。引擎只用得到视频轨的索引点，所以服务端从原 Cues 里挑出它们、
    原样（同一个 CueTime、同一个 CueClusterPosition）重新编码成一个很小的 Cues 元素，
    随播放会话下发；引擎在解复用器读 SeekHead 登记的 Cues 位置时直接给这份，不再下载
    原索引。

    只保留 libavformat 建索引用得到的三项（CueTime、CueTrack、CueClusterPosition，见
    FFmpeg ``matroska_add_index_entries``），数值与原文件逐位一致，解复用器看到的视频
    索引与读原 Cues 时相同。
    """

    #: Cues 元素在文件里的绝对位置（SeekHead 登记的那个位置）
    cues_offset: int
    #: 原 Cues 元素（含元素头）多少字节
    original_bytes: int
    #: 精简后的整个 Cues 元素（含元素头）
    data: bytes
    #: 保留了多少个索引点
    points: int


def _ebml_size(value: int) -> bytes:
    """EBML 元素长度的最短写法（全 1 是「未知长度」的保留值，要让开）。"""
    length = 1
    while value >= (1 << (7 * length)) - 1:
        length += 1
    return (value | (1 << (7 * length))).to_bytes(length, "big")


def _ebml_uint(value: int) -> bytes:
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")


def _ebml_element(element_id: int, payload: bytes) -> bytes:
    return (
        element_id.to_bytes((element_id.bit_length() + 7) // 8, "big")
        + _ebml_size(len(payload))
        + payload
    )


def build_matroska_video_cues(path: str | Path) -> MatroskaVideoCues | None:
    """读 MKV 的 Cues，只留视频轨的索引点、重新编码成一个 Cues 元素。

    不是 MKV、没有 Cues、找不到视频轨、读失败时返回 None。同步阻塞 IO，调用方放进
    单独进程或线程池。"""
    path = Path(path)
    try:
        file_size = path.stat().st_size
        with path.open("rb") as f:
            _, positions, first_cluster = _matroska_layout(f, file_size, path)
            if _CUES not in positions or _TRACKS not in positions:
                return None
            # 只处理写在簇后面（文件尾）的 Cues：写在簇前面的会被解复用器读文件头时顺序走到，
            # 换成长度不同的精简版，它后面的元素就对不上了
            if first_cluster is None or positions[_CUES] < first_cluster:
                return None
            video_numbers: set[int] = set()
            data, body = _read_element_at(f, positions[_TRACKS], _TRACKS)
            for child_id, c_start, c_end in _children(data, body, len(data)):
                if child_id != _TRACK_ENTRY:
                    continue
                number = kind = None
                for g_id, g_start, g_end in _children(data, c_start, c_end):
                    if g_id == _TRACK_NUMBER:
                        number = _uint(data, g_start, g_end)
                    elif g_id == _TRACK_TYPE:
                        kind = _uint(data, g_start, g_end)
                if number is not None and kind == 1:
                    video_numbers.add(number)
            if not video_numbers:
                return None
            cues_offset = positions[_CUES]
            cues, body = _read_element_at(f, cues_offset, _CUES)
    except (OSError, ValueError, IndexError):
        return None

    out = bytearray()
    kept = 0
    for child_id, c_start, c_end in _children(cues, body, len(cues)):
        if child_id != _CUE_POINT:
            continue
        cue_time = None
        video_positions: list[tuple[int, int]] = []
        for g_id, g_start, g_end in _children(cues, c_start, c_end):
            if g_id == _CUE_TIME:
                cue_time = _uint(cues, g_start, g_end)
            elif g_id == _CUE_TRACK_POSITIONS:
                track = cluster = None
                for h_id, h_start, h_end in _children(cues, g_start, g_end):
                    if h_id == _CUE_TRACK:
                        track = _uint(cues, h_start, h_end)
                    elif h_id == _CUE_CLUSTER_POSITION:
                        cluster = _uint(cues, h_start, h_end)
                if track in video_numbers and cluster is not None:
                    video_positions.append((track, cluster))
        if cue_time is None or not video_positions:
            continue
        point = _ebml_element(_CUE_TIME, _ebml_uint(cue_time))
        for track, cluster in video_positions:
            point += _ebml_element(
                _CUE_TRACK_POSITIONS,
                _ebml_element(_CUE_TRACK, _ebml_uint(track))
                + _ebml_element(_CUE_CLUSTER_POSITION, _ebml_uint(cluster)),
            )
        out += _ebml_element(_CUE_POINT, point)
        kept += 1
    if kept < 2:
        return None  # libavformat 见到不足两个索引点的 Cues 会整个丢弃，不如让它读原索引
    return MatroskaVideoCues(
        cues_offset=cues_offset,
        original_bytes=len(cues),
        data=_ebml_element(_CUES, bytes(out)),
        points=kept,
    )


# --- MP4 / MOV ------------------------------------------------------------------

#: moov 的读取上限：三小时的片子样本表也就几十 MB，再大只可能是损坏文件
_MAX_MOOV_BYTES = 256 << 20
_MP4_HANDLER_KIND = {
    b"vide": KIND_VIDEO,
    b"soun": KIND_AUDIO,
    b"sbtl": KIND_SUBTITLE,
    b"subt": KIND_SUBTITLE,
    b"text": KIND_SUBTITLE,
    b"clcp": KIND_SUBTITLE,
}


def _boxes(data: bytes, start: int, end: int):
    """遍历 [start, end) 内的 box，产出 (类型, 正文起点, 正文终点)。"""
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            raise ValueError("MP4 box 长度异常")
        yield kind, pos + header, min(pos + size, end)
        pos += size


def _find_box(data: bytes, start: int, end: int, kind: bytes) -> tuple[int, int] | None:
    for k, s, e in _boxes(data, start, end):
        if k == kind:
            return s, e
    return None


def _full_box_version(data: bytes, start: int) -> int:
    return data[start]


def _mp4_language(code: int) -> str | None:
    """mdhd 里的 ISO-639-2/T 三字母语言码（每字母 5 bit，+0x60）。"""
    if not code:
        return None
    letters = "".join(chr(((code >> shift) & 0x1F) + 0x60) for shift in (10, 5, 0))
    return None if letters in ("und", "```") else letters


def _read_mp4(path: Path, file_size: int) -> ContainerIndex:
    with path.open("rb") as f:
        # 顶层 box 逐个看头，跳过 mdat，找到 moov 与第一个 mdat 的位置
        moov = None
        first_mdat_body = None
        cursor = 0
        for _ in range(_MAX_TOP_LEVEL_SCAN):
            if cursor + 8 > file_size:
                break
            f.seek(cursor)
            header = f.read(16)
            if len(header) < 8:
                break
            size, kind = struct.unpack_from(">I4s", header, 0)
            header_len = 8
            if size == 1:
                size = struct.unpack_from(">Q", header, 8)[0]
                header_len = 16
            elif size == 0:
                size = file_size - cursor
            if size < header_len:
                raise ValueError("MP4 顶层 box 长度异常")
            if kind == b"moov":
                moov = (cursor, cursor + size)
            elif kind == b"mdat" and first_mdat_body is None:
                first_mdat_body = cursor + header_len
            elif kind == b"moof":
                raise ValueError("分片 MP4（moof）暂不支持")
            if moov is not None and first_mdat_body is not None:
                break
            cursor += size
        if moov is None:
            raise ValueError("找不到 moov")
        moov_start, moov_end = moov
        if moov_end - moov_start > _MAX_MOOV_BYTES:
            raise ValueError("moov 过大")
        f.seek(moov_start)
        data = f.read(moov_end - moov_start)
    if len(data) < moov_end - moov_start:
        raise ValueError("moov 被截断")

    body_start = 8 if struct.unpack_from(">I", data, 0)[0] != 1 else 16
    movie_timescale = 0
    movie_duration = 0
    mvhd = _find_box(data, body_start, len(data), b"mvhd")
    if mvhd:
        s, _ = mvhd
        if _full_box_version(data, s) == 1:
            movie_timescale, movie_duration = struct.unpack_from(">IQ", data, s + 20)
        else:
            movie_timescale, movie_duration = struct.unpack_from(">II", data, s + 12)

    tracks: list[TrackInfo] = []
    keyframes: list[KeyframePoint] = []
    subtitle_events: dict[int, tuple[float, ...]] = {}
    order_by_kind: dict[str, int] = {}
    video_done = False
    for kind, t_start, t_end in _boxes(data, body_start, len(data)):
        if kind != b"trak":
            continue
        track = _parse_trak(data, t_start, t_end)
        if track is None:
            continue
        track_kind = track["kind"]
        order = order_by_kind.get(track_kind, 0)
        order_by_kind[track_kind] = order + 1
        tracks.append(
            TrackInfo(
                number=track["id"],
                kind=track_kind,
                codec=track["codec"],
                order=order,
                language=track["language"],
            )
        )
        if track_kind == KIND_VIDEO and not video_done:
            keyframes = _mp4_keyframes(track)
            video_done = True
        elif track_kind == KIND_SUBTITLE:
            subtitle_events[track["id"]] = _mp4_subtitle_events(track)

    chapters = _mp4_nero_chapters(data, body_start)
    duration_s = movie_duration / movie_timescale if movie_timescale else 0.0
    if not duration_s and keyframes:
        duration_s = keyframes[-1].time_s
    moov_first = first_mdat_body is None or moov_start < first_mdat_body
    if moov_first:
        head_end, index_range = moov_end, None
    else:
        head_end, index_range = first_mdat_body, (moov_start, moov_end)
    return ContainerIndex(
        container="mp4",
        file_size=file_size,
        duration_s=duration_s,
        keyframes=tuple(keyframes),
        tracks=tuple(tracks),
        subtitle_events=subtitle_events,
        chapters=chapters,
        head_end=head_end,
        index_range=index_range,
    )


def _parse_trak(data: bytes, start: int, end: int) -> dict | None:
    """解出一条轨的编号、类型、编码与样本表（只取刷片需要的几张表）。"""
    track_id = None
    tkhd = _find_box(data, start, end, b"tkhd")
    if tkhd:
        s, _ = tkhd
        offset = s + (20 if _full_box_version(data, s) == 1 else 12)
        track_id = struct.unpack_from(">I", data, offset)[0]
    mdia = _find_box(data, start, end, b"mdia")
    if track_id is None or mdia is None:
        return None
    m_start, m_end = mdia
    timescale = 0
    language = None
    mdhd = _find_box(data, m_start, m_end, b"mdhd")
    if mdhd:
        s, _ = mdhd
        if _full_box_version(data, s) == 1:
            timescale = struct.unpack_from(">I", data, s + 20)[0]
            language = _mp4_language(struct.unpack_from(">H", data, s + 32)[0])
        else:
            timescale = struct.unpack_from(">I", data, s + 12)[0]
            language = _mp4_language(struct.unpack_from(">H", data, s + 20)[0])
    handler = b""
    hdlr = _find_box(data, m_start, m_end, b"hdlr")
    if hdlr:
        handler = data[hdlr[0] + 8 : hdlr[0] + 12]
    minf = _find_box(data, m_start, m_end, b"minf")
    stbl = _find_box(data, minf[0], minf[1], b"stbl") if minf else None
    if stbl is None or not timescale:
        return None
    s_start, s_end = stbl
    tables: dict[bytes, tuple[int, int]] = {}
    for kind, b_start, b_end in _boxes(data, s_start, s_end):
        tables[kind] = (b_start, b_end)
    codec = ""
    if b"stsd" in tables:
        sd = tables[b"stsd"][0]
        if struct.unpack_from(">I", data, sd + 4)[0] > 0:
            codec = data[sd + 12 : sd + 16].decode("latin-1", "replace")
    return {
        "id": track_id,
        "kind": _MP4_HANDLER_KIND.get(handler, KIND_OTHER),
        "codec": codec.strip(),
        "language": language,
        "timescale": timescale,
        "data": data,
        "tables": tables,
    }


def _mp4_sample_times(track: dict) -> list[int]:
    """stts 展开成每个样本的解码时间（以轨道 timescale 计）。"""
    data, tables = track["data"], track["tables"]
    if b"stts" not in tables:
        raise ValueError("缺少 stts")
    s = tables[b"stts"][0]
    count = struct.unpack_from(">I", data, s + 4)[0]
    times: list[int] = []
    now = 0
    for i in range(count):
        n, delta = struct.unpack_from(">II", data, s + 8 + i * 8)
        for _ in range(n):
            times.append(now)
            now += delta
    return times


def _mp4_sample_sizes(track: dict, sample_count: int) -> list[int]:
    data, tables = track["data"], track["tables"]
    if b"stsz" not in tables:
        raise ValueError("缺少 stsz（stz2 暂不支持）")
    s = tables[b"stsz"][0]
    uniform, count = struct.unpack_from(">II", data, s + 4)
    if uniform:
        return [uniform] * count
    return list(struct.unpack_from(f">{count}I", data, s + 12))[:sample_count]


def _mp4_sample_offsets(track: dict, sizes: list[int]) -> list[int]:
    """stsc + stco/co64 展开成每个样本的文件偏移。"""
    data, tables = track["data"], track["tables"]
    if b"stco" in tables:
        s = tables[b"stco"][0]
        n = struct.unpack_from(">I", data, s + 4)[0]
        chunk_offsets = list(struct.unpack_from(f">{n}I", data, s + 8))
    elif b"co64" in tables:
        s = tables[b"co64"][0]
        n = struct.unpack_from(">I", data, s + 4)[0]
        chunk_offsets = list(struct.unpack_from(f">{n}Q", data, s + 8))
    else:
        raise ValueError("缺少 stco/co64")
    if b"stsc" not in tables:
        raise ValueError("缺少 stsc")
    s = tables[b"stsc"][0]
    n = struct.unpack_from(">I", data, s + 4)[0]
    runs = [struct.unpack_from(">III", data, s + 8 + i * 12)[:2] for i in range(n)]
    offsets: list[int] = []
    sample = 0
    for r, (first_chunk, per_chunk) in enumerate(runs):
        last_chunk = runs[r + 1][0] - 1 if r + 1 < len(runs) else len(chunk_offsets)
        for chunk in range(first_chunk, last_chunk + 1):
            if chunk - 1 >= len(chunk_offsets):
                break
            pos = chunk_offsets[chunk - 1]
            for _ in range(per_chunk):
                if sample >= len(sizes):
                    return offsets
                offsets.append(pos)
                pos += sizes[sample]
                sample += 1
    return offsets


def _mp4_keyframes(track: dict) -> list[KeyframePoint]:
    data, tables = track["data"], track["tables"]
    times = _mp4_sample_times(track)
    sizes = _mp4_sample_sizes(track, len(times))
    offsets = _mp4_sample_offsets(track, sizes)
    usable = min(len(times), len(offsets))
    if b"stss" in tables:
        s = tables[b"stss"][0]
        n = struct.unpack_from(">I", data, s + 4)[0]
        sync = [x - 1 for x in struct.unpack_from(f">{n}I", data, s + 8)]
    else:
        sync = list(range(usable))  # 没有 stss 表示每个样本都是关键帧
    scale = track["timescale"]
    return [KeyframePoint(times[i] / scale, offsets[i]) for i in sync if 0 <= i < usable]


def _mp4_subtitle_events(track: dict) -> tuple[float, ...]:
    """文字字幕轨：每个非空样本是一条字幕（mov_text 用 2 字节的空样本清屏）。"""
    times = _mp4_sample_times(track)
    sizes = _mp4_sample_sizes(track, len(times))
    scale = track["timescale"]
    return tuple(times[i] / scale for i in range(min(len(times), len(sizes))) if sizes[i] > 2)


def _mp4_nero_chapters(data: bytes, body_start: int) -> tuple[tuple[float, str | None], ...]:
    """moov/udta/chpl（Nero 章节，网盘/压制组的 MP4 常见）。QuickTime 章节轨不解析。"""
    udta = _find_box(data, body_start, len(data), b"udta")
    if not udta:
        return ()
    chpl = _find_box(data, udta[0], udta[1], b"chpl")
    if not chpl:
        return ()
    s, e = chpl
    version = data[s]
    p = s + 4 + (4 if version else 0)
    count = data[p]
    p += 1
    chapters: list[tuple[float, str | None]] = []
    for _ in range(count):
        if p + 9 > e:
            break
        start = struct.unpack_from(">Q", data, p)[0]
        length = data[p + 8]
        title = data[p + 9 : p + 9 + length].decode("utf-8", "replace") or None
        chapters.append((start / 10_000_000, title))
        p += 9 + length
    return tuple(sorted(chapters, key=lambda c: c[0]))
