"""Agent 自动化两张表：值守任务定义 + 每轮运行留痕

docs/design/agent-automations.md：定时驱动的无人值守会话。定义表承载触发方式、
闸门参数与稳定会话映射；run 表按轮留痕（status/summary/tokens），历史由
cleanup_agent_automations_runs 任务按保留期清理。

Revision ID: a3f8d2c6e914
Revises: 161724435305
Create Date: 2026-10-31 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f8d2c6e914"
down_revision: str | None = "161724435305"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_automation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("owner_member_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("trigger_type", sa.String(), nullable=False),
        sa.Column("trigger_expr", sa.String(), nullable=False),
        sa.Column("delivery_channels", sa.String(), nullable=False),
        sa.Column("quiet_hours", sa.String(), nullable=True),
        sa.Column("max_runs_per_day", sa.Integer(), nullable=False),
        sa.Column("max_tokens_per_run", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_automation_owner_member_id"),
        "agent_automation",
        ["owner_member_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_automation_next_run_at"),
        "agent_automation",
        ["next_run_at"],
        unique=False,
    )

    op.create_table(
        "agent_automation_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("automation_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=False),
        sa.Column("skip_reason", sa.String(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_automation_run_automation_id"),
        "agent_automation_run",
        ["automation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_automation_run_started_at"),
        "agent_automation_run",
        ["started_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_automation_run_status"),
        "agent_automation_run",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_agent_automation_run_status"), table_name="agent_automation_run"
    )
    op.drop_index(
        op.f("ix_agent_automation_run_started_at"), table_name="agent_automation_run"
    )
    op.drop_index(
        op.f("ix_agent_automation_run_automation_id"), table_name="agent_automation_run"
    )
    op.drop_table("agent_automation_run")

    op.drop_index(op.f("ix_agent_automation_next_run_at"), table_name="agent_automation")
    op.drop_index(
        op.f("ix_agent_automation_owner_member_id"), table_name="agent_automation"
    )
    op.drop_table("agent_automation")
