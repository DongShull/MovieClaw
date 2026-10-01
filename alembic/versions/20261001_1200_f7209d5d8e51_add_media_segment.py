"""add media_segment table and library.detect_media_segments

跳过片头 / 片尾（docs/design/skip-intro.md）：

- ``media_segment``：一个媒体库文件一行，记音频指纹的状态与整季识别出的片头片尾；
- ``library.detect_media_segments``：库级开关「识别片头片尾」，**默认开**（用户决策
  2026-10-01），存量库一并为开（列的服务器默认值即为 1）。只对剧集库起作用。

向前兼容：纯新增表与带默认值的新列、无数据改写。回退到旧版本时旧代码不认识它们、
也不会去读，「跳过片头」随旧版本一起消失，不影响其他功能。

Revision ID: f7209d5d8e51
Revises: 5dbae6a0c453
Create Date: 2026-10-01 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7209d5d8e51"
down_revision: str | None = "5dbae6a0c453"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_segment",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("library_file_id", sa.Integer(), nullable=False),
        sa.Column("fingerprint_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("source_size", sa.BigInteger(), nullable=True),
        sa.Column("fingerprinted_at", sa.DateTime(), nullable=True),
        sa.Column("algo_version", sa.Integer(), nullable=True),
        sa.Column("analyzed_at", sa.DateTime(), nullable=True),
        sa.Column("segments", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["library_file_id"], ["library_file.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("library_file_id"),
    )
    with op.batch_alter_table("library") as batch:
        batch.add_column(
            sa.Column(
                "detect_media_segments",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("library") as batch:
        batch.drop_column("detect_media_segments")
    op.drop_table("media_segment")
