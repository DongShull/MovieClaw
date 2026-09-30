"""刷片接口（``/reels``）：抽样、翻页、可见性、放法声明、黑场挪位、事件落库。

容器索引与抓帧都替换成假的（解析本身由 tests/playback/test_container_index.py 覆盖，
挑点规则由 test_reels_picker.py 覆盖），这里只锁接口层的行为。
"""

from __future__ import annotations

import asyncio
import itertools
import json
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.media_probe import VideoColor
from movieclaw_api.services.reels import segments
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    FileSource,
    FileState,
    LibraryFile,
    MediaItem,
    MediaItemPerson,
    MediaMetadata,
    Person,
    PlaybackState,
    ReelEvent,
)
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_playback.container_index import ContainerIndex, KeyframePoint, TrackInfo

_ADMIN = {"username": "admin", "password": "Sup3rSecret!"}


def _fake_index(path: str | Path) -> ContainerIndex | None:
    """一小时的片子，2 秒一个关键帧，1800～1900 秒码率翻倍；文件名带 broken 的读不出。"""
    if "broken" in str(path):
        return None
    points, offset = [], 4096
    for i in range(1800):
        t = i * 2.0
        points.append(KeyframePoint(t, offset))
        offset += 2_000_000 * (2 if 1800 <= t < 1900 else 1)
    return ContainerIndex(
        container="matroska",
        file_size=offset + 100_000,
        duration_s=3600.0,
        keyframes=tuple(points),
        tracks=(
            TrackInfo(1, "video", "V_MPEGH/ISO/HEVC", 0),
            TrackInfo(3, "subtitle", "S_HDMV/PGS", 0, "chi"),
        ),
        subtitle_events={3: ()},
        head_end=4096,
        index_range=(offset, offset + 100_000),
    )


_luma_plan: list[int] = []


