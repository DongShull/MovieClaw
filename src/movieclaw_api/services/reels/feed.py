"""刷片信息流：从本人可见的电影库、剧集库里随机抽片，一页一页地给 App。

**抽样按「部」不按文件**：一部电影、一部剧各算一个名额（剧集库里《哆啦A梦》一部就有
两千多集，按文件抽会刷成它的专场）。抽到剧就在它的集里随机挑一集。一期不看观看
状态，看没看过一样抽（docs/design/reels.md）。

**无状态翻页**：第一页由服务端生成随机种子，App 翻页时把 ``seed`` 与 ``offset``
带回来；同一个种子洗出来的顺序固定，服务端不用记会话，同一次刷片里不会重复。

**按需计算 + 限时**：片段没算过的，一页里并行算（每个文件零点几秒到两秒），整页最多
等 ``PAGE_BUDGET_S``；超时没算完的这一页先跳过，计算在后台照常跑完落盘。返回一页后
顺手在后台把下一页要用的片段算好——App 翻到下一页时基本都是现成的。

**按类型筛**：App 顶部的「全部 / 某个类型」只在抽样池上做一次过滤（条目档案里的类型列表），
类型列表本身由 ``list_genres`` 从本人可见、可抽的池子里数出来，只列真有片的类型。

**怎么放和放哪段分开**：``segment`` 永远是原片时间轴上的起止；``play`` 说明这一条
怎么放（一期只有 ``seek``：自研引擎从原片中间起播）。App 用 ``modes`` 声明自己会放
哪几种，服务端只发它会放的——将来加「预剪好的片段文件」（``clip``）时老版本 App
不受影响。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.services.auth import Principal
from movieclaw_api.services.library.access import (
    ContentLimit,
    content_limit_for,
    visible_library_ids,
)
from movieclaw_api.services.library.content_rating import ratings_at_or_below
from movieclaw_api.services.library.items import backdrop_facts_many, poster_facts_many
from movieclaw_api.services.media_scrape import asset_version
from movieclaw_api.services.playback import marks as playback_marks
from movieclaw_api.services.playback.signing import issue_stream_token
from movieclaw_api.services.reels.segments import FileRef, ReelSegment, get_segment
from movieclaw_api.services.reels.tracks import choose_audio, choose_subtitle
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    Library,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    MediaItemPerson,
    MediaMetadata,
    Person,
    ReelEvent,
)

logger = logging.getLogger("movieclaw_api.reels")

#: 一期能从中间起播并读得出索引的容器（原盘、镜像、TS 一期跳过）
SUPPORTED_CONTAINERS = ("mkv", "webm", "mp4", "m4v", "mov")
#: 一页最多等多久（秒）：超时没算完的这一页先跳过
PAGE_BUDGET_S = 3.0
#: 一页最多往后看多少部（很多部挑不出片段时不至于无限往后翻）
SCAN_FACTOR = 3
#: 剧集太短（片花、预告）不抽。不能定太高：《小猪佩奇》一集只有 5 分钟左右
MIN_EPISODE_SECONDS = 120
#: App 目前会放的方式
MODE_SEEK = "seek"

_warm_tasks: set[asyncio.Task[None]] = set()


@dataclass
class ReelCandidate:
    """一页里的一条：选中的文件与算好的片段。"""

    media_item_id: int
    kind: str  # movie / episode
    file: LibraryFile
    segment: ReelSegment | None = None


@dataclass
class ReelPage:
    seed: int
    next_offset: int
    has_more: bool
    items: list[dict[str, Any]] = field(default_factory=list)


# --- 抽样 ------------------------------------------------------------------------


async def _playable_library_kinds(session: AsyncSession, principal: Principal) -> dict[int, str]:
    """本人可见的电影库 / 剧集库：库 id → 类型。"""
    visible = await visible_library_ids(session, principal)
    if not visible:
        return {}
    rows = await session.execute(
        select(Library.id, Library.kind).where(
            Library.id.in_(visible),  # type: ignore[union-attr]
            Library.kind.in_(("movie", "tv")),  # type: ignore[attr-defined]
        )
    )
    return {int(lid): str(kind) for lid, kind in rows.all()}


def _playable_file_filter(library_ids: Sequence[int]) -> list[Any]:
    return [
        LibraryFile.library_id.in_(list(library_ids)),  # type: ignore[attr-defined]
        LibraryFile.in_place(),
        LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
        LibraryFile.container.in_(SUPPORTED_CONTAINERS),  # type: ignore[union-attr]
        ~LibraryFile.file_path.ilike("%.strm"),  # type: ignore[attr-defined]
    ]


async def _title_pool(
    session: AsyncSession, libraries: dict[int, str], limit: ContentLimit
) -> list[tuple[int, str]]:
    """可抽的「部」：(条目 id, movie/episode)，按条目 id 排好（洗牌前的确定顺序）。"""
    stmt = (
        select(LibraryFile.media_item_id, LibraryFile.library_id)
        .where(*_playable_file_filter(list(libraries)))
        .distinct()
    )
    if not limit.unrestricted:
        allowed = MediaMetadata.content_rating.in_(ratings_at_or_below(limit.max_age or 0))  # type: ignore[union-attr]
        if limit.allow_unrated:
            allowed = or_(allowed, MediaMetadata.content_rating.is_(None))  # type: ignore[union-attr]
        stmt = stmt.outerjoin(
            MediaMetadata,
            MediaMetadata.media_item_id == LibraryFile.media_item_id,  # type: ignore[arg-type]
        ).where(allowed)
    kinds: dict[int, str] = {}
    for item_id, library_id in (await session.execute(stmt)).all():
        kind = "episode" if libraries.get(int(library_id)) == "tv" else "movie"
        kinds.setdefault(int(item_id), kind)
    return sorted(kinds.items())


async def _genres_of(session: AsyncSession, item_ids: Sequence[int]) -> dict[int, list[str]]:
    """条目 → 档案里的类型列表（本地化后的中文名，如「剧情」「动作」）。"""
    if not item_ids:
        return {}
    rows = await session.execute(
        select(MediaMetadata.media_item_id, MediaMetadata.genres).where(
            MediaMetadata.media_item_id.in_(list(item_ids))  # type: ignore[union-attr]
        )
    )
    return {int(item_id): [str(g) for g in (genres or []) if g] for item_id, genres in rows.all()}


async def _filter_by_genre(
    session: AsyncSession, pool: list[tuple[int, str]], genre: str | None
) -> list[tuple[int, str]]:
    if not genre:
        return pool
    genres = await _genres_of(session, [item_id for item_id, _ in pool])
    return [(item_id, kind) for item_id, kind in pool if genre in genres.get(item_id, [])]


async def list_genres(session: AsyncSession, principal: Principal) -> list[tuple[str, int]]:
    """本人刷片能刷到的类型及每类几部，多的在前（顶部「全部」下拉的选项）。"""
    libraries = await _playable_library_kinds(session, principal)
    if not libraries:
        return []
    pool = await _title_pool(session, libraries, await content_limit_for(session, principal))
    counts: dict[str, int] = {}
    for names in (await _genres_of(session, [item_id for item_id, _ in pool])).values():
        for name in set(names):
            counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


_HEIGHT = re.compile(r"(\d{3,4})")


def _height(resolution: str | None) -> int:
    match = _HEIGHT.search(resolution or "")
    return int(match.group(1)) if match else 0


def choose_file(files: Sequence[LibraryFile], kind: str, rng: random.Random) -> LibraryFile | None:
    """一部片里挑一个文件放。

    电影：多个版本时取 1080p 及以上里体积最小的（竖屏横条 1080p 足够，文件小预取快）；
    剧集：在正片季（季号 ≥ 1）里随机挑一集，太短的不要；没有正片季才用特别季。
    """
    if not files:
        return None
    if kind == "movie":
        return min(files, key=lambda f: (_height(f.resolution) < 1080, f.size_bytes or 0))
    episodes = [
        f
        for f in files
        if (f.duration_seconds or 0) == 0 or (f.duration_seconds or 0) >= MIN_EPISODE_SECONDS
    ]
    regular = [f for f in episodes if (f.season_number or 0) >= 1] or episodes
    if not regular:
        return None
    return rng.choice(
        sorted(regular, key=lambda f: (f.season_number or 0, f.episode_number or 0, f.id or 0))
    )


def _file_ref(file: LibraryFile, kind: str) -> FileRef:
    return FileRef(
        id=int(file.id or 0),
        media_item_id=int(file.media_item_id or 0),
        path=file.file_path,
        kind=kind,
        size_bytes=file.size_bytes,
        mtime_ns=file.file_mtime_ns,
        duration_s=float(file.duration_seconds) if file.duration_seconds else None,
        hdr=file.hdr,
        subtitle_streams=list(file.subtitle_streams or []),
    )


async def _files_of(
    session: AsyncSession, item_ids: Sequence[int], library_ids: Sequence[int]
) -> dict[int, list[LibraryFile]]:
    if not item_ids:
        return {}
    rows = await session.execute(
        select(LibraryFile).where(
            LibraryFile.media_item_id.in_(list(item_ids)),  # type: ignore[union-attr]
            *_playable_file_filter(library_ids),
        )
    )
    grouped: dict[int, list[LibraryFile]] = {}
    for file in rows.scalars().all():
        grouped.setdefault(int(file.media_item_id or 0), []).append(file)
    return grouped


def _session_rng(seed: int, item_id: int) -> random.Random:
    """同一次刷片（同一个种子）里，同一部剧总是抽到同一集。"""
    return random.Random(seed * 1_000_003 + item_id)


async def _choose_candidates(
    session: AsyncSession,
    pool: list[tuple[int, str]],
    library_ids: Sequence[int],
    seed: int,
) -> list[ReelCandidate]:
    files = await _files_of(session, [item_id for item_id, _ in pool], library_ids)
    candidates = []
    for item_id, kind in pool:
        chosen = choose_file(files.get(item_id, []), kind, _session_rng(seed, item_id))
        if chosen is not None:
            candidates.append(ReelCandidate(item_id, kind, chosen))
    return candidates


# --- 翻页 ------------------------------------------------------------------------


async def build_feed(
    session: AsyncSession,
    principal: Principal,
    *,
    seed: int | None,
    offset: int,
    limit: int,
    modes: set[str],
    genre: str | None = None,
) -> ReelPage:
    """组一页。``modes`` 里没有本服务能出的放法时返回空页；``genre`` 只抽这个类型的片。"""
    seed = seed if seed is not None else secrets.randbelow(2**31)
    if MODE_SEEK not in modes:
        return ReelPage(seed=seed, next_offset=offset, has_more=False)
    libraries = await _playable_library_kinds(session, principal)
    if not libraries:
        return ReelPage(seed=seed, next_offset=offset, has_more=False)
    pool = await _title_pool(session, libraries, await content_limit_for(session, principal))
    pool = await _filter_by_genre(session, pool, genre)
    random.Random(seed).shuffle(pool)

    window = pool[offset : offset + limit * SCAN_FACTOR]
    candidates = await _choose_candidates(session, window, list(libraries), seed)
    ready = await _segments_within_budget(candidates, limit)

    # 下一页从「这一页实际看到哪一部」接着往后
    last_used = ready[-1].media_item_id if ready else None
    consumed = len(window)
    if last_used is not None and len(ready) >= limit:
        consumed = next(i for i, (item_id, _) in enumerate(window) if item_id == last_used) + 1
    next_offset = offset + consumed
    has_more = next_offset < len(pool)
    if has_more:
        _warm_next_page(pool[next_offset : next_offset + limit + 3], list(libraries), seed)

    member_id = principal.member_id if principal.member_id is not None else 0
    items = await _assemble(session, ready[:limit], member_id)
    return ReelPage(seed=seed, next_offset=next_offset, has_more=has_more, items=items)


async def _segments_within_budget(
    candidates: list[ReelCandidate], limit: int
) -> list[ReelCandidate]:
    """并行取片段，最多等 PAGE_BUDGET_S；按抽样顺序返回挑得出片段的前 limit 条。"""
    if not candidates:
        return []
    tasks = {asyncio.ensure_future(get_segment(_file_ref(c.file, c.kind))): c for c in candidates}
    done, _pending = await asyncio.wait(tasks, timeout=PAGE_BUDGET_S)
    for task in done:
        if not task.cancelled() and task.exception() is None:
            tasks[task].segment = task.result()
    ready = [c for c in candidates if c.segment is not None]
    return ready[:limit]


def _warm_next_page(pool: list[tuple[int, str]], library_ids: list[int], seed: int) -> None:
    """后台把下一页要用的片段先算好（选文件要查库，用独立会话）。"""

    async def warm() -> None:
        try:
            async with get_database().session() as session:
                candidates = await _choose_candidates(session, pool, library_ids, seed)
            for candidate in candidates:
                await get_segment(_file_ref(candidate.file, candidate.kind))
        except Exception:  # noqa: BLE001 —— 预热失败不影响任何请求
            logger.debug("刷片预热下一页失败", exc_info=True)

    task = asyncio.create_task(warm())
    _warm_tasks.add(task)
    task.add_done_callback(_warm_tasks.discard)


# --- 装配 ------------------------------------------------------------------------


def reel_id(file_id: int, start_ms: int) -> str:
    """片段标识：同一文件同一起点就是同一段（事件按它归并统计）。"""
    return f"rl_{file_id}_{start_ms}"


def _asset_url(rel: str | None) -> str | None:
    return f"/images/assets/{rel}?v={asset_version(rel)}" if rel else None


async def _assemble(
    session: AsyncSession, candidates: list[ReelCandidate], member_id: int
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    item_ids = sorted({c.media_item_id for c in candidates})
    items = {
        item.id: item
        for item in (
            await session.execute(select(MediaItem).where(MediaItem.id.in_(item_ids)))  # type: ignore[union-attr]
        ).scalars()
    }
    metadata = {
        meta.media_item_id: meta
        for meta in (
            await session.execute(
                select(MediaMetadata).where(MediaMetadata.media_item_id.in_(item_ids))  # type: ignore[union-attr]
            )
        ).scalars()
    }
    posters = await poster_facts_many(session, item_ids)
    backdrops = await backdrop_facts_many(session, item_ids)
    episodes = await _episode_facts(session, candidates)
    directors = await _directors_of(session, item_ids)

    out = []
    for c in candidates:
        item, meta, segment, file = (
            items.get(c.media_item_id),
            metadata.get(c.media_item_id),
            c.segment,
            c.file,
        )
        if item is None or segment is None:
            continue
        backdrop = backdrops.get(c.media_item_id)
        poster = posters.get(c.media_item_id)
        episode = None
        runtime = (meta.runtime_minutes if meta else None) or _minutes(file.duration_seconds)
        # 收藏落在整部（电影 / 整剧）上；已看电影看整部、剧集看这一集
        favorite_target = playback_marks.MarkTarget(c.media_item_id, None, None)
        played_target = favorite_target
        if c.kind == "episode":
            key = (c.media_item_id, file.season_number or 0, file.episode_number or 0)
            name, overview, minutes = episodes.get(key, ("", None, None))
            episode = {
                "season": key[1],
                "episode": key[2],
                "name": name or None,
                "overview": overview or None,
            }
            runtime = minutes or _minutes(file.duration_seconds)
            played_target = playback_marks.MarkTarget(c.media_item_id, key[1], key[2])
        favorite = await playback_marks.get_state(session, favorite_target, member_id=member_id)
        played = (
            favorite
            if played_target is favorite_target
            else await playback_marks.get_state(session, played_target, member_id=member_id)
        )
        subtitle_ordinal = choose_subtitle(file.subtitle_streams)
        subtitle = None
        if subtitle_ordinal is not None:
            stream = (file.subtitle_streams or [])[subtitle_ordinal]
            subtitle = {
                "ordinal": subtitle_ordinal,
                "language": stream.get("language"),
                "title": stream.get("title"),
                "codec": stream.get("codec"),
            }
        token = await issue_stream_token(member_id=member_id, file_id=int(file.id or 0))
        out.append(
            {
                "id": reel_id(segment.file_id, segment.start_ms),
                "title": {
                    "media_item_id": c.media_item_id,
                    "library_id": file.library_id,
                    "kind": "tv" if c.kind == "episode" else "movie",
                    "name": item.title,
                    "year": item.year,
                    "rating": meta.vote_average if meta else None,
                    "runtime_minutes": runtime,
                    "genres": list(meta.genres or [])[:3] if meta else [],
                    "tagline": (meta.tagline or None) if meta else None,
                    "overview": (meta.overview or None) if meta else None,
                    "favorite": favorite.is_favorite,
                    "played": played.played,
                    # 电影是导演、剧集是主创；关系表还没有（旧条目没刷新）时退回档案里的姓名
                    "directors": directors.get(c.media_item_id)
                    or [
                        {"name": name, "tmdb_person_id": None, "avatar_url": None}
                        for name in (meta.directors if meta else [])[:MAX_DIRECTORS]
                    ],
                    "poster_url": poster.url if poster else None,
                    "backdrop_url": backdrop,
                    "logo_url": _asset_url(meta.logo_file) if meta and meta.logo_file else None,
                    "episode": episode,
                },
                "cover_url": _asset_url(segment.cover) or backdrop,
                "segment": {
                    "file_id": segment.file_id,
                    "start_ms": segment.start_ms,
                    "end_ms": segment.end_ms,
                    "duration_ms": int(file.duration_seconds * 1000)
                    if file.duration_seconds
                    else None,
                    "method": segment.method,
                },
                "play": {
                    "mode": MODE_SEEK,
                    "stream_url": f"/api/v1/playback/files/{file.id}/stream?token={token}",
                    "size_bytes": file.size_bytes,
                    "audio_ordinal": choose_audio(file.audio_streams),
                    "subtitle": subtitle,
                    "prefetch": [
                        {"offset": r.offset, "length": r.length, "purpose": r.purpose}
                        for r in segment.prefetch
                    ],
                },
            }
        )
    return out


#: 每部最多给几位导演（左下角只放得下一两个名字）
MAX_DIRECTORS = 2


async def _directors_of(
    session: AsyncSession, item_ids: Sequence[int]
) -> dict[int, list[dict[str, Any]]]:
    """条目 → 导演（剧集为主创），取自与详情页同一张影人关系表，按署名顺序。"""
    from movieclaw_api.core.config import get_settings

    if not item_ids:
        return {}
    base = get_settings().tmdb_image_base_url.rstrip("/")
    rows = await session.execute(
        select(
            MediaItemPerson.media_item_id, Person.name, Person.profile_path, Person.tmdb_person_id
        )
        .join(Person, Person.id == MediaItemPerson.person_id)  # type: ignore[arg-type]
        .where(
            MediaItemPerson.media_item_id.in_(list(item_ids)),  # type: ignore[attr-defined]
            MediaItemPerson.department == "director",
        )
        .order_by(MediaItemPerson.credit_order, MediaItemPerson.id)
    )
    out: dict[int, list[dict[str, Any]]] = {}
    for item_id, name, profile, tmdb_id in rows.all():
        people = out.setdefault(int(item_id), [])
        if len(people) < MAX_DIRECTORS:
            people.append(
                {
                    "name": name,
                    "tmdb_person_id": tmdb_id,
                    "avatar_url": f"{base}/w185{profile}" if profile else None,
                }
            )
    return out


def _minutes(seconds: float | int | None) -> int | None:
    return int(round(seconds / 60)) if seconds else None


async def _episode_facts(
    session: AsyncSession, candidates: list[ReelCandidate]
) -> dict[tuple[int, int, int], tuple[str, str | None, int | None]]:
    """(条目, 季, 集) → (集名, 分集简介, 单集时长分钟)。"""
    series = {c.media_item_id for c in candidates if c.kind == "episode"}
    if not series:
        return {}
    rows = await session.execute(
        select(
            MediaEpisode.media_item_id,
            MediaEpisode.season_number,
            MediaEpisode.episode_number,
            MediaEpisode.name,
            MediaEpisode.overview,
            MediaEpisode.runtime_minutes,
        ).where(MediaEpisode.media_item_id.in_(sorted(series)))  # type: ignore[attr-defined]
    )
    return {
        (int(i), int(s), int(e)): (str(n or ""), o or None, r or None)
        for i, s, e, n, o, r in rows.all()
    }


# --- 事件 ------------------------------------------------------------------------

EVENT_KINDS = frozenset(
    {
        "impression",
        "first_frame",
        "leave",
        "complete",
        "continue",
        "open",
        "fullscreen",
        "detail",
        "fail",
    }
)


async def record_events(
    session: AsyncSession, member_id: int, events: Sequence[dict[str, Any]]
) -> int:
    """把 App 攒的一批事件落库，返回落了几条（不认识的事件类型直接丢弃）。"""
    rows = [
        ReelEvent(
            member_id=member_id,
            reel_id=str(e["reel_id"])[:64],
            kind=str(e["kind"]),
            mode=str(e.get("mode") or MODE_SEEK)[:16],
            media_item_id=e.get("media_item_id"),
            file_id=e.get("file_id"),
            position_ms=e.get("position_ms"),
            watched_ms=e.get("watched_ms"),
            wait_ms=e.get("wait_ms"),
            detail=e.get("detail") or None,
        )
        for e in events
        if e.get("kind") in EVENT_KINDS and e.get("reel_id")
    ]
    if rows:
        session.add_all(rows)
        await session.commit()
    return len(rows)
