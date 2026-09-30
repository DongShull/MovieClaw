"""大库上筛选面板与首屏接口的端到端性能守卫（issue #514）。

背景：「更多筛选」（``facets?tier=all``）里 HDR / SDR / 文件失联三档用的是
文件级条件。旧写法是相关 ``EXISTS`` 子查询，SQLite 在**没有 sqlite_stat1**
时会为它挑 ``library_id`` 索引——每个条目把本库全部文件扫一遍，复杂度
O(条目数 × 文件数)。实测 229 部 / 1.8 万文件的库单次请求 121 秒。

这里造一个同量级的大库（**故意不刷新查询统计**，复现最坏情形），走真实 HTTP
接口，锁两件事：

1. **结果正确**：HDR / SDR / 失联 / 画质各档的计数与筛选后的墙，和造数时的
   已知答案逐一对上（含 NOT IN 的 NULL 语义：未识别文件不能把 SDR 吞成空）；
2. **不再随文件数爆炸**：整轮打开页面用到的接口都在宽松上限内。上限远高于
   正常耗时（几十~几百毫秒）、远低于旧写法（十几秒起），CI 机器慢也不会误报。
"""

from __future__ import annotations

import time

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from movieclaw_api.core.config import get_settings
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, FileState, LibraryFile, MediaItem, utcnow
from movieclaw_db.repositories.library_repo import LibraryRepository

_ITEMS = 300
_FILES_PER_ITEM = 60  # 1.8 万文件，与 issue 里的动画库同量级
#: 宽松上限（秒）：旧写法在此规模上单个接口 > 10 秒
_BUDGET = 3.0
#: 真实台账行带音轨/字幕/章节 JSON（几 KB 一行）：相关子查询每次回表都要越过它们，
#: 行太瘦会把旧写法的代价低估一个量级
_STREAMS = [{"index": i, "codec": "eac3", "language": "jpn", "title": "x" * 40} for i in range(6)]
_CHAPTERS = [{"start": k * 100, "title": f"Chapter {k}"} for k in range(12)]


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'perf.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(db):
    from movieclaw_api.api.deps import require_admin, require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    admin = Principal(kind="admin", name="管理员")
    app.dependency_overrides[require_login] = lambda: admin
    app.dependency_overrides[require_admin] = lambda: admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        # 预热：进程内第一个请求要付一次 FastAPI / pydantic 的 schema 构建代价
        # （实测 ~4 秒，与查询无关，真实部署里只发生在开机后的第一次请求）。
        # 不预热的话上限会去量它，而不是量我们要守的查询
        await c.get("/api/v1/libraries")
        yield c


def _hdr(i: int) -> bool:
    return i % 5 == 0


def _missing(i: int) -> bool:
    return i % 7 == 0


def _uhd(i: int) -> bool:
    return i % 3 == 0


async def _seed_big_library(db) -> int:
    """300 部剧 × 60 集。已知答案：i%5==0 有 HDR 文件，i%7==0 有失联文件，
    i%3==0 有 2160p 文件；另放一批 media_item_id 为空的未识别文件。"""
    async with db.session() as s:
        lib = await LibraryRepository(s).create(name="动画", kind="tv", root_paths=["/anime"])
        library_id = lib.id
        items = [
            MediaItem(kind="tv", tmdb_id=1000 + n, title=f"作品{n:04d}", original_title=f"T{n}")
            for n in range(_ITEMS)
        ]
        s.add_all(items)
        await s.flush()
        now = utcnow()
        rows = []
        for n, item in enumerate(items):
            for c in range(_FILES_PER_ITEM):
                special = c == 0
                rows.append(
                    dict(
                        library_id=library_id,
                        media_item_id=item.id,
                        season_number=c // 12 + 1,
                        episode_number=c % 12 + 1,
                        file_path=f"/anime/{n}/S{c // 12 + 1:02d}E{c % 12 + 1:02d}.mkv",
                        size_bytes=1000 + c,
                        source=FileSource.SCANNED,
                        resolution="2160p" if special and _uhd(n) else "1080p",
                        hdr="HDR10" if special and _hdr(n) else None,
                        state=FileState.MISSING if c == 1 and _missing(n) else FileState.IN_PLACE,
                        missing_since=now if c == 1 and _missing(n) else None,
                        audio_streams=_STREAMS,
                        subtitle_streams=_STREAMS,
                        chapters=_CHAPTERS,
                        created_at=now,
                        updated_at=now,
                    )
                )
        for k in range(50):  # 未识别：media_item_id 为空
            rows.append(
                dict(
                    library_id=library_id,
                    media_item_id=None,
                    season_number=0,
                    episode_number=0,
                    file_path=f"/anime/unknown{k}.mkv",
                    size_bytes=1,
                    source=FileSource.SCANNED,
                    state=FileState.IN_PLACE,
                    unidentified_code="no_match",
                    created_at=now,
                    updated_at=now,
                )
            )
        for i in range(0, len(rows), 3000):
            await s.execute(LibraryFile.__table__.insert(), rows[i : i + 3000])
        await s.commit()
    return library_id


