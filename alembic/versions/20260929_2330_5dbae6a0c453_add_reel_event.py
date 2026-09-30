"""add reel_event table

刷片（沉浸式竖滑看片段）的事件记录：曝光、出画面、滑走、看完、接着看
（docs/design/reels.md）。一期只落库不做个性化，用来调片段长度与挑点规则。

向前兼容：纯新增表、无回填。回退到旧版本时旧代码不认识这张表、也不会去读它，
刷片入口随旧版本一起消失，不影响其他功能。

Revision ID: 5dbae6a0c453
Revises: f4823bbbae60
Create Date: 2026-09-29 23:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5dbae6a0c453"
down_revision: str | None = "f4823bbbae60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reel_event",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reel_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False, server_default="seek"),
        sa.Column("media_item_id", sa.Integer(), nullable=True),
        sa.Column("file_id", sa.Integer(), nullable=True),
        sa.Column("position_ms", sa.Integer(), nullable=True),
        sa.Column("watched_ms", sa.Integer(), nullable=True),
        sa.Column("wait_ms", sa.Integer(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("reel_event", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_reel_event_member_id"), ["member_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_reel_event_media_item_id"), ["media_item_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("reel_event", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_reel_event_media_item_id"))
        batch_op.drop_index(batch_op.f("ix_reel_event_member_id"))
    op.drop_table("reel_event")