def _fake_grab(video: Path, dest: Path, seconds: float, color: VideoColor) -> bool:
    """按 _luma_plan 依次出图（没有计划时出亮图）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    level = _luma_plan.pop(0) if _luma_plan else 128
    Image.new("RGB", (32, 18), (level, level, level)).save(dest, "JPEG")
    return True


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'reels.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    monkeypatch.setenv("MOVIECLAW_REELS_CACHE_DIR", str(tmp_path / "reels-cache"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("TMDB_API_KEY", "test-key-not-used")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    monkeypatch.setattr(segments, "read_container_index", _fake_index)
    monkeypatch.setattr(segments, "grab_frame", _fake_grab)
    monkeypatch.setattr(segments, "video_color_for", lambda *_a, **_k: VideoColor())
    # 抓帧闸是全局共用的模块级信号量，等待过就绑在当时的事件循环上；每个用例一个新的
    monkeypatch.setattr(segments, "FRAME_GRAB_GATE", asyncio.Semaphore(2))
    _luma_plan.clear()

    from movieclaw_api.app import create_app

    with TestClient(create_app()) as c:
        c.post("/api/v1/auth/bootstrap", json=_ADMIN)
        yield c

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


_counter = itertools.count(1)


async def _seed(tmp_path: Path, *, movies: int = 2, episodes: int = 3, extras: bool = True) -> dict:
    n = next(_counter)
    root = tmp_path / f"media{n}"
    root.mkdir(exist_ok=True)

    def _file(item_id, library_id, season, episode, name, container="mkv") -> LibraryFile:
        path = root / name
        path.write_bytes(b"FAKE" * 64)
        return LibraryFile(
            library_id=library_id,
            media_item_id=item_id,
            season_number=season,
            episode_number=episode,
            file_path=str(path),
            size_bytes=path.stat().st_size,
            source=FileSource.SCANNED,
            state=FileState.IN_PLACE,
            duration_seconds=3600,
            container=container,
            resolution="1080p",
            subtitle_streams=[{"codec": "hdmv_pgs_subtitle", "language": "chi", "title": "简体"}],
            audio_streams=[
                {"codec": "truehd", "language": "eng", "default": True},
                {"codec": "ac3", "language": "eng"},
            ],
        )

    async with get_database().session() as session:
        repo = LibraryRepository(session)
        movie_lib = await repo.create(name=f"电影库{n}", kind="movie", root_paths=[str(root)])
        tv_lib = await repo.create(name=f"剧集库{n}", kind="tv", root_paths=[str(root)])
        other_lib = await repo.create(name=f"其他{n}", kind="video", root_paths=[str(root)])
        ids = {"movies": [], "movie_library": movie_lib.id, "tv_library": tv_lib.id}
        for i in range(movies):
            item = MediaItem(
                kind="movie", tmdb_id=10_000 * n + i, title=f"电影{i}", original_title=f"M{i}"
            )
            session.add(item)
            await session.flush()
            session.add(_file(item.id, movie_lib.id, 0, 0, f"movie{i}.mkv"))
            ids["movies"].append(item.id)
        if episodes:
            show = MediaItem(kind="tv", tmdb_id=90_000 + n, title="剧", original_title="S")
            session.add(show)
            await session.flush()
            for e in range(1, episodes + 1):
                session.add(_file(show.id, tv_lib.id, 1, e, f"S01E{e:02d}.mkv"))
            ids["show"] = show.id
        if extras:
            disc = MediaItem(kind="movie", tmdb_id=80_000 + n, title="原盘", original_title="D")
            broken = MediaItem(kind="movie", tmdb_id=70_000 + n, title="坏片", original_title="B")
            clip = MediaItem(kind="video", tmdb_id=60_000 + n, title="个人视频", original_title="V")
            session.add_all([disc, broken, clip])
            await session.flush()
            session.add(_file(disc.id, movie_lib.id, 0, 0, "BDMV", container="bluray"))
            session.add(_file(broken.id, movie_lib.id, 0, 0, "broken.mkv"))
            session.add(_file(clip.id, other_lib.id, 0, 0, "clip.mkv"))
            ids.update(disc=disc.id, broken=broken.id, clip=clip.id)
        await session.commit()
        return ids


def seed(client: TestClient, tmp_path: Path, **kwargs) -> dict:
    return client.portal.call(partial(_seed, tmp_path, **kwargs))  # type: ignore[attr-defined]


def feed(client: TestClient, **params) -> dict:
    resp = client.get("/api/v1/reels", params=params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    return body["data"]


def test_feed_draws_one_item_per_title_from_movie_and_tv_libraries(client, tmp_path):
    ids = seed(client, tmp_path)
    data = feed(client, limit=10)
    got = {item["title"]["media_item_id"] for item in data["items"]}
    # 原盘（一期不支持）、读不出索引的片、非电影 / 剧集库的条目都不出现；剧只出一条
    assert got == {*ids["movies"], ids["show"]}
    assert data["has_more"] is False
    assert isinstance(data["seed"], int)

    item = next(i for i in data["items"] if i["title"]["media_item_id"] == ids["movies"][0])
    assert item["title"]["kind"] == "movie"
    assert item["id"] == f"rl_{item['segment']['file_id']}_{item['segment']['start_ms']}"
    # 1800～1900 秒码率翻倍：挑中这一段
    assert item["segment"]["method"] == "bitrate"
    assert 1785_000 <= item["segment"]["start_ms"] <= 1860_000
    assert 30_000 <= item["segment"]["end_ms"] - item["segment"]["start_ms"] <= 60_000
    play = item["play"]
    assert play["mode"] == "seek"
    assert play["stream_url"].startswith(
        f"/api/v1/playback/files/{item['segment']['file_id']}/stream?token="
    )
    assert [r["purpose"] for r in play["prefetch"]] == ["head", "index", "start"]
    # 默认音轨是 TrueHD：换同语言的 AC3；中文字幕打开
    assert play["audio_ordinal"] == 1
    assert play["subtitle"]["ordinal"] == 0
    assert "/reels/" in item["cover_url"]

    show = next(i for i in data["items"] if i["title"]["media_item_id"] == ids["show"])
    assert show["title"]["kind"] == "tv"
    assert show["title"]["episode"]["season"] == 1


def test_stream_url_in_feed_serves_the_file(client, tmp_path):
    seed(client, tmp_path, episodes=0, extras=False, movies=1)
    item = feed(client)["items"][0]
    resp = client.get(item["play"]["stream_url"], headers={"Range": "bytes=0-3"})
    assert resp.status_code == 206
    assert resp.content == b"FAKE"


def test_same_seed_is_stable_and_pages_do_not_repeat(client, tmp_path):
    ids = seed(client, tmp_path, movies=23, episodes=12, extras=False)
    first = feed(client, limit=10)
    again = feed(client, limit=10, seed=first["seed"])
    assert [i["id"] for i in again["items"]] == [i["id"] for i in first["items"]]

    seen: list[int] = [i["title"]["media_item_id"] for i in first["items"]]
    offset, has_more = first["next_offset"], first["has_more"]
    while has_more:
        page = feed(client, limit=10, seed=first["seed"], offset=offset)
        seen += [i["title"]["media_item_id"] for i in page["items"]]
        offset, has_more = page["next_offset"], page["has_more"]
    assert len(seen) == len(set(seen)) == 24  # 23 部电影 + 1 部剧（12 集只占一个名额）
    assert set(seen) == {*ids["movies"], ids["show"]}


def test_client_without_seek_mode_gets_nothing(client, tmp_path):
    seed(client, tmp_path)
    assert feed(client, modes="clip")["items"] == []


def test_dark_first_frame_moves_start_to_a_later_keyframe(client, tmp_path):
    seed(client, tmp_path, movies=1, episodes=0, extras=False)
    _luma_plan.extend([2, 3, 140])  # 前两次抓到黑场，第三次亮
    item = feed(client)["items"][0]
    start_ms = item["segment"]["start_ms"]
    record = json.loads(next((tmp_path / "reels-cache").glob("*.json")).read_text())
    assert record["segment"]["start_ms"] == start_ms
    covers = list((tmp_path / "metadata" / "images").rglob("*.jpg"))
    # 黑场那两张不留：只剩最终起点的封面
    assert [c.name for c in covers] == [f"{start_ms:010d}.jpg"]
    # 至少挪了两次、每次至少 2 秒
    assert start_ms >= 1785_000 + 4_000


def test_unsupported_file_is_remembered(client, tmp_path):
    ids = seed(client, tmp_path, movies=0, episodes=0)
    assert feed(client)["items"] == []
    records = [json.loads(p.read_text()) for p in (tmp_path / "reels-cache").glob("*.json")]
    assert any("unsupported" in r for r in records)
    assert ids["broken"]


def test_member_only_sees_visible_libraries(client, tmp_path):
    ids = seed(client, tmp_path)
    created = client.post(
        "/api/v1/members",
        json={"username": "family", "password": "family-pass-1", "nickname": "家人"},
    )
    assert created.status_code == 200, created.text
    updated = client.put(
        f"/api/v1/members/{created.json()['data']['id']}",
        json={"all_libraries": False, "library_ids": [ids["tv_library"]]},
    )
    assert updated.status_code == 200, updated.text
    client.post("/api/v1/auth/logout")
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "family", "password": "family-pass-1"}
        ).status_code
        == 200
    )
    got = {i["title"]["media_item_id"] for i in feed(client)["items"]}
    assert got == {ids["show"]}


def test_events_are_recorded_without_touching_watch_history(client, tmp_path):
    seed(client, tmp_path, movies=1, episodes=0, extras=False)
    item = feed(client)["items"][0]
    base = {
        "reel_id": item["id"],
        "media_item_id": item["title"]["media_item_id"],
        "file_id": item["segment"]["file_id"],
    }
    resp = client.post(
        "/api/v1/reels/events",
        json={
            "events": [
                {**base, "kind": "impression"},
                {**base, "kind": "first_frame", "wait_ms": 420},
                {
                    **base,
                    "kind": "leave",
                    "watched_ms": 12_000,
                    "position_ms": item["segment"]["start_ms"] + 12_000,
                },
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"accepted": 3}

    bad = client.post("/api/v1/reels/events", json={"events": [{**base, "kind": "hack"}]})
    assert bad.status_code == 422

    async def rows():
        async with get_database().session() as session:
            events = (await session.execute(select(ReelEvent))).scalars().all()
            states = (await session.execute(select(PlaybackState))).scalars().all()
            return [(e.kind, e.member_id, e.wait_ms, e.watched_ms) for e in events], len(states)

    events, states = client.portal.call(rows)  # type: ignore[attr-defined]
    assert events == [
        ("impression", 0, None, None),
        ("first_frame", 0, 420, None),
        ("leave", 0, None, 12_000),
    ]
    assert states == 0


async def _set_metadata(genres: dict[int, list[str]]) -> None:
    async with get_database().session() as session:
        for item_id, names in genres.items():
            session.add(
                MediaMetadata(
                    media_item_id=item_id,
                    genres=names,
                    overview=f"条目 {item_id} 的简介",
                    runtime_minutes=101,
                )
            )
        await session.commit()


def test_genres_list_and_genre_filter(client, tmp_path):
    ids = seed(client, tmp_path, movies=3, episodes=2, extras=False)
    a, b, c = ids["movies"]
    client.portal.call(  # type: ignore[attr-defined]
        partial(
            _set_metadata, {a: ["剧情", "爱情"], b: ["动作"], c: ["剧情"], ids["show"]: ["剧情"]}
        )
    )
    resp = client.get("/api/v1/reels/genres")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == [
        {"name": "剧情", "count": 3},
        {"name": "动作", "count": 1},
        {"name": "爱情", "count": 1},
    ]
    got = {i["title"]["media_item_id"] for i in feed(client, genre="剧情")["items"]}
    assert got == {a, c, ids["show"]}
    assert feed(client, genre="科幻")["items"] == []


def test_feed_carries_marks_overview_and_runtime(client, tmp_path):
    ids = seed(client, tmp_path, movies=1, episodes=1, extras=False)
    movie = ids["movies"][0]
    client.portal.call(partial(_set_metadata, {movie: ["剧情"]}))  # type: ignore[attr-defined]
    marks = client.post("/api/v1/playback/marks", json={"media_item_id": movie, "favorite": True})
    assert marks.status_code == 200, marks.text
    marks = client.post(
        "/api/v1/playback/marks",
        json={
            "media_item_id": ids["show"],
            "season_number": 1,
            "episode_number": 1,
            "played": True,
        },
    )
    assert marks.status_code == 200, marks.text

    items = {i["title"]["media_item_id"]: i["title"] for i in feed(client)["items"]}
    assert items[movie]["favorite"] is True
    assert items[movie]["played"] is False
    assert items[movie]["overview"] == f"条目 {movie} 的简介"
    assert items[movie]["runtime_minutes"] == 101
    assert items[movie]["library_id"] == ids["movie_library"]
    show = items[ids["show"]]
    assert show["favorite"] is False
    assert show["played"] is True  # 这一集看过了
    assert show["runtime_minutes"] == 60  # 没有分集档案：按文件时长


async def _set_directors(movie_id: int, fallback_id: int) -> None:
    async with get_database().session() as session:
        person = Person(tmdb_person_id=4321, name="姜文", profile_path="/jiangwen.jpg")
        session.add(person)
        await session.flush()
        session.add(
            MediaItemPerson(
                media_item_id=movie_id, person_id=person.id, department="director", credit_order=0
            )
        )
        session.add(MediaMetadata(media_item_id=movie_id, directors=["不该用到的名字"]))
        session.add(MediaMetadata(media_item_id=fallback_id, directors=["甲", "乙", "丙"]))
        await session.commit()


def test_feed_carries_directors_and_film_duration(client, tmp_path):
    ids = seed(client, tmp_path, movies=2, episodes=0, extras=False)
    structured, fallback = ids["movies"]
    client.portal.call(partial(_set_directors, structured, fallback))  # type: ignore[attr-defined]
    items = {i["title"]["media_item_id"]: i for i in feed(client)["items"]}
    # 关系表里有的用关系表（带头像与人物 id）
    [director] = items[structured]["title"]["directors"]
    assert director["name"] == "姜文"
    assert director["tmdb_person_id"] == 4321
    assert director["avatar_url"].endswith("/w185/jiangwen.jpg")
    # 没有关系行的旧条目退回档案里的姓名，最多两位
    assert [d["name"] for d in items[fallback]["title"]["directors"]] == ["甲", "乙"]
    assert items[fallback]["title"]["directors"][0]["tmdb_person_id"] is None
    # 原片总长（台账探测时长）
    assert items[structured]["segment"]["duration_ms"] == 3600 * 1000
