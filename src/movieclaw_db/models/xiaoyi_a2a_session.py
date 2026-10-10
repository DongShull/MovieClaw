"""小艺云 A2A 会话映射表（platform sessionId → 本机 agent_session）。

⚠️ 定位：只是「平台会话 id → AI 会话 id」的路由映射，不存任何对话内容
（内容的事实源在 JSONL 转录 + agent_session 索引，见 agent_session.py）。
一行一个平台 sessionId；``clearContext`` 不是删行而是把该行指到新会话。
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel

from movieclaw_db.models.base import TimestampMixin


class XiaoyiA2aSession(TimestampMixin, SQLModel, table=True):
    __tablename__ = "xiaoyi_a2a_session"

    #: 平台侧 sessionId（message/stream params.sessionId，用户清理上下文后会变）
    platform_session_id: str = Field(primary_key=True)
    #: 映射到的本机 AI 会话 id（agent_session.id）
    agent_session_id: str = Field(index=True)
