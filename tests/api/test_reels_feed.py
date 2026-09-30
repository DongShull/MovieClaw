"""刷片信息流单测：剧集固定放哪一集、一页凑够就返回（docs/design/reels.md §4）。"""

from __future__ import annotations

import asyncio
import time

import pytest

from movieclaw_api.services.reels import feed, segments
from movieclaw_api.services.reels.feed import ReelCandidate, choose_file
from movieclaw_api.services.reels.segments import ReelSegment
from movieclaw_db.models import LibraryFile


def _file(
    file_id: int,
    season: int = 1,
    episode: int = 1,
    *,
    resolution: str = "1080p",
    size: int = 1000,
    duration: int = 2400,
) -> LibraryFile:
    return LibraryFile(
        id=file_id,
        library_id=1,
        media_item_id=file_id,
        season_number=season,
        episode_number=episode,
        file_path=f"/media/{file_id}.mkv",
        size_bytes=size,
        duration_seconds=duration,
        container="mkv",
        resolution=resolution,
    )


# --- 剧集放哪一集 ------------------------------------------------------------------


def test_episode_picks_second_episode_of_earliest_regular_season():
    files = [
        _file(1, season=0, episode=2),  # 特别篇
        _file(2, season=1, episode=1),
        _file(3, season=1, episode=2, resolution="720p", size=100),
        _file(4, season=1, episode=2, resolution="2160p", size=9000),
        _file(5, season=1, episode=2, resolution="1080p", size=3000),
        _file(6, season=1, episode=3),
        _file(7, season=2, episode=2),
    ]
    # 第二集有三个版本：取 1080p 及以上里最小的
    assert choose_file(files, "episode").id == 5  # type: ignore[union-attr]


def test_episode_without_second_prefers_the_later_neighbour():
    files = [_file(1, episode=1), _file(3, episode=3), _file(4, episode=4)]
    assert choose_file(files, "episode").id == 3  # type: ignore[union-attr]
    assert choose_file([_file(1, episode=1)], "episode").id == 1  # type: ignore[union-attr]


def test_episode_skips_too_short_files_and_falls_back_to_specials():
    trailer = _file(1, episode=2, duration=60)
    assert choose_file([trailer, _file(2, episode=5)], "episode").id == 2  # type: ignore[union-attr]
    special = _file(3, season=0, episode=1)
    assert choose_file([special], "episode").id == 3  # type: ignore[union-attr]
    assert choose_file([trailer], "episode") is None


# --- 一页凑够就返回 ----------------------------------------------------------------


def _segment(file_id: int) -> ReelSegment:
    return ReelSegment(file_id, 1000, 46_000, "bitrate", 1.0, (), None)


@pytest.fixture(autouse=True)
async def _cancel_background():
    """提前返回后还在跑的假计算（很慢的那些）用例结束时收掉。"""
    yield
    for task in list(feed._warm_tasks):
        task.cancel()


def _patch(monkeypatch, *, cached: set[int], delays: dict[int, float]) -> list[int]:
    """cached 里的文件现成；其余按 delays 延时算完（没写的视为很慢）。返回现算过的文件。"""
    computed: list[int] = []

    def fake_cached(ref):
        return _segment(ref.id) if ref.id in cached else segments._MISS

    async def fake_get(ref):
        await asyncio.sleep(delays.get(ref.id, 30.0))
        computed.append(ref.id)
        return _segment(ref.id)

    monkeypatch.setattr(feed, "cached_segment", fake_cached)
    monkeypatch.setattr(feed, "get_segment", fake_get)
    return computed


async def test_returns_at_once_when_cached_segments_fill_the_page(monkeypatch):
    candidates = [ReelCandidate(i, "movie", _file(i)) for i in range(1, 11)]
    _patch(monkeypatch, cached={1, 3, 4, 6, 8, 9}, delays={})
    started = time.perf_counter()
    ready = await feed._segments_within_budget(candidates, limit=4)
    assert time.perf_counter() - started < 0.5  # 以前要等满 PAGE_BUDGET_S
    # 按抽样顺序取现成的；没算过的 2、5 这一页跳过
    assert [c.media_item_id for c in ready] == [1, 3, 4, 6]