async def _get(client, url: str) -> tuple[dict, float]:
    started = time.perf_counter()
    resp = await client.get(url)
    elapsed = time.perf_counter() - started
    assert resp.status_code == 200, resp.text
    return resp.json()["data"], elapsed


def _counts(facet: list[dict]) -> dict[str, int]:
    return {f["value"]: f["count"] for f in facet}


async def test_facets_all_correct_and_fast_without_query_stats(db, client) -> None:
    library_id = await _seed_big_library(db)
    async with db.session() as s:  # 前提：确实没有统计信息（最坏情形）
        stats = await s.execute(
            text("select count(*) from sqlite_master where name='sqlite_stat1'")
        )
        assert stats.scalar() == 0

    data, elapsed = await _get(client, f"/api/v1/libraries/{library_id}/facets?tier=all")
    assert elapsed < _BUDGET, f"facets?tier=all 用了 {elapsed:.1f}s（旧写法在此规模上 >10s）"

    hdr_n = sum(_hdr(n) for n in range(_ITEMS))
    assert data["total"] == _ITEMS
    assert _counts(data["hdr"]) == {"1": hdr_n, "0": _ITEMS - hdr_n}
    assert _counts(data["stock"])["missing"] == sum(_missing(n) for n in range(_ITEMS))
    assert _counts(data["resolutions"]) == {
        "1080p": _ITEMS,
        "2160p": sum(_uhd(n) for n in range(_ITEMS)),
    }


async def test_file_level_filters_return_exact_wall(db, client) -> None:
    """筛选后的墙与已知答案一致（面板计数 = 点进去的数量）。"""
    library_id = await _seed_big_library(db)
    base = f"/api/v1/libraries/{library_id}/items?limit=200"

    async def titles(query: str) -> set[str]:
        got: set[str] = set()
        offset = 0
        while True:
            page, elapsed = await _get(client, f"{base}&offset={offset}&{query}")
            assert elapsed < _BUDGET
            got |= {row["title"] for row in page}
            if len(page) < 200:
                return got
            offset += 200

    def expect(pred) -> set[str]:
        return {f"作品{n:04d}" for n in range(_ITEMS) if pred(n)}

    assert await titles("hdr=true") == expect(_hdr)
    # NOT EXISTS 的语义：SDR = 没有任何 HDR 文件的条目，未识别文件（NULL）不能干扰
    assert await titles("hdr=false") == expect(lambda n: not _hdr(n))
    assert await titles("stock=missing") == expect(_missing)
    assert await titles("res=2160p") == expect(_uhd)
    assert await titles("hdr=true&res=2160p") == expect(lambda n: _hdr(n) and _uhd(n))


async def test_open_library_page_requests_are_fast(db, client) -> None:
    """前端打开详情页会发的那批请求：库列表、首屏墙、跳转索引、管理员待办清单。"""
    library_id = await _seed_big_library(db)
    urls = [
        "/api/v1/libraries",
        f"/api/v1/libraries/{library_id}/items?limit=60",
        f"/api/v1/libraries/{library_id}/items?limit=200&identity=provisional&sort=added_at",
        f"/api/v1/libraries/{library_id}/item-index?sort=title",
        f"/api/v1/libraries/{library_id}/facets",
        f"/api/v1/libraries/{library_id}/missing",
        f"/api/v1/libraries/identification/unidentified-files?library_id={library_id}",
        f"/api/v1/libraries/identification/review-cases?library_id={library_id}",
        f"/api/v1/libraries/identification/ignored-files?library_id={library_id}",
    ]
    for url in urls:
        _, elapsed = await _get(client, url)
        assert elapsed < _BUDGET, f"{url} 用了 {elapsed:.1f}s"


async def test_file_level_filters_use_uncorrelated_subquery(db) -> None:
    """结构守卫（不依赖机器快慢）：文件级筛选的执行计划里不能有相关子查询。

    相关子查询每个条目执行一次；没有统计信息时 SQLite 还会给它挑错索引，
    退化成 O(条目数 × 文件数)。这条直接看 EXPLAIN QUERY PLAN，慢机器也不误报。
    """
    from sqlalchemy import func, select

    from movieclaw_api.services.library.items import LibraryFilter, _facet_scope

    library_id = await _seed_big_library(db)
    for probe in (
        LibraryFilter(hdr=True),
        LibraryFilter(hdr=False),
        LibraryFilter(stock=("missing",)),
        LibraryFilter(resolutions=("2160p",)),
    ):
        query = select(func.count(func.distinct(LibraryFile.media_item_id))).where(
            *_facet_scope(library_id, probe, 0, "")
        )
        sql = str(query.compile(compile_kwargs={"literal_binds": True}))
        async with db.session() as s:
            plan = "\n".join(
                str(row[3]) for row in (await s.execute(text("EXPLAIN QUERY PLAN " + sql))).all()
            )
        assert "CORRELATED" not in plan, f"{probe} 的计划里出现相关子查询：\n{plan}"
