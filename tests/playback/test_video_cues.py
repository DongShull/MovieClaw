"""MKV 精简索引服务端测试（docs/design/playback-qoe.md §9.12，App 引擎补丁 P58）。

生成走真的子进程（与线上相同的解析代码），锁死三件事：记录只在片源没变时下发、
省得不多的只记一笔不再重算、开会话排队时连下一集一起生成。
"""

from __future__ import annotations

import asyncio

import pytest
from test_container_index import _build_mkv  # 同目录的 MKV 拼装工具

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.playback import video_cues
from movieclaw_db.engine import get_database
from movieclaw_db.models import FileSource, FileState, LibraryFile
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_playback import container_index as ci


@pytest.fixture(autouse=True)
def _cues_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOVIECLAW_PLAYBACK_CUES_CACHE_DIR", str(tmp_path / "cues"))
    get_settings.cache_clear()
    video_cues._pending.clear()
    yield
    video_cues._pending.clear()
    get_settings.cache_clear()


def _film(tmp_path, name: str = "film.mkv"):
    path = tmp_path / name
    _build_mkv(
        path,
        duration_ms=600_000,
        clusters=[(0, 1000), (2000, 3000), (4000, 500)],
        subtitle_ms=[1500, 2500, 4100],
        chapters=[],
    )
    return path


async def test_generated_record_is_served_until_the_source_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(video_cues, "MIN_SAVING_BYTES", 1)
    path = _film(tmp_path)
    assert video_cues.cached(7, str(path)) is None

    await video_cues._generate(7, str(path))
    cues = video_cues.cached(7, str(path))
    expected = ci.build_matroska_video_cues(path)
    assert cues is not None and expected is not None
    assert (cues.cues_offset, cues.data, cues.original_bytes) == (
        expected.cues_offset,
        expected.data,
        expected.original_bytes,
    )

    # 片子被替换（大小或修改时间变了）：记录作废，引擎照常下原索引
    with path.open("ab") as f:
        f.write(b"\x00")
    assert video_cues.cached(7, str(path)) is None


async def test_small_saving_is_recorded_once_and_never_served(tmp_path, monkeypatch):
    """原索引本来就小（字幕轨少）：记一笔「不值得」，不下发，以后也不再起子进程。"""
    path = _film(tmp_path)
    await video_cues._generate(8, str(path))
    assert video_cues.cached(8, str(path)) is None
    assert video_cues._cache_path(8).is_file()

    calls: list[str] = []

    async def counting(file_path: str):
        calls.append(file_path)
        return None

    monkeypatch.setattr(video_cues, "_run_worker", counting)
    await video_cues._generate(8, str(path))
    assert calls == []


async def test_non_matroska_bytes_are_recorded_as_not_applicable(tmp_path):
    path = tmp_path / "fake.mkv"
    path.write_bytes(b"\x00" * 4096)
    await video_cues._generate(9, str(path))
    assert video_cues._cache_path(9).is_file()
    assert video_cues.cached(9, str(path)) is None


async def test_schedule_covers_this_episode_and_every_version_of_the_next(
    tmp_path, monkeypatch, seeded_db
):
    """同季下一集的每个在位版本都生成；回收站里的、隔一集的不管；季末接下一季第一集。"""
    monkeypatch.setattr(video_cues, "MIN_SAVING_BYTES", 1)
    show = seeded_db["show"]
    rows: dict[str, LibraryFile] = {}
    async with get_database().session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(tmp_path)]
        )
        for key, season, episode, state in [
            ("e1", 1, 1, FileState.IN_PLACE),
            ("e2", 1, 2, FileState.IN_PLACE),
            ("e2-4k", 1, 2, FileState.IN_PLACE),
            ("e2-trashed", 1, 2, FileState.TRASHED),
            ("e3", 1, 3, FileState.IN_PLACE),
            ("s2e1", 2, 1, FileState.IN_PLACE),
        ]:
            path = _film(tmp_path, f"{key}.mkv")
            row = LibraryFile(
                library_id=library.id,
                media_item_id=show,
                season_number=season,
                episode_number=episode,
                file_path=str(path),
                size_bytes=path.stat().st_size,
                source=FileSource.SCANNED,
                state=state,
                container="mkv",
            )
            session.add(row)
            rows[key] = row
        await session.commit()
        for row in rows.values():
            await session.refresh(row)

    video_cues.schedule(rows["e1"], delay_s=0)
    await asyncio.gather(*video_cues._running)
    generated = {key for key, row in rows.items() if video_cues._cache_path(row.id).is_file()}
    assert generated == {"e1", "e2", "e2-4k"}
    assert video_cues.cached(rows["e2"].id, rows["e2"].file_path) is not None

    assert await video_cues._next_episode_files(show, 1, 3) == [
        (rows["s2e1"].id, rows["s2e1"].file_path)
    ]
    # 电影的季号、集号都存 0：不查下一集
    assert await video_cues._next_episode_files(seeded_db["movie"], 0, 0) == []
