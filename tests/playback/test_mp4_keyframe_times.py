"""MP4 关键帧呈现时间（服务端 HLS 分片计划用）：读 moov + 抽检 NAL，不读整片。

口径与 ffmpeg 一致才不会让分片编号错位（container_index.py「MP4 关键帧呈现时间」一节）：
时间 = dts + ctts − 编辑表 media_time + 片头空段；stss 只是候选，ffmpeg 的解析器只认
IDR / 恢复点 SEI（H.264）与 IRAP（HEVC），对不上就整份交还 ffprobe。

这里直接拼出最小的 MP4 字节结构锁死这些规则；真实片源的逐帧对照在 NAS 上做过（见设计文档）。
"""

from __future__ import annotations

import struct
from pathlib import Path

from movieclaw_playback import container_index as ci
from movieclaw_playback import keyframes


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _full(kind: bytes, payload: bytes, version: int = 0) -> bytes:
    return _box(kind, bytes([version, 0, 0, 0]) + payload)


def _nal(header: bytes, body: bytes = b"\x88\x84") -> bytes:
    """4 字节长度前缀的一个 NAL。"""
    data = header + body
    return struct.pack(">I", len(data)) + data


# H.264 NAL 头：IDR = 0x65（nal_ref_idc 3、类型 5），非 IDR 片 = 0x41（类型 1），SEI = 0x06
H264_IDR = _nal(b"\x65")
H264_P = _nal(b"\x41")
H264_SEI_RECOVERY = _nal(b"\x06", b"\x06\x02\x80\x00\x80")  # payloadType 6、长度 2
H264_SEI_OTHER = _nal(b"\x06", b"\x05\x02\xaa\xbb\x80")  # payloadType 5（user data）
# HEVC NAL 头两字节：类型在第一字节的第 1～6 位
HEVC_IDR = _nal(bytes([19 << 1, 1]))
HEVC_CRA = _nal(bytes([21 << 1, 1]))
HEVC_TRAIL = _nal(bytes([1 << 1, 1]))


def _sample_entry(codec: bytes) -> bytes:
    fixed = b"\x00" * 78
    if codec in (b"avc1", b"avc3"):
        config = _box(b"avcC", bytes([1, 0x64, 0, 0x28, 0xFF]) + b"\xe0\x00")
    else:
        config = _box(b"hvcC", bytes(21) + bytes([0x03]) + b"\x00")
    return _box(codec, fixed + config)


def _build(
    path: Path,
    samples: list[bytes],
    *,
    codec: bytes = b"avc1",
    timescale: int = 1000,
    delta: int = 1000,
    sync: list[int],
    ctts: list[int] | None = None,
    elst: list[tuple[int, int]] | None = None,
    movie_timescale: int = 1000,
) -> None:
    """一条视频轨，每个样本一块（chunk），moov 在前。sync 是 1 起的样本序号。"""
    ftyp = _box(b"ftyp", b"isom\x00\x00\x02\x00isomavc1")

    def moov(chunk_offsets: list[int]) -> bytes:
        mvhd = _full(
            b"mvhd",
            struct.pack(">IIII", 0, 0, movie_timescale, delta * len(samples)) + b"\x00" * 80,
        )
        tkhd = _full(b"tkhd", struct.pack(">IIII", 0, 0, 1, 0) + b"\x00" * 64)
        mdhd = _full(b"mdhd", struct.pack(">IIIIHH", 0, 0, timescale, delta * len(samples), 0, 0))
        hdlr = _full(b"hdlr", struct.pack(">I4s", 0, b"vide") + b"\x00" * 13)
        stsd = _full(b"stsd", struct.pack(">I", 1) + _sample_entry(codec))
        stts = _full(b"stts", struct.pack(">I", 1) + struct.pack(">II", len(samples), delta))
        stsz = _full(
            b"stsz",
            struct.pack(">II", 0, len(samples))
            + b"".join(struct.pack(">I", len(s)) for s in samples),
        )
        stsc = _full(b"stsc", struct.pack(">I", 1) + struct.pack(">III", 1, 1, 1))
        stco = _full(
            b"stco",
            struct.pack(">I", len(chunk_offsets))
            + b"".join(struct.pack(">I", o) for o in chunk_offsets),
        )
        stss = _full(
            b"stss", struct.pack(">I", len(sync)) + b"".join(struct.pack(">I", s) for s in sync)
        )
        tables = stsd + stts + stsz + stsc + stco + stss
        if ctts is not None:
            tables += _full(
                b"ctts",
                struct.pack(">I", len(ctts)) + b"".join(struct.pack(">Ii", 1, c) for c in ctts),
            )
        minf = _box(b"minf", _box(b"stbl", tables))
        edts = b""
        if elst is not None:
            edts = _box(
                b"edts",
                _full(
                    b"elst",
                    struct.pack(">I", len(elst))
                    + b"".join(struct.pack(">IihH", d, m, 1, 0) for d, m in elst),
                ),
            )
        trak = _box(b"trak", tkhd + edts + _box(b"mdia", mdhd + hdlr + minf))
        return _box(b"moov", mvhd + trak)

    moov_len = len(moov([0] * len(samples)))
    offsets = []
    pos = len(ftyp) + moov_len + 8
    for s in samples:
        offsets.append(pos)
        pos += len(s)
    path.write_bytes(ftyp + moov(offsets) + _box(b"mdat", b"".join(samples)))