async def test_waits_only_for_the_earlier_titles_in_order(monkeypatch):
    candidates = [ReelCandidate(i, "movie", _file(i)) for i in range(1, 6)]
    # 1 现成、2 要现算 0.2 秒；3、4、5 很慢——凑满 2 条只需要等 2
    computed = _patch(monkeypatch, cached={1}, delays={2: 0.2})
    started = time.perf_counter()
    ready = await feed._segments_within_budget(candidates, limit=2)
    assert time.perf_counter() - started < 1.0
    assert [c.media_item_id for c in ready] == [1, 2]
    assert computed == [2]


# --- 按评分加权洗牌 ----------------------------------------------------------------


def _first_share(ratings: dict, target: int, pool_size: int, seeds: int = 2000) -> float:
    """target 排在第一位的比例（多个种子上统计）。"""
    pool = [(i, "movie") for i in range(1, pool_size + 1)]
    return sum(feed.weighted_order(pool, ratings, s)[0][0] == target for s in range(seeds)) / seeds


def test_weighted_order_is_a_stable_permutation():
    pool = [(i, "movie") for i in range(1, 51)]
    ratings = {i: (5.0 + i % 5, 1000) for i in range(1, 51)}
    order = feed.weighted_order(pool, ratings, 42)
    assert sorted(order) == pool
    assert feed.weighted_order(pool, ratings, 42) == order
    assert feed.weighted_order(pool, ratings, 43) != order


def test_higher_rating_comes_first_more_often_but_not_always():
    # 1 号 9 分，其余 7 分，评分人数都很多：1 号排第一的机会明显高于均匀的 1/10
    ratings = {1: (9.0, 5000), **{i: (7.0, 5000) for i in range(2, 11)}}
    share = _first_share(ratings, 1, 10)
    assert 0.35 < share < 0.75
    # 低分片不会被埋没：1 号 5 分时仍有机会排第一
    ratings[1] = (5.0, 5000)
    assert 0.003 < _first_share(ratings, 1, 10) < 0.06


def test_few_votes_are_pulled_toward_the_pool_mean():
    # 1 号 9.5 分但只有 3 人评：几乎按均分对待，排第一的机会接近均匀的 1/10
    few = {1: (9.5, 3), **{i: (7.0, 5000) for i in range(2, 11)}}
    assert _first_share(few, 1, 10) < 0.2
    # 没有评分的按均分算
    unrated = {i: (7.0, 5000) for i in range(2, 11)}
    assert 0.05 < _first_share(unrated, 1, 10) < 0.16


# --- 第一条起得快 ------------------------------------------------------------------


def _sized(item_id: int, megabytes: float) -> ReelCandidate:
    from movieclaw_api.services.reels.picker import ByteRange

    size = int(megabytes * (1 << 20))
    segment = ReelSegment(item_id, 0, 45_000, "bitrate", 1.0, (ByteRange(0, size, "start"),), None)
    return ReelCandidate(item_id, "movie", _file(item_id), segment)


def test_quick_first_moves_the_first_light_item_to_the_front():
    page = [_sized(1, 30), _sized(2, 20), _sized(3, 8), _sized(4, 5)]
    # 第一个不超过 12 MB 的是 3（不是最小的 4）：只动第一条，其余照原顺序
    assert [c.media_item_id for c in feed._quick_first(page)] == [3, 1, 2, 4]
    # 都超过就挑最小的
    heavy = [_sized(1, 30), _sized(2, 20), _sized(3, 25)]
    assert [c.media_item_id for c in feed._quick_first(heavy)] == [2, 1, 3]
    # 第一条本来就轻：顺序不变
    light = [_sized(1, 4), _sized(2, 30)]
    assert [c.media_item_id for c in feed._quick_first(light)] == [1, 2]


def test_preread_range_reads_up_to_the_end_of_file(tmp_path):
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"x" * 3000)
    feed._preread_range(str(path), 1000, 10_000)  # 越过文件尾：读到尾就停，不报错
    feed._preread_range(str(path), 5000, 100)  # 整段在文件尾之后


async def test_preread_in_background_survives_missing_files(tmp_path):
    missing = _sized(1, 1)
    missing.file.file_path = str(tmp_path / "gone.mkv")
    feed._preread_in_background([missing])
    await asyncio.gather(*list(feed._warm_tasks))  # 失败只记日志，任务正常结束
