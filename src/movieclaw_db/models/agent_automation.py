"""Agent 自动化：定时驱动的无人值守会话（docs/design/agent-automations.md）。

两张表各司其职：

- ``agent_automation``：一条值守任务的定义与调度台账（触发方式、闸门参数、
  稳定会话映射、失败计数）。它是唯一的状态承载——重启不丢，tick 幂等。
- ``agent_automation_run``：每一轮的执行留痕（status / summary / tokens）。
  ``skipped`` 行记录被闸门挡掉的到期（单飞、预算），其余状态见
  ``AutomationRunStatus``。

历史会增长：清理任务（``cleanup_agent_automation_runs``）按保留期归档 run 行。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel

from movieclaw_db.models.base import TimestampMixin, utcnow


class AutomationTriggerType(StrEnum):
    """自动化的触发方式，与定时任务同款两分法。

    - ``INTERVAL``：固定间隔重复（如每 3600 秒一次），``trigger_expr`` 存秒数。
    - ``CRON``：按 5 段 cron 表达式在具体时刻触发（如每天 08:00），
      ``trigger_expr`` 存表达式；求值时区取调度器时区。
    """

    INTERVAL = "interval"
    CRON = "cron"


class AutomationRunStatus(StrEnum):
    """一轮自动化运行的终态。

        RUNNING ──► COMPLETED        （有结论）
                └─► NO_OP            （无事项，不投递）
                └─► FAILED           （会话出错；连续失败触发熔断，见 L5）
                └─► BUDGET_EXCEEDED  （token/墙钟超限，优雅终止，内容已落库）
        SKIPPED                      （被闸门挡掉：单飞、日预算；skip_reason 注明）
    """

    RUNNING = "running"
    COMPLETED = "completed"
    NO_OP = "no_op"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    SKIPPED = "skipped"


class AgentAutomation(TimestampMixin, table=True):
    """一条值守自动化：用户说「每天早上八点给我一份昨日入库汇总」落成的行。

    ``session_id`` 是 automation_id ↔ 会话的稳定映射：每条自动化长期复用
    同一个 Agent 会话，上下文可接续，网页 /sessions/{id} 能看到全部历史轮次
    （owner 前缀 ``automation:`` 标识归属，前端据此挂徽标）。

    v1 边界：仅超管可建可跑（``owner_member_id`` 恒为 0）；事件触发放 v2。
    """

    __tablename__ = "agent_automation"

    id: int | None = Field(default=None, primary_key=True)
    # v1 恒为 0（超管哨兵，与成员级表的口径一致）；成员级自动化待令牌降权后开放
    owner_member_id: int = Field(default=0, index=True, description="归属成员 id（0=超管）")
    name: str = Field(description="自动化名称（列表与推送标题用）")
    # 值守指令全文；写成自包含任务书（背景一句 + 要做什么 + 输出格式 + 拿不准时的处理）
    prompt: str = Field(sa_column=Column(Text, nullable=False), description="值守指令全文")
    trigger_type: AutomationTriggerType = Field(description="触发方式：interval / cron")
    # INTERVAL 存秒数（≥60）；CRON 存标准 5 段表达式，求值时区取调度器时区
    trigger_expr: str = Field(description="触发表达式：秒数或 5 段 cron")
    # 投递渠道：v1 仅 push（结论经推送规则分发，IM/Webhook 跟随事件开关；
    # per-automation 的 im 渠道等事件消费者模型，v2 扩展）。NO-OP 永不投递
    delivery_channels: str = Field(default="push", description="投递渠道（v1：push）")
    # 静默时段 "HH:MM-HH:MM"（如 23:00-07:00，支持跨午夜）；NULL = 全天可跑
    quiet_hours: str | None = Field(default=None, description="静默时段，如 23:00-07:00")
    # 单日最多真实运行轮数（skipped 不计）；默认 12 ≈ 两小时一轮
    max_runs_per_day: int = Field(default=12, description="单日最多运行轮数")
    # 单轮 token 上限（含全部步骤的累计用量）；超限优雅终止并标 budget_exceeded
    max_tokens_per_run: int = Field(default=30000, description="单轮 token 上限")
    enabled: bool = Field(default=True, description="是否启用（连续失败熔断会自动置 false）")
    session_id: str | None = Field(default=None, description="稳定复用的 Agent 会话 id")
    last_run_at: datetime | None = Field(default=None, description="最近一轮真实运行（含失败）")
    # 到期判据（tick 挑 next_run_at <= now 的行）；停机错过的到期合并为一次补跑
    next_run_at: datetime = Field(
        default_factory=utcnow, index=True, description="下次到期时刻（UTC naive）"
    )
    consecutive_failures: int = Field(default=0, description="连续失败计数（成功即清零）")
    last_error: str | None = Field(default=None, description="最近一次失败原因")


class AgentAutomationRun(SQLModel, table=True):
    """一轮自动化运行的留痕。

    不使用 TimestampMixin：本表是事件流水（与 task_run 同款取舍），
    ``started_at`` 即创建时刻。``delivered_at`` 是投递幂等标记——同一轮
    只投递一次（L4 验收：同一 run 重试投递，用户只收到一次）。
    """

    __tablename__ = "agent_automation_run"

    id: int | None = Field(default=None, primary_key=True)
    # 冗余存 automation_id（不做外键）：自动化被删后历史仍可独立留存与查询
    automation_id: int = Field(index=True, description="所属自动化")
    started_at: datetime = Field(default_factory=utcnow, index=True, description="开始时刻")
    finished_at: datetime | None = Field(default=None, description="结束时刻")
    status: AutomationRunStatus = Field(
        default=AutomationRunStatus.RUNNING, index=True, description="终态"
    )
    session_id: str | None = Field(default=None, description="本轮使用的 Agent 会话")
    summary: str | None = Field(default=None, description="本轮结论（有事项时的一段话）")
    tokens_used: int = Field(default=0, description="本轮消耗 token（全部步骤累计）")
    # status=skipped 时注明被哪道闸挡掉：busy / daily_run_limit / daily_token_budget
    skip_reason: str | None = Field(default=None, description="跳过原因（仅 skipped）")
    delivered_at: datetime | None = Field(default=None, description="投递时刻（幂等标记）")
