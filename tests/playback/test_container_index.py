"""容器索引读取单测（docs/design/reels.md）。

不依赖 ffmpeg：测试里直接拼出最小的合法 Matroska / MP4 字节结构，锁死「关键帧时间 +
字节位置、字幕事件、章节、文件头与索引范围」这几项解析结果。真实片源的吻合度由
NAS 只读实测验证（见设计文档）。
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from movieclaw_playback import container_index as ci

# --- Matroska 拼装 ----------------------------------------------------------------


def _id(element_id: int) -> bytes:
    return element_id.to_bytes((element_id.bit_length() + 7) // 8, "big")


def _el(element_id: int, payload: bytes) -> bytes:
    # 统一用 8 字节长度（0x01 + 7 字节），长度与内容无关，方便先算布局再填位置
    return _id(element_id) + b"\x01" + len(payload).to_bytes(7, "big") + payload


def _u(element_id: int, value: int, width: int | None = None) -> bytes:
    width = width or max(1, (value.bit_length() + 7) // 8)
    return _el(element_id, value.to_bytes(width, "big"))


def _s(element_id: int, text: str) -> bytes:
    return _el(element_id, text.encode())


def _track(
    number: int, kind: int, codec: str, language: str | None = None, name: str | None = None
):
    body = _u(0xD7, number) + _u(0x83, kind) + _s(0x86, codec)
    if language:
        body += _s(0x22B59C, language)
    if name:
        body += _s(0x536E, name)
    return _el(0xAE, body)


def _build_mkv(
    path: Path,
    *,
    duration_ms: int,
    clusters: list[tuple[int, int]],
    subtitle_ms: list[int],
    chapters: list[tuple[int, str]],
    cues_in_seekhead: bool = True,
    tail_seekhead: bool = False,
) -> dict:
    """clusters: [(簇起始毫秒, 簇正文字节数)]，每个簇开头一个视频关键帧。

    tail_seekhead：Cues 后面再写第二个 SeekHead（记各簇位置），开头的 SeekHead 指向它。
    """
    info = _el(
        0x1549A966, _u(0x2AD7B1, 1_000_000) + _el(0x4489, struct.pack(">d", float(duration_ms)))
    )
    tracks = _el(
        0x1654AE6B,
        _track(1, 1, "V_MPEGH/ISO/HEVC")
        + _track(2, 2, "A_AAC", "eng")
        + _track(3, 17, "S_HDMV/PGS", "chi", "简体"),
    )
    atoms = b"".join(
        _el(0xB6, _u(0x91, ms * 1_000_000) + _el(0x80, _s(0x85, title))) for ms, title in chapters
    )
    chapters_el = _el(0x1043A770, _el(0x45B9, atoms))
    cluster_bytes = [_el(0x1F43B675, _u(0xE7, ms) + b"\x00" * size) for ms, size in clusters]

    def seekhead(pos: dict[int, int]) -> bytes:
        entries = b""
        for element_id in (0x1549A966, 0x1654AE6B, 0x1043A770, 0x1C53BB6B, 0x114D9B74):
            if element_id == 0x1C53BB6B and not cues_in_seekhead:
                continue
            if element_id == 0x114D9B74 and not tail_seekhead:
                continue
            entries += _el(
                0x4DBB, _el(0x53AB, _id(element_id)) + _u(0x53AC, pos.get(element_id, 0), 8)
            )
        return _el(0x114D9B74, entries)

    head_len = len(seekhead({}))
    pos: dict[int, int] = {}
    cursor = head_len
    pos[0x1549A966] = cursor
    cursor += len(info)
    pos[0x1654AE6B] = cursor
    cursor += len(tracks)
    pos[0x1043A770] = cursor
    cursor += len(chapters_el)
    cluster_pos = []
    for c in cluster_bytes:
        cluster_pos.append(cursor)
        cursor += len(c)

    def cluster_of(ms: int) -> int:
        idx = max(i for i, (start, _) in enumerate(clusters) if start <= ms)
        return cluster_pos[idx]

    points = [
        _el(0xBB, _u(0xB3, ms) + _el(0xB7, _u(0xF7, 1) + _u(0xF1, cluster_pos[i], 8)))
        for i, (ms, _) in enumerate(clusters)
    ]
    points += [
        _el(0xBB, _u(0xB3, ms) + _el(0xB7, _u(0xF7, 3) + _u(0xF1, cluster_of(ms), 8)))
        for ms in subtitle_ms
    ]
    cues = _el(0x1C53BB6B, b"".join(points))
    pos[0x1C53BB6B] = cursor
    tail = b""
    if tail_seekhead:
        pos[0x114D9B74] = cursor + len(cues)
        tail = _el(
            0x114D9B74,
            b"".join(
                _el(0x4DBB, _el(0x53AB, _id(0x1F43B675)) + _u(0x53AC, p, 8)) for p in cluster_pos
            ),
        )
    segment_body = (
        seekhead(pos) + info + tracks + chapters_el + b"".join(cluster_bytes) + cues + tail
    )
    ebml = _el(0x1A45DFA3, _s(0x4282, "matroska"))
    segment_header = _id(0x18538067) + b"\x01" + len(segment_body).to_bytes(7, "big")
    path.write_bytes(ebml + segment_header + segment_body)
    base = len(ebml) + len(segment_header)
    return {
        "base": base,
        "cluster_abs": [base + p for p in cluster_pos],
        "cues_abs": (base + pos[0x1C53BB6B], base + pos[0x1C53BB6B] + len(cues)),
        "tail_seekhead_abs": base + pos[0x114D9B74] if tail_seekhead else None,
    }


def _truncate(path: Path, size: int) -> None:
    """模拟下载或复制中断：只留文件前 size 字节，Segment 声明的长度不变。"""
    path.write_bytes(path.read_bytes()[:size])


@pytest.fixture(autouse=True)
def _clear_cache():
    ci._cache.clear()
    yield
    ci._cache.clear()


def test_matroska_keyframes_subtitles_chapters_and_ranges(tmp_path):
    path = tmp_path / "film.mkv"
    layout = _build_mkv(
        path,
        duration_ms=600_000,
        clusters=[(0, 1000), (2000, 3000), (4000, 500)],
        subtitle_ms=[1500, 2500, 4100],
        chapters=[(0, "开场"), (4000, "第二幕")],
    )
    index = ci.read_container_index(path)
    assert index is not None
    assert index.container == "matroska"
    assert index.duration_s == pytest.approx(600.0)
    assert [k.time_s for k in index.keyframes] == [0.0, 2.0, 4.0]
    # 关键帧偏移 = 所在 Cluster 的绝对位置（SeekPosition 相对 Segment 正文起点）
    assert [k.offset for k in index.keyframes] == layout["cluster_abs"]
    assert index.subtitle_events == {3: (1.5, 2.5, 4.1)}
    assert index.chapters == ((0.0, "开场"), (4.0, "第二幕"))
    # 文件头到第一个 Cluster 为止；Cues 在文件尾，单独给出范围
    assert index.head_end == layout["cluster_abs"][0]
    assert index.index_range == layout["cues_abs"]
    kinds = [(t.number, t.kind, t.order, t.language) for t in index.tracks]
    assert kinds == [(1, "video", 0, None), (2, "audio", 0, "eng"), (3, "subtitle", 0, "chi")]


def test_matroska_cues_missing_from_seekhead_is_unsupported(tmp_path):
    """SeekHead 不指向 Cues、Cues 又在簇后面：找不到索引，按合同返回 None。"""
    path = tmp_path / "noindex.mkv"
    _build_mkv(
        path,
        duration_ms=10_000,
        clusters=[(0, 10)],
        subtitle_ms=[],
        chapters=[],
        cues_in_seekhead=False,
    )
    assert ci.read_container_index(path) is None


def test_matroska_truncated_before_cues_is_unsupported_with_clear_reason(tmp_path, caplog):
    """文件尾被截掉、截在最后一个簇中间（NAS 实测《饥饿站台》只剩 64%）：SeekHead 记的
    Cues 在文件末尾之外。按合同返回 None，日志要说清「文件不完整」而不是报越界。"""
    path = tmp_path / "truncated.mkv"
    layout = _build_mkv(
        path,
        duration_ms=10_000,
        clusters=[(0, 1000), (2000, 3000), (4000, 500)],
        subtitle_ms=[1500],
        chapters=[],
    )
    _truncate(path, layout["cluster_abs"][-1] + 100)
    with caplog.at_level("WARNING", logger="movieclaw_playback.container_index"):
        assert ci.read_container_index(path) is None
    [message] = [r.getMessage() for r in caplog.records if "读取容器索引失败" in r.getMessage()]
    assert "文件不完整" in message


@pytest.mark.parametrize("keep", [20, 0], ids=["half-left", "gone"])
def test_matroska_truncated_tail_seekhead_is_skipped(tmp_path, keep):
    """文件尾只缺一小截（NAS 实测《搏击俱乐部》缺尾 900 KB）：Cues 完好，只有写在它后面的
    第二个 SeekHead 读不全或整个没了。SeekHead 只是目录，跳过它照样出完整索引。"""
    path = tmp_path / "tail.mkv"
    layout = _build_mkv(
        path,
        duration_ms=10_000,
        clusters=[(0, 1000), (2000, 3000), (4000, 500)],
        subtitle_ms=[1500, 4100],
        chapters=[(0, "开场")],
        tail_seekhead=True,
    )
    _truncate(path, layout["tail_seekhead_abs"] + keep)
    index = ci.read_container_index(path)
    assert index is not None
    assert [k.offset for k in index.keyframes] == layout["cluster_abs"]
    assert index.subtitle_events == {3: (1.5, 4.1)}
    assert index.index_range == layout["cues_abs"]


def test_garbage_with_mkv_suffix_returns_none(tmp_path):
    path = tmp_path / "fake.mkv"
    path.write_bytes(b"\x00" * 4096)
    assert ci.read_container_index(path) is None


def test_unsupported_container_returns_none(tmp_path):
    path = tmp_path / "clip.ts"
    path.write_bytes(b"G" * 188 * 10)
    assert ci.read_container_index(path) is None


# --- MP4 拼装 ---------------------------------------------------------------------


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _full(kind: bytes, payload: bytes, version: int = 0) -> bytes:
    return _box(kind, bytes([version, 0, 0, 0]) + payload)


def _trak(
    track_id: int,
    handler: bytes,
    codec: bytes,
    timescale: int,
    deltas: list[int],
    sizes: list[int],
    chunk_offsets: list[int],
    per_chunk: int,
    sync: list[int] | None,
) -> bytes:
    tkhd = _full(b"tkhd", struct.pack(">IIII", 0, 0, track_id, 0) + b"\x00" * 64)
    lang = ((ord("c") - 0x60) << 10) | ((ord("h") - 0x60) << 5) | (ord("i") - 0x60)
    mdhd = _full(b"mdhd", struct.pack(">IIIIHH", 0, 0, timescale, sum(deltas), lang, 0))
    hdlr = _full(b"hdlr", struct.pack(">I4s", 0, handler) + b"\x00" * 13)
    stsd = _full(b"stsd", struct.pack(">I", 1) + _box(codec, b"\x00" * 16))
    stts = _full(
        b"stts", struct.pack(">I", len(deltas)) + b"".join(struct.pack(">II", 1, d) for d in deltas)
    )
    stsz = _full(
        b"stsz", struct.pack(">II", 0, len(sizes)) + b"".join(struct.pack(">I", s) for s in sizes)
    )
    stsc = _full(b"stsc", struct.pack(">I", 1) + struct.pack(">III", 1, per_chunk, 1))
    stco = _full(
        b"stco",
        struct.pack(">I", len(chunk_offsets))
        + b"".join(struct.pack(">I", o) for o in chunk_offsets),
    )
    tables = stsd + stts + stsz + stsc + stco
    if sync is not None:
        tables += _full(
            b"stss", struct.pack(">I", len(sync)) + b"".join(struct.pack(">I", s) for s in sync)
        )
    minf = _box(b"minf", _box(b"stbl", tables))
    return _box(b"trak", tkhd + _box(b"mdia", mdhd + hdlr + minf))


def _build_mp4(path: Path, *, moov_first: bool) -> dict:
    """视频：6 个样本（每个 1 秒，第 1、4 个是关键帧），每 3 个样本一块；
    字幕：4 个样本（非空、清屏、非空、清屏），每块 1 个样本。"""
    video_sizes = [5000, 100, 100, 4000, 100, 100]
    sub_sizes = [20, 2, 30, 2]
    ftyp = _box(b"ftyp", b"isom\x00\x00\x02\x00isomavc1")

    def moov(video_chunks: list[int], sub_chunks: list[int]) -> bytes:
        mvhd = _full(b"mvhd", struct.pack(">IIII", 0, 0, 1000, 6000) + b"\x00" * 80)
        video = _trak(1, b"vide", b"hvc1", 1000, [1000] * 6, video_sizes, video_chunks, 3, [1, 4])
        subs = _trak(
            2, b"sbtl", b"tx3g", 1000, [1000, 1500, 1000, 2500], sub_sizes, sub_chunks, 1, None
        )
        chpl = _full(
            b"chpl",
            struct.pack(">I", 0)
            + bytes([2])
            + struct.pack(">QB", 0, 2)
            + b"OP"
            + struct.pack(">QB", 30_000_000, 3)
            + b"ACT",
            version=1,
        )
        return _box(b"moov", mvhd + video + subs + _box(b"udta", chpl))

    moov_len = len(moov([0, 0], [0, 0, 0, 0]))
    media = (
        b"".join(b"\x11" * s for s in video_sizes[:3])
        + b"".join(b"\x22" * s for s in sub_sizes)
        + b"".join(b"\x33" * s for s in video_sizes[3:])
    )
    mdat_header = 8
    mdat_start = len(ftyp) + (moov_len if moov_first else 0)
    body = mdat_start + mdat_header
    v1 = body
    s_chunks = []
    p = v1 + sum(video_sizes[:3])
    for s in sub_sizes:
        s_chunks.append(p)
        p += s
    v2 = p
    moov_bytes = moov([v1, v2], s_chunks)
    mdat = _box(b"mdat", media)
    data = ftyp + (moov_bytes + mdat if moov_first else mdat + moov_bytes)
    path.write_bytes(data)
    moov_start = len(ftyp) if moov_first else len(ftyp) + len(mdat)
    return {
        "keyframe_offsets": [v1, v2],
        "mdat_body": body,
        "moov": (moov_start, moov_start + len(moov_bytes)),
    }


@pytest.mark.parametrize("moov_first", [True, False])
def test_mp4_parsed_from_moov_without_touching_mdat(tmp_path, moov_first):
    path = tmp_path / "episode.mp4"
    layout = _build_mp4(path, moov_first=moov_first)
    index = ci.read_container_index(path)
    assert index is not None
    assert index.container == "mp4"
    assert index.duration_s == pytest.approx(6.0)
    assert [(k.time_s, k.offset) for k in index.keyframes] == [
        (0.0, layout["keyframe_offsets"][0]),
        (3.0, layout["keyframe_offsets"][1]),
    ]
    # 空样本（2 字节）是清屏，不算一条字幕
    assert index.subtitle_events == {2: (0.0, 2.5)}
    assert index.chapters == ((0.0, "OP"), (3.0, "ACT"))
    assert [(t.kind, t.codec, t.language) for t in index.tracks] == [
        ("video", "hvc1", "chi"),
        ("subtitle", "tx3g", "chi"),
    ]
    if moov_first:
        assert index.head_end == layout["moov"][1]
        assert index.index_range is None
    else:
        assert index.head_end == layout["mdat_body"]
        assert index.index_range == layout["moov"]


def test_result_is_cached_by_path_mtime_and_size(tmp_path, monkeypatch):
    path = tmp_path / "episode.mp4"
    _build_mp4(path, moov_first=True)
    first = ci.read_container_index(path)
    monkeypatch.setattr(ci, "_read_mp4", lambda *_: pytest.fail("应命中缓存"))
    assert ci.read_container_index(path) is first
