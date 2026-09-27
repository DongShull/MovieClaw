"""site_torrent.match_text（身份匹配检索文本）与行的源字段始终一致。

发布预测的 SQL 预筛只有在检索文本与行的最终状态一致时才不会漏：新建、合并刷新
（副标题只补空、attrs 覆盖写）、富化回填，每条写入路径都要跟着重算；迁移里冻结的
存量回填口径必须与运行期口径一致。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services import enrich_backfill
from movieclaw_api.services.torrent_match_text import torrent_match_text
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import SiteTorrent, TorrentSource
from movieclaw_db.repositories.torrent_repo import TorrentObservation, TorrentRepository

_MIGRATION = next(
    (Path(__file__).resolve().parents[2] / "alembic" / "versions").glob(
        "*_add_site_torrent_match_text.py"
    )
)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'match_text.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _obs(**overrides) -> TorrentObservation:
    fields = {
        "site_id": "site-a",
        "torrent_id": "1",
        "source": TorrentSource.LIST,
        "title": "The.Show.S01E01.1080p.WEB-DL",
    }
    fields.update(overrides)
    return TorrentObservation(**fields)


async def _row(session) -> SiteTorrent:
    row = (await session.execute(select(SiteTorrent))).scalar_one()
    await session.refresh(row)
    return row


def _consistent(row: SiteTorrent) -> bool:
    return row.match_text == torrent_match_text(row.title, row.subtitle, row.attrs)


async def test_new_row_gets_match_text(db) -> None:
    async with db.session() as session:
        await TorrentRepository(session).bulk_upsert(
            [
                _obs(
                    subtitle="测试剧 / 别名",
                    attrs={"titles_zh": ["测试剧"], "titles_en": ["The Show"]},
                    enrich_version=1,
                )
            ]
        )
    async with db.session() as session:
        row = await _row(session)
        assert _consistent(row)
        assert "测试剧" in row.match_text and "theshow" in row.match_text


async def test_refresh_recomputes_after_subtitle_fill_and_attrs_replace(db) -> None:
    """副标题补空、attrs 覆盖都会改变检索文本；按行的最终状态重算，而不是按观测。"""
    async with db.session() as session:
        await TorrentRepository(session).bulk_upsert([_obs(attrs={}, enrich_version=1)])
    async with db.session() as session:
        await TorrentRepository(session).bulk_upsert(
            [_obs(subtitle="新副标题", attrs={"titles_zh": ["新片名"]}, enrich_version=2)]
        )
    async with db.session() as session:
        row = await _row(session)
        assert row.subtitle == "新副标题"
        assert _consistent(row) and "新片名" in row.match_text

    # 已有副标题时列表刷新不会改写它（静态层只补空）：检索文本必须仍对应库里那份副标题
    async with db.session() as session:
        await TorrentRepository(session).bulk_upsert(
            [_obs(subtitle="被截断的副", attrs={"titles_zh": ["新片名"]}, enrich_version=2)]
        )
    async with db.session() as session:
        row = await _row(session)
        assert row.subtitle == "新副标题"
        assert _consistent(row) and "被截断的副" not in row.match_text


async def test_row_missing_match_text_heals_on_next_write(db) -> None:
    """旧版本写入的行没有检索文本：下次任何写入都顺手补齐。"""
    async with db.session() as session:
        await TorrentRepository(session).bulk_upsert([_obs(subtitle="测试剧")])
        await session.execute(text("UPDATE site_torrent SET match_text = NULL"))
        await session.commit()
    async with db.session() as session:
        await TorrentRepository(session).bulk_upsert([_obs(seeders=12)])
    async with db.session() as session:
        row = await _row(session)
        assert row.match_text is not None and _consistent(row)


async def test_reenrich_recomputes_match_text(db, monkeypatch) -> None:
    """富化版本升级后重算 attrs，NER 片名变了，检索文本跟着变。"""
    async with db.session() as session:
        await TorrentRepository(session).bulk_upsert([_obs(attrs={}, enrich_version=None)])
    monkeypatch.setattr(
        enrich_backfill,
        "_enrich_batch",
        lambda inputs: [{"titles_zh": ["回填片名"]} for _ in inputs],
    )
    assert await enrich_backfill.reenrich_stale_torrents() == 1
    async with db.session() as session:
        row = await _row(session)
        assert _consistent(row) and "回填片名" in row.match_text


def _load_migration():
    spec = importlib.util.spec_from_file_location("match_text_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("title", "subtitle", "attrs"),
    [
        ("Ｔｈｅ．Ｓｈｏｗ S01E02 1080p", "中文名／别名｜类型：剧情", {"titles_zh": ["中文名"]}),
        (
            "A.Prophet.2026.S01E05",
            "我不是大师 全24集",
            {"titles_en": ["A Prophet"], "titles_zh": ["我不是大师"]},
        ),
        ("Her 2013", "", None),
        ("Title", None, {"titles_zh": "不是列表", "titles_en": [3, "Mixed"]}),
        ("Ça Mᴀʀche—Ⅱ", "ｶﾀｶﾅ ﾃｽﾄ", {"titles_zh": [""], "titles_en": ["Ça Marche"]}),
    ],
)
def test_migration_backfill_matches_runtime_definition(title, subtitle, attrs) -> None:
    """迁移里冻结的回填口径与运行期口径逐字一致（迁移不 import 应用代码，靠这里守护）。"""
    migration = _load_migration()
    attrs_json = json.dumps(attrs, ensure_ascii=False) if attrs is not None else None
    assert migration._match_text(title, subtitle, attrs_json) == torrent_match_text(
        title, subtitle, attrs
    )
