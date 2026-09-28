"""下载线索（download_hint）的扫描开销守卫：解析按需、查找只随目录深度增长。

线索表只增不删。此前每轮扫描开场把全部线索逐条跑 enrich（含 NER 推理），重启后
第一轮（NER 缓存为空）几千条要在事件循环里连续算好几秒，而绝大多数扫描一条线索
都用不上。这里锁住三件事：开场不解析、嵌套目录取最深的一条、用到时才解析且只
解析一次。
"""

from __future__ import annotations

from pathlib import Path

import pytest_asyncio

import movieclaw_api.services.library.scan as scan_mod
from movieclaw_api.core.config import get_settings
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import DownloadHint


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'hints.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def test_hints_parse_lazily_and_pick_the_deepest_directory(db, monkeypatch) -> None:
    async with db.session() as session:
        session.add_all(
            [
                DownloadHint(save_path="/lib/tv/Show A", subtitle="甲剧 全12集"),
                DownloadHint(save_path="/lib/tv/Show A/Season 1/", subtitle="甲剧第一季 全6集"),
                DownloadHint(save_path="/lib/tv/Show B", subtitle="乙剧 全8集"),
                # 锚到根的线索与旧判定一致：永不命中（否则会波及所有文件）
                DownloadHint(save_path="/", subtitle="根目录线索"),
            ]
        )
        await session.commit()

    parsed: list[str] = []
    real_enrich = scan_mod.enrich

    def counting_enrich(text, *args, **kwargs):
        parsed.append(text)
        return real_enrich(text, *args, **kwargs)

    monkeypatch.setattr(scan_mod, "enrich", counting_enrich)
    async with db.session() as session:
        hints = await scan_mod._load_hints(session)
    assert parsed == [], "开场不应解析任何线索"

    hint = scan_mod._hint_for(Path("/lib/tv/Show A/Season 1/E01.mkv"), hints)
    assert hint is not None and hint.subtitle == "甲剧第一季 全6集", "嵌套目录取最深的一条"
    assert (
        scan_mod._hint_for(Path("/lib/tv/Show A/Specials/SP01.mkv"), hints).subtitle
        == "甲剧 全12集"
    )
    assert scan_mod._hint_for(Path("/lib/tv/Show C/E01.mkv"), hints) is None
    assert scan_mod._hint_for(Path("/lib/tv/Show A"), hints) is None, "线索目录本身不算其下文件"
    assert hint.total_episodes == 6
    assert parsed == []

    _ = hint.alt_title
    _ = hint.alt_title
    assert parsed == ["甲剧第一季 全6集"], "用到时才解析，且同一条只解析一次"