def test_h264_idr_keyframes_with_ctts_and_edit_list(tmp_path):
    # 典型的 B 帧 MP4：每个样本带半秒的合成偏移，编辑表 media_time 抵消掉
    path = tmp_path / "movie.mp4"
    samples = [H264_IDR, H264_P, H264_P, H264_IDR, H264_P, H264_P]
    _build(path, samples, sync=[1, 4], ctts=[500] * 6, elst=[(6000, 500)])
    assert ci.read_mp4_keyframe_times(path) == [0.0, 3.0]


def test_negative_ctts_does_not_shift_presentation_time(tmp_path):
    # libavformat 用 dts_shift 把 dts 往前挪，pts 仍是 dts + ctts（片库实测：首帧 pts 0、dts -2000）
    path = tmp_path / "movie.mp4"
    samples = [H264_IDR, H264_P, H264_P, H264_IDR]
    _build(path, samples, sync=[1, 4], ctts=[0, 3000, -2000, 0])
    assert ci.read_mp4_keyframe_times(path) == [0.0, 3.0]


def test_leading_empty_edit_offsets_into_track_timescale(tmp_path):
    # 片头 21 毫秒空段（影片时基 1000）换到轨道时基 11988：av_rescale 四舍五入为 252 刻度
    path = tmp_path / "movie.mp4"
    samples = [H264_IDR, H264_P, H264_IDR]
    _build(
        path,
        samples,
        timescale=11988,
        delta=400,
        sync=[1, 3],
        ctts=[800] * 3,
        elst=[(21, -1), (1200, 800)],
    )
    assert ci.read_mp4_keyframe_times(path) == [252 / 11988, (800 + 252) / 11988]


def test_recovery_point_sei_counts_as_keyframe(tmp_path):
    path = tmp_path / "movie.mp4"
    samples = [H264_IDR, H264_P, H264_SEI_OTHER + H264_SEI_RECOVERY + H264_P]
    _build(path, samples, sync=[1, 3])
    assert ci.read_mp4_keyframe_times(path) == [0.0, 2.0]


def test_non_idr_sync_sample_falls_back(tmp_path):
    # stss 登记了一个普通 I 帧（非 IDR、没有恢复点）：ffmpeg 不会在那里切片，整份交还 ffprobe
    path = tmp_path / "movie.mp4"
    samples = [H264_IDR, H264_P, H264_SEI_OTHER + H264_P, H264_IDR]
    _build(path, samples, sync=[1, 3, 4])
    assert ci.read_mp4_keyframe_times(path) is None


def test_hevc_cra_is_keyframe_but_trailing_picture_is_not(tmp_path):
    path = tmp_path / "movie.mp4"
    _build(path, [HEVC_IDR, HEVC_TRAIL, HEVC_CRA], codec=b"hvc1", sync=[1, 3])
    assert ci.read_mp4_keyframe_times(path) == [0.0, 2.0]
    bad = tmp_path / "bad.mp4"
    _build(bad, [HEVC_IDR, HEVC_TRAIL, HEVC_TRAIL], codec=b"hev1", sync=[1, 3])
    assert ci.read_mp4_keyframe_times(bad) is None


