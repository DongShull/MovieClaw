"""Agent 自动化的对外 API 模型（docs/design/agent-automations.md §5）。

请求模型的校验只做形状（长度/范围）；触发表达式与静默时段的语义校验在
服务层做——cron 解析与时段求值需要 APScheduler/时区，属于服务职责。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field, field_serializer

from movieclaw_api.schemas.base import BaseModel
from movieclaw_db.models.agent_automation import (
    AgentAutomation,
    AgentAutomationRun,
)


class AutomationUpsertPayload(BaseModel):
    """创建 / 更新自动化的载荷（update 全字段可选，None = 不改）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", min_length=0, max_length=100, description="自动化名称")
    prompt: str = Field(
        default="", min_length=0, max_length=8000, description="值守指令全文（自包含任务书）"
    )
    trigger_type: Literal["interval", "cron"] | None = None
    trigger_expr: str | None = Field(
        default=None, max_length=100, description="秒数（interval，≥60）或 5 段 cron 表达式"
    )
    delivery_channels: str | None = Field(
        default=None,
        max_length=20,
        description="投递渠道（v1 仅 push；IM 跟随「账号 → 通知」的事件开关）",
    )
    quiet_hours: str | None = Field(
        default=None, max_length=11, description="静默时段 HH:MM-HH:MM；空串 = 清除"
    )
    max_runs_per_day: int | None = Field(default=None, ge=1, le=96)
    max_tokens_per_run: int | None = Field(default=None, ge=1000, le=1_000_000)
    enabled: bool | None = None


class AutomationRunView(BaseModel):
    """一轮运行（或被闸门跳过）的留痕投影。"""

    id: int
    automation_id: int
    started_at: datetime
    finished_at: datetime | None
    status: str
    session_id: str | None
    summary: str | None
    tokens_used: int
    skip_reason: str | None
    delivered_at: datetime | None

    @field_serializer("started_at", "finished_at", "delivered_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        from movieclaw_api.schemas.agent import _iso_utc

        return _iso_utc(value)

    @classmethod
    def from_model(cls, row: AgentAutomationRun) -> AutomationRunView:
        return cls(
            id=row.id or 0,
            automation_id=row.automation_id,
            started_at=row.started_at,
            finished_at=row.finished_at,
            status=getattr(row.status, "value", row.status),
            session_id=row.session_id,
            summary=row.summary,
            tokens_used=row.tokens_used,
            skip_reason=row.skip_reason,
            delivered_at=row.delivered_at,
        )


class AutomationView(BaseModel):
    """自动化列表 / 详情项：定义 + 调度台账 + 最近一轮摘要。"""

    id: int
    name: str
    prompt: str
    trigger_type: str
    trigger_expr: str
    delivery_channels: str
    quiet_hours: str | None
    max_runs_per_day: int
    max_tokens_per_run: int
    enabled: bool
    session_id: str | None
    last_run_at: datetime | None
    next_run_at: datetime
    consecutive_failures: int
    last_error: str | None
    last_run: AutomationRunView | None = Field(description="最近一轮（含 skipped）摘要")

    @field_serializer("last_run_at", "next_run_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        from movieclaw_api.schemas.agent import _iso_utc

        return _iso_utc(value)

    @classmethod
    def from_model(
        cls, row: AgentAutomation, last_run: AgentAutomationRun | None = None
    ) -> AutomationView:
        return cls(
            id=row.id or 0,
            name=row.name,
            prompt=row.prompt,
            trigger_type=getattr(row.trigger_type, "value", row.trigger_type),
            trigger_expr=row.trigger_expr,
            delivery_channels=row.delivery_channels,
            quiet_hours=row.quiet_hours,
            max_runs_per_day=row.max_runs_per_day,
            max_tokens_per_run=row.max_tokens_per_run,
            enabled=row.enabled,
            session_id=row.session_id,
            last_run_at=row.last_run_at,
            next_run_at=row.next_run_at,
            consecutive_failures=row.consecutive_failures,
            last_error=row.last_error,
            last_run=AutomationRunView.from_model(last_run) if last_run else None,
        )


class AutomationPolicyView(BaseModel):
    """全局闸门（设置页开关 + 全局日 token 预算）。"""

    enabled: bool
    daily_token_budget: int


class AutomationPolicyPayload(BaseModel):
    """全局闸门更新载荷；None = 不改。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    daily_token_budget: int | None = Field(default=None, ge=1000)
