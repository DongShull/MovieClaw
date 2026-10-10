"""小艺云 A2A 会话映射（``xiaoyi_a2a_session`` 表）的数据访问层。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_db.models.base import utcnow
from movieclaw_db.models.xiaoyi_a2a_session import XiaoyiA2aSession


class XiaoyiA2aSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def set_mapping(self, platform_session_id: str, agent_session_id: str) -> None:
        """绑定 / 改绑平台会话到指定 AI 会话（upsert）。"""
        row = await self._session.get(XiaoyiA2aSession, platform_session_id)
        if row is None:
            row = XiaoyiA2aSession(
                platform_session_id=platform_session_id, agent_session_id=agent_session_id
            )
        else:
            row.agent_session_id = agent_session_id
            row.updated_at = utcnow()
        self._session.add(row)
        await self._session.commit()


async def get_xiaoyi_session_mapping(platform_session_id: str) -> str | None:
    """独立会话工厂版的读取：取映射的 AI 会话 id；无映射返回 None。"""
    from movieclaw_db.engine import get_database

    async with get_database().session() as session:
        row = await session.get(XiaoyiA2aSession, platform_session_id)
        return row.agent_session_id if row else None
