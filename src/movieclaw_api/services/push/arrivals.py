"""「媒体库有新片」推送（docs/design/cloud-push.md §5）。

入库的路径很多（订阅入库、手动下载、监听目录、扫描库根……），在每个写台账的地方
各挂一次钩子迟早会漏。这里反过来：后台每两分钟看一眼台账里**新出现的行**，从数据上
判断「是不是新片」，所有入库路径一次覆盖。

什么算新片：某部片（或某一集）**第一次**出现在这个库里——同库同单元已经有更早的行，
说明是洗版、多版本或改名，不算。扫描发现的文件在库建好后的头 24 小时内不算：那是
新建媒体库的首次全量扫描，推出去就是几千条。

攒一攒再发：一个库连续 5 分钟没有新行才发，最多等 30 分钟；一批里不止一部就合成一条。
推给打开了这项、并勾选了这个库（或选了「全部」）的人；看不到的库、超出分级的片不推；
自己订阅了的片已经会收到「入库完成」，这里不重复。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.services.push.events import (
    _item_visible,
    _lazy_image,
    _library_path,
    episode_label,
    manually_downloaded,
)
from movieclaw_api.services.push.notify import AlertContent, notify
from movieclaw_api.settings.cloud import ArrivalsProgress
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    FileSource,
    FileState,
    Library,
    LibraryFile,
    MediaItem,
    Subscription,
    SubscriptionFollower,
    utcnow,
)

logger = logging.getLogger("movieclaw_api.push.arrivals")

TICK_S = 120
#: 一个库安静这么久（没有新行）才发
QUIET = timedelta(minutes=5)
#: 最多攒这么久，持续入库时也按这个节奏发
MAX_WAIT = timedelta(minutes=30)
#: 新建的库头 24 小时扫描出来的不算新片（首次全量扫描）
NEW_LIBRARY_GRACE = timedelta(hours=24)
#: 一条汇总里点名的片数
NAMED_IN_SUMMARY = 3


@dataclass
class ItemArrival:
    """一个条目在这一批里新到的单元。"""

    item: MediaItem
    units: list[tuple[int, int]] = field(default_factory=list)
    #: 这个库里以前没有这部（新片 / 新剧），否则是已有剧集的更新
    new_item: bool = False


@dataclass
class LibraryBatch:
    library: Library
    items: list[ItemArrival]


async def _progress() -> ArrivalsProgress:
    from movieclaw_api.settings import get_setting_store

    return await get_setting_store().get(ArrivalsProgress)


async def _save(progress: ArrivalsProgress) -> None:
    from movieclaw_api.settings import get_setting_store

    await get_setting_store().set(progress)


async def collect_ready(session: AsyncSession, now: datetime) -> tuple[list[LibraryBatch], dict]:
    """找出可以发的批次，返回（批次，新的水位）。不写库，调用方发完再存水位。"""
    progress = await _progress()
    started = progress.started_at or now
    marks = {int(k): v for k, v in progress.marks.items()}
    floor = min([started, *marks.values()])
    rows = (
        (
            await session.execute(
                select(LibraryFile)
                .where(LibraryFile.created_at > floor)  # type: ignore[operator]
                .order_by(LibraryFile.id)
            )
        )
        .scalars()
        .all()
    )

    by_library: dict[int, list[LibraryFile]] = {}
    for row in rows:
        if row.library_id is None:
            continue
        if row.created_at > marks.get(row.library_id, started):
            by_library.setdefault(row.library_id, []).append(row)

    batches: list[LibraryBatch] = []
    new_marks = dict(marks)
    for library_id, group in by_library.items():
        newest = max(r.created_at for r in group)
        oldest = min(r.created_at for r in group)
        if newest > now - QUIET and oldest > now - MAX_WAIT:
            continue  # 还在陆续入库，再等等
        new_marks[library_id] = newest
        library = await session.get(Library, library_id)
        if library is None:
            continue
        mark = marks.get(library_id, started)
        items = await _arrivals(session, library, group, mark)
        if items:
            batches.append(LibraryBatch(library=library, items=items))
    return batches, {str(k): v for k, v in new_marks.items()}


async def _arrivals(
    session: AsyncSession, library: Library, rows: list[LibraryFile], mark: datetime
) -> list[ItemArrival]:
    """这一批里真正「新」的单元，按条目归并。"""
    found: dict[int, ItemArrival] = {}
    for row in rows:
        if row.media_item_id is None or row.state != FileState.IN_PLACE.value:
            continue
        if (
            row.source == FileSource.SCANNED.value
            and row.created_at - library.created_at < NEW_LIBRARY_GRACE
        ):
            continue  # 新建库的首次全量扫描
        unit = (row.season_number, row.episode_number)
        earlier = (
            await session.execute(
                select(LibraryFile.id).where(  # type: ignore[call-overload]
                    LibraryFile.library_id == library.id,
                    LibraryFile.media_item_id == row.media_item_id,
                    LibraryFile.season_number == unit[0],
                    LibraryFile.episode_number == unit[1],
                    LibraryFile.id < row.id,  # type: ignore[operator]
                )
            )
        ).first()
        if earlier is not None:
            continue  # 洗版、多版本、改名：这个单元库里早就有了
        arrival = found.get(row.media_item_id)
        if arrival is None:
            item = await session.get(MediaItem, row.media_item_id)
            if item is None:
                continue
            before = (
                await session.execute(
                    select(LibraryFile.id).where(  # type: ignore[call-overload]
                        LibraryFile.library_id == library.id,
                        LibraryFile.media_item_id == row.media_item_id,
                        LibraryFile.created_at <= mark,  # type: ignore[operator]
                    )
                )
            ).first()
            arrival = found[row.media_item_id] = ItemArrival(item=item, new_item=before is None)
        if unit not in arrival.units:
            arrival.units.append(unit)
    return list(found.values())


async def _subscribed(session: AsyncSession, member_id: int, item_ids: set[int]) -> set[int]:
    """这些条目里，这个人自己订阅（发起或关注）了的：他会收到「入库完成」，这里不重复。"""
    subs = (
        (
            await session.execute(
                select(Subscription).where(Subscription.media_item_id.in_(item_ids))  # type: ignore[attr-defined]
            )
        )
        .scalars()
        .all()
    )
    mine: set[int] = set()
    for sub in subs:
        if (sub.created_by_member_id or 0) == member_id:
            mine.add(sub.media_item_id)
            continue
        if (
            member_id
            and (
                await session.execute(
                    select(SubscriptionFollower.id).where(  # type: ignore[call-overload]
                        SubscriptionFollower.subscription_id == sub.id,
                        SubscriptionFollower.member_id == member_id,
                    )
                )
            ).first()
        ):
            mine.add(sub.media_item_id)
    return mine


def _recipients(library_id: int):  # type: ignore[no-untyped-def]
    """打开了「媒体库有新片」、并关心这个库、也看得到这个库的人。"""

    async def resolve(session: AsyncSession) -> set[int]:
        from movieclaw_api.services.library.access import member_visible_ids
        from movieclaw_api.services.push import preferences
        from movieclaw_db.models import Member

        members = {0} | {
            int(m)
            for m in (
                await session.execute(
                    select(Member.id).where(Member.status == "active")  # type: ignore[call-overload]
                )
            ).scalars()
            if m is not None
        }
        wanting = await preferences.wants(session, members, "library_new")
        selections = await preferences.library_selections(session, wanting)
        result = set()
        for member_id in wanting:
            chosen = selections.get(member_id)
            if chosen is not None and library_id not in chosen:
                continue
            if library_id in await member_visible_ids(session, member_id):
                result.add(member_id)
        return result

    return resolve


def _content(library: Library, arrivals: list[ItemArrival]):  # type: ignore[no-untyped-def]
    images = {a.item.id: _lazy_image(_image_url(a.item)) for a in arrivals}

    async def build(session: AsyncSession, member_id: int) -> AlertContent | None:
        item_ids = {a.item.id for a in arrivals if a.item.id}
        # 自己订阅的、自己手动下载的都已经收到「入库完成」，这里不重复
        mine = await _subscribed(session, member_id, item_ids) | manually_downloaded(
            member_id, item_ids
        )
        visible = [
            a
            for a in arrivals
            if a.item.id not in mine and await _item_visible(session, member_id, a.item.id or 0)
        ]
        if not visible:
            return None
        lib = library.name
        first = visible[0]
        image = await images[first.item.id]()
        if len(visible) == 1:
            item = first.item
            single = first.units[0] if len(first.units) == 1 else None
            path = await _library_path(session, member_id, item.id or 0, single)
            if first.new_item:
                kind = "新剧" if item.kind == "tv" else "新片"
                body = f"已加入「{lib}」"
                if item.kind == "tv" and first.units != [(0, 0)]:
                    body += f"，{episode_label(first.units)}"
                return AlertContent(
                    title=f"{kind}：{item.title}",
                    body=body + "，点开就能看",
                    image=image,
                    open=path,
                    thread=f"library-{library.id}",
                )
            return AlertContent(
                title=f"{item.title} 更新了",
                body=f"{episode_label(first.units)}已加入「{lib}」",
                image=image,
                open=path,
                thread=f"library-{library.id}",
            )
        names = "、".join(a.item.title for a in visible[:NAMED_IN_SUMMARY])
        more = "等" if len(visible) > NAMED_IN_SUMMARY else ""
        return AlertContent(
            title=f"「{lib}」新增 {len(visible)} 部",
            body=f"{names}{more}",
            image=image,
            open=f"/library/{library.id}",
            thread=f"library-{library.id}",
        )

    return build


def _image_url(item: MediaItem) -> str | None:
    from movieclaw_api.services.channel_push import tmdb_push_image_url

    return tmdb_push_image_url(item.backdrop_path, item.poster_path)


async def check_once(now: datetime | None = None) -> int:
    """检查一轮，返回发出的批次数。"""
    now = now or utcnow()
    async with get_database().session() as session:
        progress = await _progress()
        if progress.started_at is None:
            # 第一次运行：从现在开始算，不回溯已有的库存
            await _save(ArrivalsProgress(started_at=now, marks={}))
            return 0
        batches, marks = await collect_ready(session, now)
    for batch in batches:
        notify(
            "library_new", _recipients(batch.library.id or 0), _content(batch.library, batch.items)
        )
    if marks != {k: v for k, v in progress.marks.items()}:
        await _save(ArrivalsProgress(started_at=progress.started_at, marks=marks))
    if batches:
        logger.info(
            "媒体库新片：%s",
            "、".join(f"{b.library.name} {len(b.items)} 部" for b in batches),
        )
    return len(batches)


_task: asyncio.Task[None] | None = None


async def _loop() -> None:
    while True:
        try:
            await check_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- 一轮出错下一轮再来，不能拖垮应用
            logger.exception("检查媒体库新片时出错")
        await asyncio.sleep(TICK_S)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop())


async def stop() -> None:
    global _task
    task, _task = _task, None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
