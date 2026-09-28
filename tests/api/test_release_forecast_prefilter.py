"""发布预测的 SQL 预筛与按变化触发：只改开销，不改预测结论。

- 预筛（site_torrent.match_text + 别名子串 / 外部 ID）找出的观测，必须与把整个
  90 天窗口读出来逐行细查（旧做法，这里当参照）完全相同，包括全角标题、只有 NER
  片名、只有副标题带别名、检索文本还没算（NULL）这些边角；
- 同步收尾只在本轮新种带来本剧单集观测时才重算；每小时兜底照旧全量重算。
"""

from __future__ import annotations

from datetime import timedelta

import pytest_asyncio
from sqlalchemy import text
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subscription import release_forecast
from movieclaw_api.services.subscription.matching import load_season_titles, to_candidate
from movieclaw_api.services.torrent_matcher import process_new_torrents
from movieclaw_api.settings.store import init_setting_store, reset_setting_store
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    MediaEpisode,
    MediaItem,
    SiteTorrent,
    TorrentSource,
    WantedItem,
    utcnow,
)

from .test_subscription_release_forecast import _seed_target


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'prefilter.db'}")
    monkeypatch.setenv("SUBSCRIPTION_DISPATCH_DRY_RUN", "true")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    init_setting_store()
    yield get_database()
    reset_setting_store()
    await dispose_db()
    get_settings.cache_clear()


def _torrent(site_id: str, torrent_id: str, title: str, publish_time, **fields) -> SiteTorrent:
    attrs = {"media_type": "tv", "seasons": [1], "episodes": [1], "resolution": "1080p"}
    attrs.update(fields.pop("attrs", {}))
    return SiteTorrent(
        site_id=site_id,
        torrent_id=torrent_id,
        title=title,
        attrs=attrs,
        enrich_version=1,
        source=TorrentSource.LIST,
        publish_time=publish_time,
        **fields,
    )


async def _oracle_and_prefiltered(session, media_item_id: int):
    """同一条目：参照做法（整窗读出逐行细查）与预筛做法各自给出的观测。"""
    item = await session.get(MediaItem, media_item_id)
    episodes = list(
        (
            await session.execute(
                select(MediaEpisode).where(MediaEpisode.media_item_id == media_item_id)
            )
        )
        .scalars()
        .all()
    )
    identity = release_forecast._identity_for(
        item, episodes, await load_season_titles(session, media_item_id)
    )
    since = utcnow() - timedelta(days=90)
    every_row = (
        await session.execute(
            select(*release_forecast._CANDIDATE_COLUMNS).where(SiteTorrent.publish_time >= since)
        )
    ).all()
    oracle = release_forecast._observations_for_item(
        identity=identity,
        episodes=episodes,
        candidates=[(row, c) for row in every_row if (c := to_candidate(row)) is not None],
    )
    prefiltered = release_forecast._observations_for_item(
        identity=identity,
        episodes=episodes,
        candidates=await release_forecast._load_item_candidates(session, identity, since=since),
    )
    return oracle, prefiltered


async def test_prefilter_finds_exactly_the_full_scan_observations(db) -> None:
    async with db.session() as session:
        wanted, _ = await _seed_target(session, cadence_days=7)
        first_publish = (await session.execute(select(SiteTorrent.publish_time))).scalar_one()
        later = first_publish + timedelta(hours=2)
        session.add_all(
            [
                # 全角标题
                _torrent("site-b", "fw", "Ｔｅｓｔ　Ｓｈｏｗ　Ｓ０１Ｅ０１ 2160p", later),
                # 标题副标题都是拼音，只有 NER 片名里有别名
                _torrent(
                    "site-c", "ner", "Ce Shi Ju Ji S01E01", later, attrs={"titles_zh": ["测试剧集"]}
                ),
                # 只有副标题带中文别名
                _torrent("site-d", "sub", "Random Name S01E01", later, subtitle="测试剧集 第1集"),
                # 旧版本写入、检索文本还没算的行
                _torrent("site-e", "null", "Test Show S01E01 WEB", later),
                # 整季包不是单集观测；无关剧集更不是
                _torrent(
                    "site-f",
                    "pack",
                    "Test Show S01 Complete",
                    later,
                    attrs={"episodes": [], "complete": True},
                ),
                _torrent("site-g", "other", "Another Show S01E01", later),
                *[
                    _torrent("site-h", f"noise-{i}", f"Noise Title {i} S01E01 1080p", later)
                    for i in range(200)
                ],
            ]
        )
        await session.commit()
        await session.execute(
            text("UPDATE site_torrent SET match_text = NULL WHERE torrent_id = 'null'")
        )
        await session.commit()

        oracle, prefiltered = await _oracle_and_prefiltered(session, wanted.media_item_id)
    assert sorted(oracle, key=repr) == sorted(prefiltered, key=repr)
    assert {o.site_id for o in prefiltered} == {"site-a", "site-b", "site-c", "site-d", "site-e"}


async def test_sync_tail_recomputes_only_when_new_torrents_bring_observations(
    db, monkeypatch
) -> None:
    async with db.session() as session:
        wanted, _ = await _seed_target(session, cadence_days=7)
        first_publish = (await session.execute(select(SiteTorrent.publish_time))).scalar_one()
    await process_new_torrents()  # 首跑：只初始化水位

    full_loads: list[int] = []
    real_load = release_forecast._load_item_candidates

    async def spy(session, identity, *, since, after_id=None):
        if after_id is None:
            full_loads.append(1)
        return await real_load(session, identity, since=since, after_id=after_id)

    monkeypatch.setattr(release_forecast, "_load_item_candidates", spy)

    # 本轮新种与在追剧无关：只看新增的几行，不重算
    async with db.session() as session:
        session.add_all(
            [
                _torrent("site-x", f"unrelated-{i}", f"Other Show {i} S01E01", first_publish)
                for i in range(5)
            ]
        )
        await session.commit()
    await process_new_torrents()
    assert full_loads == []

    # 本轮新种带来本剧 E1 在新站点的观测：重算，预测里出现这个站点
    async with db.session() as session:
        session.add(
            _torrent("site-y", "e1-y", "Test Show S01E01 2160p", first_publish + timedelta(hours=1))
        )
        await session.commit()
    await process_new_torrents()
    assert full_loads == [1]
    async with db.session() as session:
        stored = await session.get(WantedItem, wanted.id)
        assert {site["site_id"] for site in stored.release_forecast["sites"]} == {
            "site-a",
            "site-y",
        }

    # 每小时兜底：没有新种也全量重算
    await process_new_torrents(full_forecast_refresh=True)
    assert full_loads == [1, 1]
