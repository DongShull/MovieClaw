from __future__ import annotations

from typing import Any

from sqlalchemy import Row, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models.base import utcnow
from movieclaw_db.models.rule_set import RuleSet
from movieclaw_db.models.subscription import Subscription, SubscriptionFollower, WantedItem
from movieclaw_db.models.subscription_activity import ActivityType, SubscriptionActivity


class RuleSetRepository:
    """规则组的数据访问层。禁删语义（被引用/默认组）由服务层判断，
    这里只提供"数它被多少订阅引用"的原始查询。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, rule_set_id: int) -> RuleSet | None:
        return await self._session.get(RuleSet, rule_set_id)

    async def get_default(self) -> RuleSet | None:
        result = await self._session.execute(
            select(RuleSet).where(RuleSet.is_default.is_(True))  # type: ignore[attr-defined]
        )
        return result.scalars().first()

    async def list_all(self) -> list[RuleSet]:
        result = await self._session.execute(select(RuleSet).order_by(RuleSet.id))
        return list(result.scalars().all())

    async def count_references(self, rule_set_id: int) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.rule_set_id == rule_set_id)
        )
        return int(result.scalar_one())

    async def reference_counts(self) -> dict[int, int]:
        """{rule_set_id: 引用它的订阅数}。列表页展示与前端禁删提示用。"""
        result = await self._session.execute(
            select(Subscription.rule_set_id, func.count()).group_by(Subscription.rule_set_id)
        )
        return {rule_set_id: int(count) for rule_set_id, count in result.all()}

    async def save(self, row: RuleSet) -> RuleSet:
        row.updated_at = utcnow()
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def set_default(self, row: RuleSet) -> RuleSet:
        """把默认标记转移到 row：清掉旧默认与立新默认在同一事务内完成，
        任何时刻查询 is_default 都恰有一个结果。"""
        result = await self._session.execute(
            select(RuleSet).where(RuleSet.is_default.is_(True))  # type: ignore[attr-defined]
        )
        for old in result.scalars().all():
            if old.id != row.id:
                old.is_default = False
                old.updated_at = utcnow()
                self._session.add(old)
        row.is_default = True
        row.updated_at = utcnow()
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def rollback(self) -> None:
        await self._session.rollback()

    async def delete(self, row: RuleSet) -> None:
        await self._session.delete(row)
        await self._session.commit()


class SubscriptionRepository:
    """订阅与工单的数据访问层。

    工单的批量增删只在"订阅创建/修改 diff"里发生，都走本仓储；
    匹配管线（P4）对工单的状态推进走各自的条件更新，不经过这里。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------

    async def get(self, subscription_id: int) -> Subscription | None:
        return await self._session.get(Subscription, subscription_id)

    async def get_by_media_item(self, media_item_id: int) -> Subscription | None:
        result = await self._session.execute(
            select(Subscription).where(Subscription.media_item_id == media_item_id)
        )
        return result.scalar_one_or_none()

    async def list_all(self, *, kind: str | None = None) -> list[Subscription]:
        # 最近活动时间由数据库触发器在写活动流水时维护；读取路径只走固定字段
        # 与覆盖索引，不扫描工单或聚合活动表。id 兜底保证同一时间戳下顺序稳定。
        query = select(Subscription).order_by(  # type: ignore[attr-defined]
            Subscription.last_activity_at.desc(),
            Subscription.id.desc(),
        )
        if kind is not None:
            query = query.where(Subscription.kind == kind)
        result = await self._session.execute(query)
        return list(result.scalars().all())

    async def save(self, row: Subscription) -> Subscription:
        row.updated_at = utcnow()
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def delete(self, row: Subscription) -> None:
        """删除订阅；工单随外键级联删除（不动已下载的文件与下载器任务）。"""
        await self._session.delete(row)
        await self._session.commit()

    # ------------------------------------------------------------------
    # 关注（docs/design/member-management.md §3.5：订阅全局唯一，
    # 第二个成员再订同一部转为关注；取消互不影响）
    # ------------------------------------------------------------------

    async def add_follower(self, subscription_id: int, member_id: int) -> bool:
        """成员关注订阅（幂等）。返回是否新增了关注行。"""
        existing = await self._get_follower(subscription_id, member_id)
        if existing is not None:
            return False
        self._session.add(
            SubscriptionFollower(subscription_id=subscription_id, member_id=member_id)
        )
        await self._session.commit()
        return True

    async def remove_follower(self, subscription_id: int, member_id: int) -> bool:
        """取消关注。返回是否真的删了一行（本就没关注返回 False）。"""
        row = await self._get_follower(subscription_id, member_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.commit()
        return True

    async def _get_follower(
        self, subscription_id: int, member_id: int
    ) -> SubscriptionFollower | None:
        result = await self._session.execute(
            select(SubscriptionFollower).where(
                SubscriptionFollower.subscription_id == subscription_id,
                SubscriptionFollower.member_id == member_id,
            )
        )
        return result.scalar_one_or_none()

    async def follower_member_ids(self, subscription_id: int) -> list[int]:
        """订阅的关注成员 id，按关注先后（发起人转移时取最早的一个）。"""
        result = await self._session.execute(
            select(SubscriptionFollower.member_id)
            .where(SubscriptionFollower.subscription_id == subscription_id)
            .order_by(SubscriptionFollower.id)
        )
        return list(result.scalars().all())

    async def followed_subscription_ids(self, member_id: int) -> set[int]:
        """成员关注中的订阅 id 集合（"我的订阅"列表的第二个来源）。"""
        result = await self._session.execute(
            select(SubscriptionFollower.subscription_id).where(
                SubscriptionFollower.member_id == member_id
            )
        )
        return set(result.scalars().all())

    # ------------------------------------------------------------------
    # 工单
    # ------------------------------------------------------------------

    async def list_wanted(
        self, subscription_id: int, *, in_scope_only: bool = False
    ) -> list[WantedItem]:
        """列出工单；默认含退出范围的历史行，面向当前业务视图时显式过滤。"""
        query = select(WantedItem).where(WantedItem.subscription_id == subscription_id)
        if in_scope_only:
            query = query.where(WantedItem.in_scope.is_(True))  # type: ignore[attr-defined]
        result = await self._session.execute(
            query.order_by(WantedItem.season_number, WantedItem.episode_number)
        )
        return list(result.scalars().all())

    async def list_wanted_many(
        self,
        subscription_ids: list[int],
        *,
        in_scope_only: bool = False,
        statuses: tuple[str, ...] | None = None,
    ) -> list[WantedItem]:
        """批量读取多条订阅的工单，供首页聚合视图使用，避免逐订阅查询。

        ``statuses`` 只取这几种状态：已入库工单占绝大多数，整行（带预测 JSON）
        读成 ORM 对象是首页聚合里最重的一步，不需要它们的调用方应当滤掉。
        """
        if not subscription_ids:
            return []
        query = select(WantedItem).where(
            WantedItem.subscription_id.in_(subscription_ids)  # type: ignore[union-attr]
        )
        if in_scope_only:
            query = query.where(WantedItem.in_scope.is_(True))  # type: ignore[attr-defined]
        if statuses is not None:
            query = query.where(WantedItem.status.in_(statuses))  # type: ignore[attr-defined]
        result = await self._session.execute(
            query.order_by(
                WantedItem.subscription_id,
                WantedItem.season_number,
                WantedItem.episode_number,
            )
        )
        return list(result.scalars().all())

    async def imported_timings_many(self, subscription_ids: list[int]) -> list[Row[Any]]:
        """批量读取已入库工单（在范围内）的链路时间戳，供首页估算「投递→入库」耗时。

        只取四列、不建 ORM 对象：这类工单是表里的大头，整行读出来只为算中位数
        太重。返回的行可按属性名取 ``subscription_id / grabbed_at /
        downloaded_at / imported_at``。
        """
        if not subscription_ids:
            return []
        result = await self._session.execute(
            select(
                WantedItem.subscription_id,
                WantedItem.grabbed_at,
                WantedItem.downloaded_at,
                WantedItem.imported_at,
            ).where(
                WantedItem.subscription_id.in_(subscription_ids),  # type: ignore[union-attr]
                WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                WantedItem.imported_at.is_not(None),  # type: ignore[union-attr]
            )
        )
        return list(result.all())

    async def add_wanted(self, rows: list[WantedItem]) -> None:
        """批量补工单；与订阅行的变更共用调用方的提交时机。"""
        for row in rows:
            self._session.add(row)
        await self._session.commit()

    async def delete_wanted(self, rows: list[WantedItem]) -> None:
        for row in rows:
            await self._session.delete(row)
        await self._session.commit()

    # ------------------------------------------------------------------
    # 活动流水（订阅可解释性的落点）
    # ------------------------------------------------------------------

    async def add_activity(self, row: SubscriptionActivity) -> None:
        """落一条活动。活动是历史事实，只增不改；数据库同步推进海报墙排序键。"""
        self._session.add(row)
        await self._session.commit()

    async def list_activities(
        self, subscription_id: int, *, limit: int = 100
    ) -> list[SubscriptionActivity]:
        """按时间倒序返回活动流水（时间线展示：最新在前）。"""
        result = await self._session.execute(
            select(SubscriptionActivity)
            .where(SubscriptionActivity.subscription_id == subscription_id)
            .order_by(SubscriptionActivity.id.desc())  # type: ignore[attr-defined]
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_grabbed_activities(
        self, subscription_id: int
    ) -> list[SubscriptionActivity]:
        """返回全部成功投递活动（最新在前），供每集资源耗时回放。

        不能复用时间线默认的 100 条上限：长篇剧的早期集也需要一直显示当时
        从资源发布到被系统拉取的耗时。
        """
        result = await self._session.execute(
            select(SubscriptionActivity)
            .where(
                SubscriptionActivity.subscription_id == subscription_id,
                SubscriptionActivity.type == ActivityType.GRABBED,
            )
            .order_by(SubscriptionActivity.id.desc())  # type: ignore[attr-defined]
        )
        return list(result.scalars().all())

    async def count_wanted_by_status(
        self, subscription_ids: list[int]
    ) -> dict[int, dict[str, int]]:
        """批量统计各订阅的工单状态分布：{订阅id: {状态: 数量}}——列表页进度用。"""
        if not subscription_ids:
            return {}
        result = await self._session.execute(
            select(WantedItem.subscription_id, WantedItem.status, func.count())
            .where(
                WantedItem.subscription_id.in_(subscription_ids),  # type: ignore[attr-defined]
                WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
            )
            .group_by(WantedItem.subscription_id, WantedItem.status)
        )
        counts: dict[int, dict[str, int]] = {}
        for sub_id, status, count in result.all():
            counts.setdefault(sub_id, {})[status] = int(count)
        return counts