def test_multi_segment_edit_list_is_left_to_ffprobe(tmp_path):
    path = tmp_path / "movie.mp4"
    _build(path, [H264_IDR, H264_P, H264_IDR], sync=[1, 3], elst=[(1000, 0), (1000, 2000)])
    assert ci.read_mp4_keyframe_times(path) is None


def test_trimmed_head_over_one_second_is_left_to_ffprobe(tmp_path):
    # 编辑表从第 2.5 秒开始（裁掉了片头）：libavformat 往回找起始关键帧的规则交给 ffprobe
    path = tmp_path / "movie.mp4"
    samples = [H264_IDR, H264_IDR, H264_P, H264_IDR, H264_P]
    _build(path, samples, sync=[1, 2, 4], elst=[(5000, 2500)])
    assert ci.read_mp4_keyframe_times(path) is None


def test_keyframe_index_uses_moov_without_ffprobe(tmp_path, monkeypatch):
    path = tmp_path / "movie.mp4"
    _build(path, [H264_IDR, H264_P, H264_IDR, H264_P], sync=[1, 3])
    monkeypatch.setattr(
        keyframes,
        "_ffprobe_keyframes",
        lambda _p: (_ for _ in ()).throw(AssertionError("不该 ffprobe")),
    )
    keyframes._index_cache.clear()
    index = keyframes.read_keyframe_index(path)
    assert index is not None and index.times_s == (0.0, 2.0)


def test_keyframe_index_falls_back_to_ffprobe_unless_fast_only(tmp_path, monkeypatch):
    path = tmp_path / "movie.mp4"
    _build(path, [H264_IDR, H264_P, H264_SEI_OTHER + H264_P], sync=[1, 3])
    calls = []
    monkeypatch.setattr(
        keyframes, "_ffprobe_keyframes", lambda p, **_kw: calls.append(p) or [0.0, 4.0]
    )
    keyframes._index_cache.clear()
    # 决策阶段只走快路径：要 ffprobe 通读才拿得到时返回 None，交给三段采样
    assert keyframes.read_keyframe_index(path, allow_ffprobe=False) is None
    assert calls == []
    index = keyframes.read_keyframe_index(path)
    assert index is not None and index.times_s == (0.0, 4.0)
    assert len(calls) == 1


def test_sparse_bad_sync_sample_escapes_sampling_but_not_full_check(tmp_path, monkeypatch):
    # 片库实测：2486 个关键帧里混了 2 个 ffmpeg 不认的帧，抽检没抽中。全量核对要能抓住
    path = tmp_path / "movie.mp4"
    samples = [H264_IDR, H264_IDR, H264_SEI_OTHER + H264_P, H264_IDR, H264_IDR]
    _build(path, samples, sync=[1, 2, 3, 4, 5])
    monkeypatch.setattr(ci, "_VERIFY_MAX", 2)  # 只抽首尾两个，正好躲过第 3 个
    times, sampled = ci.read_mp4_keyframes_checked(path)
    assert times == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert sampled is True
    assert ci.verify_all_mp4_keyframes(path) is False


def test_full_check_marks_file_untrusted_and_rebuilds_with_ffprobe(tmp_path, monkeypatch):
    import asyncio

    path = tmp_path / "movie.mp4"
    _build(path, [H264_IDR, H264_IDR, H264_SEI_OTHER + H264_P, H264_IDR], sync=[1, 2, 3, 4])
    monkeypatch.setattr(ci, "_VERIFY_MAX", 2)
    monkeypatch.setattr(keyframes, "_ffprobe_keyframes", lambda _p, **_kw: [0.0, 1.0, 3.0])
    for state in (keyframes._index_cache, keyframes._needs_full_check, keyframes._untrusted_mp4):
        state.clear()

    first = keyframes.read_keyframe_index(path)
    assert first is not None and first.times_s == (0.0, 1.0, 2.0, 3.0)  # 抽检版（含坏条目）

    async def run() -> None:
        keyframes.schedule_background_index(path, delay_s=0)
        await asyncio.gather(*keyframes._full_check_tasks)

    asyncio.run(run())
    # 下一次开会话拿到的是 ffprobe 重建的正确索引；清了缓存也不再走快路径
    assert keyframes.read_keyframe_index(path).times_s == (0.0, 1.0, 3.0)
    keyframes._index_cache.clear()
    assert keyframes.read_keyframe_index(path).times_s == (0.0, 1.0, 3.0)


