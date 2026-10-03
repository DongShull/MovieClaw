"""业务事件 → App 推送（docs/design/cloud-push.md §5）。

产生点在 ``session.commit()`` 之后调这里的函数，只传 id 和现成的文字；查询都在推送
自己的后台会话里做。规则：

- 订阅类（入库、开始下载、洗版）推给**订阅的人**：发起人（空为管理员）+ 关注者；
- 收件人**看不到的条目一律不推**：走 ``assert_item_visible``（库可见范围 + 内容分级）；
- 新设备登录推给账号本人，不推给刚登录的那台；
- 待处理事项推给管理员，同一个问题的新通知替换旧的（collapse_id）。
"""

from __future__ import annotations

import functools
import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.exceptions import NotFoundException
from movieclaw_api.services.push.images import image_path
from movieclaw_api.services.push.notify import AlertContent, notify

logger = logging.getLogger("movieclaw_api.push.events")


def _never_raise(func):  # type: ignore[no-untyped-def]
    """产生点在业务链路上：推送这边出任何错都只记日志，不能把业务打断。"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            func(*args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("准备 App 推送失败（已忽略）")

    return wrapper


# ----------------------------------------------------------------------
# 收件人与可见性
# ----------------------------------------------------------------------


async def _principal(session: AsyncSession, member_id: int):  # type: ignore[no-untyped-def]
    """按成员 id 造一个请求主体，给可见性判定用；成员不在或停用返回 None。"""
    from movieclaw_api.services.auth import Principal
    from movieclaw_db.repositories.member_repo import MemberRepository

    if member_id == 0:
        return Principal(kind="admin", name="admin", is_admin=True)
    member = await MemberRepository(session).get(member_id)
    if member is None or member.status != "active":
        return None
    return Principal(
        kind="member", name=member.username, member_id=member.id, is_admin=False, member=member
    )


async def _item_visible(session: AsyncSession, member_id: int, item_id: int) -> bool:
    from movieclaw_api.services.library.access import assert_item_visible

    principal = await _principal(session, member_id)
    if principal is None:
        return False
    try:
        await assert_item_visible(session, principal, item_id)
    except NotFoundException:
        return False
    return True


async def _library_path(
    session: AsyncSession, member_id: int, item_id: int, unit: tuple[int, int] | None
) -> str | None:
    """这个人能看到的、放着这部片的库里的条目页；找不到返回 None。"""
    from movieclaw_api.services.library.access import visible_library_ids
    from movieclaw_db.models import LibraryFile

    principal = await _principal(session, member_id)
    if principal is None:
        return None
    visible = await visible_library_ids(session, principal)
    rows = await session.execute(
        select(LibraryFile.library_id).where(  # type: ignore[call-overload]
            LibraryFile.media_item_id == item_id,
            LibraryFile.library_id.is_not(None),  # type: ignore[union-attr]
        )
    )
    libraries = sorted({int(lid) for lid in rows.scalars() if lid is not None and lid in visible})
    if not libraries:
        return None
    path = f"/library/{libraries[0]}/item/{item_id}"
    if unit is not None and unit != (0, 0):
        path += f"?season={unit[0]}&episode={unit[1]}"
    return path


def subscribers(subscription_id: int):  # type: ignore[no-untyped-def]
    """订阅的人：发起人（空为管理员）+ 关注者。"""

    async def resolve(session: AsyncSession) -> set[int]:
        from movieclaw_db.models import Subscription, SubscriptionFollower

        subscription = await session.get(Subscription, subscription_id)
        if subscription is None:
            return set()
        members = {subscription.created_by_member_id or 0}
        rows = await session.execute(
            select(SubscriptionFollower.member_id).where(  # type: ignore[call-overload]
                SubscriptionFollower.subscription_id == subscription_id
            )
        )
        members.update(int(m) for m in rows.scalars())
        return members

    return resolve


def _season_name(season: int) -> str:
    return "特别篇" if season == 0 else f"第 {season} 季"


def episode_label(units: list[tuple[int, int]]) -> str:
    """单元描述（给人看）：电影为空；「第 2 季第 7 集」「第 1 季 8 集」「12 集」。"""
    episodes = sorted({u for u in units if u != (0, 0)})
    if not episodes:
        return ""
    if len(episodes) == 1:
        season, episode = episodes[0]
        return f"{_season_name(season)}第 {episode} 集"
    seasons = {s for s, _ in episodes}
    if len(seasons) == 1:
        return f"{_season_name(next(iter(seasons)))} {len(episodes)} 集"
    return f"{len(episodes)} 集"


def _display_title(title: str, year: int | None) -> str:
    return title.strip() or (f"{year} 年的作品" if year else "一部作品")


def _lazy_image(image_url: str | None):  # type: ignore[no-untyped-def]
    """配图地址签一次、给所有收件人共用（签名在后台任务里做，不占业务链路）。"""
    cache: dict[str, str | None] = {}

    async def get() -> str | None:
        if "v" not in cache:
            cache["v"] = await image_path(image_url)
        return cache["v"]

    return get


# ----------------------------------------------------------------------
# 订阅类事件
# ----------------------------------------------------------------------


@_never_raise
def imported(
    *,
    subscription_id: int,
    item_id: int,
    title: str,
    year: int | None,
    kind: str,
    units: list[tuple[int, int]],
    image_url: str | None,
) -> None:
    """订阅的内容整理进媒体库了。"""
    image = _lazy_image(image_url)
    label = episode_label(units)
    single = units[0] if len(units) == 1 else None
    name = _display_title(title, year)

    async def build(session: AsyncSession, member_id: int) -> AlertContent | None:
        if not await _item_visible(session, member_id, item_id):
            return None
        path = await _library_path(session, member_id, item_id, single)
        if kind == "tv" and label:
            return AlertContent(
                title=f"{name} 更新了",
                body=f"{label}已入库，点开就能看",
                image=await image(),
                open=path or f"/subscriptions/{subscription_id}",
                thread=f"subscription-{subscription_id}",
            )
        return AlertContent(
            title=f"{name} 已入库",
            body="点开就能看",
            image=await image(),
            open=path or f"/subscriptions/{subscription_id}",
            thread=f"subscription-{subscription_id}",
        )

    notify("imported", subscribers(subscription_id), build)


@_never_raise
def download_started(
    *,
    subscription_id: int,
    item_id: int,
    title: str,
    year: int | None,
    units: list[tuple[int, int]],
    detail: str,
    upgrade: bool,
    image_url: str | None,
) -> None:
    """订阅找到资源、交给下载器了。"""
    image = _lazy_image(image_url)
    label = episode_label(units)
    name = _display_title(title, year)
    verb = "开始洗版下载" if upgrade else "开始下载"

    async def build(session: AsyncSession, member_id: int) -> AlertContent | None:
        if not await _item_visible(session, member_id, item_id):
            return None
        return AlertContent(
            title=f"{verb}：{name}",
            body=" · ".join(part for part in (label, detail) if part),
            image=await image(),
            open=f"/subscriptions/{subscription_id}",
            thread=f"subscription-{subscription_id}",
        )

    notify("download_started", subscribers(subscription_id), build)


@_never_raise
def upgraded(
    *,
    subscription_id: int,
    item_id: int,
    title: str,
    year: int | None,
    unit: tuple[int, int],
    old_label: str,
    new_label: str,
    image_url: str | None,
) -> None:
    """订阅的内容换成了更好的版本。"""
    image = _lazy_image(image_url)
    label = episode_label([unit])
    name = _display_title(title, year)

    async def build(session: AsyncSession, member_id: int) -> AlertContent | None:
        if not await _item_visible(session, member_id, item_id):
            return None
        return AlertContent(
            title=f"洗版完成：{name}",
            body=" · ".join(part for part in (label, f"{old_label} → {new_label}") if part),
            image=await image(),
            open=f"/subscriptions/{subscription_id}",
            thread=f"subscription-{subscription_id}",
        )

    notify("upgraded", subscribers(subscription_id), build)


# ----------------------------------------------------------------------
# 账号安全
# ----------------------------------------------------------------------


@_never_raise
def new_device(
    *, member_id: int, device_id: int, name: str, kind_label: str, ip: str | None
) -> None:
    """有新的 App、命令行或转码器登录了这个账号：告诉本人的其他设备。"""
    where = f"，来源 {ip}" if ip else ""

    async def build(_session: AsyncSession, _member_id: int) -> AlertContent:
        return AlertContent(
            title="新设备登录了你的账号",
            body=(
                f"「{name}」（{kind_label}）刚刚登录{where}。"
                "不是你本人的话，去「账号 → 设备」注销它。"
            ),
            open="/settings/devices",
            thread="account",
        )

    notify("new_device", {member_id}, build, exclude_device_ids=frozenset({device_id}))


# ----------------------------------------------------------------------
# 待处理事项（管理员）
# ----------------------------------------------------------------------


def notice_path(source: str, payload: dict) -> str:
    """待处理事项的跳转：能修它的页面（与网页、App 的待处理列表同一套映射）。"""
    if source == "subscription":
        subscription_id = payload.get("subscription_id")
        return f"/subscriptions/{subscription_id}" if subscription_id else "/subscriptions"
    return {
        "ingest": "/settings/import-watch",
        "downloader": "/settings/downloaders",
        "site": "/settings/sites",
        "cloud": "/settings/cloud",
    }.get(source, "/settings")


@_never_raise
def system_alert(*, dedupe_key: str, source: str, title: str, message: str, payload: dict) -> None:
    """待处理事项新出现或复发：推给管理员。同一个问题的新通知替换旧的。"""

    async def build(_session: AsyncSession, _member_id: int) -> AlertContent:
        return AlertContent(
            title=title,
            body=message,
            open=notice_path(source, payload),
            thread="system",
        )

    notify("system_alert", {0}, build, collapse=("notice", dedupe_key))
