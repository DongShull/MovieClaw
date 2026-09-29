"""存量轨记忆订正迁移（alembic f4823bbbae60）：只清「清了之后什么都不变」的那部分。

旧口径下播放器每次上报的「正在放的轨」都被记成了记忆，其中多数就是默认挑选。迁移把
这类值置回 NULL；判断全按迁移里冻结的旧规则，条件是换成 NULL 后旧代码放出来的结果
完全一样——所以回退版本不受影响。这里逐一测「该清的清了、不该清的没动」。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest_asyncio
from sqlalchemy import select, text

from movieclaw_api.core.config import get_settings
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, FileState, LibraryFile, MediaItem, PlaybackState
from movieclaw_db.repositories.library_repo import LibraryRepository

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic/versions/20260929_2200_f4823bbbae60_track_memory_only_user_choices.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("track_memory_cleanup", _MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cleanup.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


_AUDIO = [
    {"codec": "truehd", "channels": 8, "language": "eng", "default": True},
    {"codec": "ac3", "channels": 6, "language": "chi", "default": False},
]
_SUBS = [
    {"codec": "subrip", "language": "eng", "default": False, "forced": False},
    {"codec": "subrip", "language": "chi", "default": True, "forced": False},
]
_NO_DEFAULT_SUBS = [{"codec": "subrip", "language": "eng", "default": False, "forced": False}]


async def _run_cleanup(db) -> int:
    migration = _load_migration()
    async with db.session() as session:
        cleaned = await session.run_sync(
            lambda sync: migration.clean_automatic_track_memory(sync.connection())
        )
        await session.commit()
    return cleaned


async def _seed(
    db, files_by_item: dict[str, list[dict]], states: dict[str, tuple]
) -> dict[str, int]:
    """按名字建条目、在位文件与观看记录，返回 名字 → 条目 id。"""
    ids: dict[str, int] = {}
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/m"]
        )
        for n, (name, files) in enumerate(files_by_item.items(), start=1):
            item = MediaItem(kind="movie", tmdb_id=n, title=name, original_title=name)
            session.add(item)
            await session.flush()
            assert item.id is not None
            ids[name] = item.id
            for k, fields in enumerate(files):
                session.add(
                    LibraryFile(
                        library_id=library.id,
                        media_item_id=item.id,
                        file_path=f"/m/{name}-{k}.mkv",
                        size_bytes=1,
                        source=FileSource.SCANNED,
                        state=FileState.IN_PLACE,
                        **{"container": "mkv", **fields},
                    )
                )
        for name, (audio, subtitle) in states.items():
            session.add(PlaybackState(member_id=1, media_item_id=ids[name],
                                      audio_track=audio, subtitle_track=subtitle))
        await session.commit()
    return ids


async def _tracks(db, ids: dict[str, int]) -> dict[str, tuple]:
    async with db.session() as session:
        rows = (await session.execute(select(PlaybackState))).scalars().all()
        by_item = {row.media_item_id: (row.audio_track, row.subtitle_track) for row in rows}
    return {name: by_item[item_id] for name, item_id in ids.items() if item_id in by_item}


async def test_cleans_exactly_the_automatic_choices(db):
    probed = {"audio_streams": _AUDIO, "subtitle_streams": _SUBS, "external_subtitles": []}
    ids = await _seed(
        db,
        {
            "默认挑选": [probed],
            "用户换过": [probed],
            "没有默认字幕": [{**probed, "subtitle_streams": _NO_DEFAULT_SUBS}],
            "外挂默认": [{**probed, "external_subtitles": [
                {"filename": "x.chs.srt", "format": "srt", "language": "chi", "title": None,
                 "forced": False}]}],
            "原盘": [{**probed, "container": "bluray"}],
            "没探测字幕": [{**probed, "subtitle_streams": None}],
            "失效引用": [probed],
            "没有文件": [],
        },
        {
            "默认挑选": ("embedded:0", "embedded:1"),
            "用户换过": ("embedded:1", "off"),
            "没有默认字幕": (None, "off"),
            "外挂默认": (None, "external:x.chs.srt"),
            "原盘": ("embedded:0", "embedded:1"),
            "没探测字幕": (None, "off"),
            "失效引用": ("embedded:9", "embedded:9"),
            "没有文件": ("embedded:0", "off"),
        },
    )
    assert await _run_cleanup(db) == 4
    assert await _tracks(db, ids) == {
        "默认挑选": (None, None),
        "用户换过": ("embedded:1", "off"),  # 和默认不同：用户的选择，原样保留
        "没有默认字幕": (None, None),  # 本来就不开字幕：「关闭」只是默认状态
        "外挂默认": (None, None),  # 装了外挂就默认选外挂
        "原盘": ("embedded:0", "embedded:1"),  # 盘内轨以引擎为准，记忆对它是真值
        "没探测字幕": (None, "off"),  # 引擎可能自己读出带默认旗标的内封字幕
        "失效引用": (None, None),  # 旧代码对失效引用本来就回落默认
        "没有文件": ("embedded:0", "off"),  # 判断不了
    }


async def test_multi_version_is_cleaned_only_when_every_version_agrees(db):
    """多版本：记着的轨在每个有它的版本里都是默认挑选才清；有一个版本里它不是默认，
    旧代码在那个版本上就会照记忆放，清了会变，所以不动。"""
    en_default = {"audio_streams": _AUDIO}
    zh_default = {"audio_streams": [
        {"codec": "aac", "channels": 2, "language": "eng", "default": False},
        {"codec": "aac", "channels": 2, "language": "chi", "default": True},
    ]}
    single_track = {"audio_streams": [{"codec": "aac", "channels": 2, "default": True}]}
    ids = await _seed(
        db,
        {"版本不一致": [en_default, zh_default], "另一版没这条轨": [zh_default, single_track]},
        {"版本不一致": ("embedded:1", None), "另一版没这条轨": ("embedded:1", None)},
    )
    assert await _run_cleanup(db) == 1
    assert await _tracks(db, ids) == {
        "版本不一致": ("embedded:1", None),
        "另一版没这条轨": (None, None),
    }


async def test_unreadable_rows_never_break_startup(db):
    """迁移随应用启动执行：轨信息是坏 JSON 的行当「判断不了」，不能抛异常让应用起不来。"""
    ids = await _seed(db, {"坏数据": [{"audio_streams": _AUDIO, "subtitle_streams": _SUBS}]},
                      {"坏数据": (None, "off")})
    async with db.session() as session:
        await session.execute(text("UPDATE library_file SET subtitle_streams = '{broken'"))
        await session.commit()
    assert await _run_cleanup(db) == 0
    assert await _tracks(db, ids) == {"坏数据": (None, "off")}


async def test_nothing_to_clean_is_a_no_op(db):
    assert await _run_cleanup(db) == 0
