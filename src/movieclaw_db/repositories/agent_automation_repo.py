"""Agent 自动化两张表的数据访问层（docs/design/agent-automations.md §4）。

与 scheduled_task_repo 同款约定：构造时接会话、方法自行提交；调用点都在
服务层/后台任务，就地提交不打断上层事务语义。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models.agent_automation import (
    AgentAutomation,
    AgentAutomationRun,
    AutomationRunStatus,
)
from movieclaw_db.models.base import utcnow

#: 列表页「最近一次 run 摘要」取几条就够——只取每个自动化最新的一行
_LATEST_RUN_LIMIT = 1


class AgentAutomationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, automation_id: int) -> AgentAutomation | None:
        return await self._session.get(AgentAutomation, automation_id)

    async def list_all(self) -> list[AgentAutomation]:
        """全部自动化（创建顺序倒序，新建的排前面）。"""
        stmt = select(AgentAutomation).order_by(AgentAutomation.id.desc())
        return list((await self._session.execute(stmt)).scalars().all())

    async def due(self, now: datetime) -> list[AgentAutomation]:
        """节拍候选（L1）：启用且已到期；错过多次到期的行也只出现一次（合并补跑）。"""
        stmt = (
            select(AgentAutomation)
            .where(AgentAutomation.enabled == True)  # noqa: E712 - SQLModel 布尔等值
            .where(AgentAutomation.next_run_at <= now)  # type: ignore[arg-type]
            .order_by(AgentAutomation.next_run_at)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def create(self, automation: AgentAutomation) -> AgentAutomation:
        self._session.add(automation)
        await self._session.commit()
        await self._session.refresh(automation)
        return automation

    async def save(self, automation: AgentAutomation) -> None:
        automation.updated_at = utcnow()
        self._session.add(automation)
        await self._session.commit()

    async def delete(self, automation: AgentAutomation) -> None:
        """删除自动化定义（run 历史保留，冗余的 automation_id 不做外键）。"""
        await self._session.delete(automation)
        await self._session.commit()

    # ------------------------------------------------------------------
    # 台账维护（L1/L5）
    # ------------------------------------------------------------------
    async def bind_session(self, automation_id: int, session_id: str) -> None:
        """把稳定会话映射写回自动化行（automation_id ↔ session_id）。"""
        row = await self.get(automation_id)
        if row is None:
            return
        row.session_id = session_id
        await self.save(row)

    async def count_runs_since(self, automation_id: int, since: datetime) -> int:
        """某时刻以来该自动化的**真实运行**轮数（skipped 不占日预算）。"""
        stmt = (
            select(func.count())
            .select_from(AgentAutomationRun)
            .where(
                AgentAutomationRun.automation_id == automation_id,  # type: ignore[arg-type]
                AgentAutomationRun.started_at >= since,  # type: ignore[arg-type]
                AgentAutomationRun.status != AutomationRunStatus.SKIPPED,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def tokens_used_since(self, since: datetime) -> int:
        """某时刻以来**所有**自动化消耗的 token 总量（全局日预算的口径）。"""
        stmt = (
            select(func.coalesce(func.sum(AgentAutomationRun.tokens_used), 0))
            .select_from(AgentAutomationRun)
            .where(
                AgentAutomationRun.started_at >= since,  # type: ignore[arg-type]
                AgentAutomationRun.status != AutomationRunStatus.SKIPPED,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def latest_run(self, automation_id: int) -> AgentAutomationRun | None:
        """最近一轮（含 skipped），列表页摘要与 run-now 回显用。"""
        stmt = (
            select(AgentAutomationRun)
            .where(AgentAutomationRun.automation_id == automation_id)  # type: ignore[arg-type]
            .order_by(AgentAutomationRun.id.desc())
            .limit(_LATEST_RUN_LIMIT)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def list_runs(
        self, automation_id: int, *, offset: int = 0, limit: int = 50
    ) -> tuple[list[AgentAutomationRun], int]:
        """run 历史分页（L5 验收：run 历史 API 分页可查），返回 (rows, total)。"""
        base = select(AgentAutomationRun).where(
            AgentAutomationRun.automation_id == automation_id  # type: ignore[arg-type]
        )
        total = int(
            (
                await self._session.execute(
                    select(func.count()).select_from(base.subquery())
                )
            ).scalar_one()
        )
        stmt = base.order_by(AgentAutomationRun.id.desc()).offset(offset).limit(limit)
        rows = list((await self._session.execute(stmt)).scalars().all())
        return rows, total

    async def get_run(self, run_id: int) -> AgentAutomationRun | None:
        return await self._session.get(AgentAutomationRun, run_id)

    async def save_run(self, run: AgentAutomationRun) -> None:
        self._session.add(run)
        await self._session.commit()

    async def purge_runs_older_than(self, cutoff: datetime) -> int:
        """L6 维护：删除保留期之前的 run 历史，返回删除条数。"""
        result = await self._session.execute(
            delete(AgentAutomationRun).where(
                AgentAutomationRun.started_at < cutoff  # type: ignore[arg-type]
            )
        )
        await self._session.commit()
        return int(result.rowcount or 0)