def test_full_check_skipped_when_every_sync_sample_was_verified(tmp_path):
    path = tmp_path / "movie.mp4"
    _build(path, [H264_IDR, H264_P, H264_IDR], sync=[1, 3])
    for state in (keyframes._index_cache, keyframes._needs_full_check, keyframes._untrusted_mp4):
        state.clear()
    assert keyframes.read_keyframe_index(path) is not None
    assert keyframes._needs_full_check == set()


def test_edit_list_aligns_first_presented_frame_to_zero(tmp_path):
    # 编辑表 media_time 为 0、首帧还带一帧的合成偏移：libavformat 处理完编辑表把整条轨往前挪到首帧
    # pts 为 0（片库实测：ffprobe 首包 pts 0、dts -1200）
    path = tmp_path / "movie.mp4"
    _build(
        path, [H264_IDR, H264_P, H264_IDR, H264_P], sync=[1, 3], ctts=[1000] * 4, elst=[(4000, 0)]
    )
    assert ci.read_mp4_keyframe_times(path) == [0.0, 2.0]


def test_edit_start_inside_first_frame_shifts_to_next_kept_frame(tmp_path):
    # 片库实测（timescale 1200000）：首帧时长 48000、编辑表 media_time 40039——首帧在编辑起点
    # 之前被丢弃，第二帧 pts 7961 是最早保留的帧，整条轨再挪 7961：首帧 -48000（只用于解码）、
    # 第二帧 0
    path = tmp_path / "movie.mp4"
    samples = [H264_IDR, H264_P, H264_IDR]
    _build(path, samples, timescale=1_200_000, delta=48_000, sync=[1, 3], elst=[(3000, 40_039)])
    assert ci.read_mp4_keyframe_times(path) == [-48_000 / 1_200_000, 48_000 / 1_200_000]


def _reset_background_state():
    for state in (
        keyframes._index_cache,
        keyframes._needs_full_check,
        keyframes._untrusted_mp4,
        keyframes._background_failed,
    ):
        state.clear()


def _run_background(path):
    import asyncio

    async def run() -> None:
        keyframes.schedule_background_index(path, delay_s=0)
        await asyncio.gather(*keyframes._full_check_tasks)

    asyncio.run(run())


def test_ts_index_is_built_in_background_instead_of_on_the_session_path(tmp_path, monkeypatch):
    # 没有索引可读的容器：开会话只走快路径拿不到（这次走会话相对模式，不再当场通读 120 秒），
    # 后台低优先级通读，算好的索引给下一次播放
    path = tmp_path / "movie.ts"
    path.write_bytes(b"\x47" * 188)
    calls = []

    def fake_ffprobe(p, **kw):
        calls.append(kw)
        return [0.5, 4.0, 8.0]

    monkeypatch.setattr(keyframes, "_ffprobe_keyframes", fake_ffprobe)
    _reset_background_state()
    assert keyframes.read_keyframe_index(path, allow_ffprobe=False) is None
    assert calls == []
    _run_background(path)
    assert calls == [{"timeout": keyframes._BACKGROUND_FFPROBE_TIMEOUT, "low_priority": True}]
    index = keyframes.read_keyframe_index(path, allow_ffprobe=False)
    assert index is not None and index.times_s == (0.0, 0.5, 4.0, 8.0)
    # 已经有了就不再排
    _run_background(path)
    assert len(calls) == 1


def test_background_index_failure_is_not_retried_and_huge_files_are_skipped(tmp_path, monkeypatch):
    path = tmp_path / "movie.ts"
    path.write_bytes(b"\x47" * 188)
    calls = []
    monkeypatch.setattr(keyframes, "_ffprobe_keyframes", lambda p, **kw: calls.append(p) or [])
    _reset_background_state()
    _run_background(path)
    _run_background(path)
    assert len(calls) == 1  # 超时 / 读不出：本进程内不再白读
    big = tmp_path / "big.ts"
    big.write_bytes(b"\x47" * 188)
    monkeypatch.setattr(keyframes, "BACKGROUND_INDEX_MAX_BYTES", 100)
    _run_background(big)
    assert len(calls) == 1  # 太大的不通读，留在会话相对模式
