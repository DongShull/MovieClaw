"""add collection.created_by_member_id

成员权限 v2（docs/design/member-permissions-v2.md §3.6）：全家共享（household）合集
记录创建者，改名 / 改规则 / 改名单 / 删除只允许超管与创建者，修掉「任何成员都能改删
别人的共享合集」的越权。

- ``created_by_member_id``：0 = 超管（哨兵值，与 member_scoped 同一约定，不是外键）。
  存量合集一律归超管——此前无从得知是谁建的，归超管是唯一不扩大成员权限的选择。

向前兼容：只加带服务器默认值的列、无数据改写。回退到旧版本时旧代码不读这一列，
合集回到旧的（不校验创建者的）行为。

Revision ID: 3c8e1f5a7b20
Revises: f7209d5d8e51
Create Date: 2026-10-01 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c8e1f5a7b20"
down_revision: str | None = "f7209d5d8e51"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("collection") as batch:
        batch.add_column(
            sa.Column(
                "created_by_member_id",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("collection") as batch:
        batch.drop_column("created_by_member_id")
