"""刷片挑点规则单测（docs/design/reels.md）。

用人造的容器索引锁死规则：只在不剧透区间里挑、码率有起伏取最高窗口、信号平退回章节、
起止点落在对白空隙、预取范围覆盖文件头 / 索引 / 起点后约 4 秒。
"""

from __future__ import annotations

import pytest

from movieclaw_api.services.reels.picker import (
    INFORMATIVE_SPREAD,
    MAX_S,
    MIN_S,
    pick_segment,
)
from movieclaw_playback.container_index import ContainerIndex, KeyframePoint


def _index(
    duration: float,
    *,
    rate=lambda t: 1_000_000,
    gop: float = 2.0,
    chapters: tuple = (),
    head_end: int = 4096,
    index_range=(10**10, 10**10 + 50_000),
    container: str = "matroska",
) -> ContainerIndex:
    """每 gop 秒一个关键帧，字节位置按 rate(t)（字节/秒）累加。"""
    points = []
    offset = head_end
    t = 0.0
    while t < duration:
        points.append(KeyframePoint(round(t, 3), offset))
        offset += int(rate(t) * gop)
        t += gop
    return ContainerIndex(
        container=container,
        file_size=offset + 60_000 + 10**10,
        duration_s=duration,
        keyframes=tuple(points),
        tracks=(),
        chapters=chapters,
        head_end=head_end,
        index_range=index_range,
    )


def test_bitrate_peak_inside_region_wins():
    """100 分钟的片子，2000～2100 秒码率翻倍：挑中这一段。"""
    index = _index(6000, rate=lambda t: 2_000_000 if 2000 <= t < 2100 else 1_000_000)
    pick = pick_segment(index, kind="movie")
    assert pick is not None
    assert pick.method == "bitrate"
    assert 1985 <= pick.start_s <= 2060
    assert pick.score > 1 + INFORMATIVE_SPREAD
    assert MIN_S <= pick.end_s - pick.start_s <= MAX_S


def test_peak_in_climax_is_never_picked_for_movies():
    """码率最高的是 90% 处的高潮：电影只在 5%～75% 里挑，不剧透。"""
    index = _index(
        6000,
        rate=lambda t: (
            3_000_000 if 5400 <= t < 5500 else (1_500_000 if 1000 <= t < 1100 else 1_000_000)
        ),
    )
    pick = pick_segment(index, kind="movie")
    assert pick is not None
    assert pick.start_s <= 6000 * 0.75
    assert 985 <= pick.start_s <= 1060


def test_episode_region_skips_opening_and_ending():
    """剧集取 10%～80%：片头曲处（前 5%）码率再高也不挑。"""
    index = _index(
        2400,
        rate=lambda t: 3_000_000 if t < 120 else (1_300_000 if 1200 <= t < 1260 else 1_000_000),
    )
    pick = pick_segment(index, kind="episode")
    assert pick is not None
    assert pick.start_s >= 2400 * 0.10
    assert 1185 <= pick.start_s <= 1230


def test_flat_bitrate_falls_back_to_chapter():
    """恒定码率（网络平台的剧集常见）：不信码率，取区间前三分之一附近的章节起点。"""
    chapters = ((0.0, None), (600.0, None), (1000.0, None), (1800.0, None))
    index = _index(3600, chapters=chapters)
    pick = pick_segment(index, kind="episode")
    assert pick is not None
    assert pick.method == "chapter"
    # 区间 360～2880，前三分之一处约 1185：最近的章节是 1000
    assert pick.start_s == 1000.0


def test_flat_bitrate_without_chapters_uses_fixed_position():
    index = _index(3600, chapters=())
    pick = pick_segment(index, kind="episode")
    assert pick is not None
    assert pick.method == "position"
    assert 1160 <= pick.start_s <= 1200


def test_start_lands_in_dialogue_gap_and_end_too():
    """台词每 2 秒一句（最后一句按 3 秒算完），只在 2048 之后到 2062、2088 之后到 2102
    有停顿：起止点都落在停顿里——起点的停顿比默认搜索范围更靠后，靠放宽搜索找到。"""
    index = _index(6000, rate=lambda t: 2_000_000 if 2040 <= t < 2100 else 1_000_000)
    speech = [t for t in range(1900, 2200, 2) if not (2050 <= t < 2062 or 2090 <= t < 2102)]
    pick = pick_segment(index, kind="movie", speech_events=speech)
    assert pick is not None
    assert 2048 + 3 <= pick.start_s < 2062
    assert 2088 + 3 <= pick.end_s < 2102


def test_prefetch_covers_head_index_and_four_seconds_after_start():
    index = _index(6000, rate=lambda t: 2_000_000 if 2000 <= t < 2100 else 1_000_000)
    pick = pick_segment(index, kind="movie")
    assert pick is not None
    ranges = {r.purpose: r for r in pick.prefetch}
    assert (ranges["head"].offset, ranges["head"].length) == (0, 4096)
    assert ranges["index"].offset == 10**10 and ranges["index"].length == 50_000
    start_point = next(k for k in index.keyframes if k.time_s == pick.start_s)
    assert ranges["start"].offset == start_point.offset
    # 起点后正好 4 秒（码率 2 MB/s → 8 MB）再加 1 MiB 余量
    assert ranges["start"].length == pytest.approx(8_000_000 + (1 << 20), rel=0.01)


def test_long_gop_prefetch_interpolates_instead_of_taking_next_keyframe():
    """10 秒一个关键帧：只取到第 4 秒的插值位置，不多带整整一个 GOP。"""
    index = _index(6000, rate=lambda t: 2_000_000 if 2000 <= t < 2100 else 1_000_000, gop=10.0)
    pick = pick_segment(index, kind="movie")
    assert pick is not None
    start = next(r for r in pick.prefetch if r.purpose == "start")
    i = next(n for n, k in enumerate(index.keyframes) if k.time_s == pick.start_s)
    gop_bytes = index.keyframes[i + 1].offset - index.keyframes[i].offset
    # 第 4 秒在这个 10 秒 GOP 的 40% 处
    assert start.length == pytest.approx(0.4 * gop_bytes + (1 << 20), rel=0.01)
    assert start.length < gop_bytes


def test_mp4_start_range_reaches_back_for_interleaved_audio():
    index = _index(
        6000, rate=lambda t: 2_000_000 if 2000 <= t < 2100 else 1_000_000, container="mp4"
    )
    pick = pick_segment(index, kind="movie")
    assert pick is not None
    start = next(r for r in pick.prefetch if r.purpose == "start")
    start_point = next(k for k in index.keyframes if k.time_s == pick.start_s)
    assert start.offset < start_point.offset


def test_index_inside_head_is_not_fetched_twice():
    index = _index(6000, index_range=None)
    pick = pick_segment(index, kind="movie")
    assert pick is not None
    assert [r.purpose for r in pick.prefetch] == ["head", "start"]


def test_too_short_returns_none():
    assert pick_segment(_index(20), kind="movie") is None


def test_short_clip_uses_whole_duration():
    """十来分钟以内放不下区间窗口时放宽到整片，照样出一段。"""
    pick = pick_segment(_index(80), kind="episode")
    assert pick is not None
    assert pick.start_s >= 0
    assert pick.end_s <= 80
