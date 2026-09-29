"""add media_metadata.logo_file / logo_locked（片名 Logo 本地资产与选图锁）

片名 Logo（``media_item.logo_path``，透明底 PNG）此前只在订阅首页 Hero 直链
TMDB 图床使用。issue #472：把它像海报/背景一样落成本地资产
（``data/metadata/images/{id}/logo.png``），镜像给外部播放器（条目目录
``clearlogo.png``，Kodi/Jellyfin 命名），并经 Jellyfin 协议的 ``Logo`` 图下发；
「更换图片」增加徽标页，手动选定即锁（与 poster_locked 同款语义）。

存量回填：不写一次性任务。资产下载挂在既有入口上（元数据刷新、入库补齐），
老条目下次刷新时自然补齐；想立刻补齐可手动整库刷新。

回退兼容：纯新增两列（可空 / 带默认值）。旧代码不认识它们，回退后不读不写；
已下载的 logo.png 与镜像出去的 clearlogo.png 对旧代码只是多出来的文件。
无运行时依赖变更，不 bump runtime-version。

Revision ID: 0af15109570c
Revises: 0641cc32b069
Create Date: 2026-09-29 20:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0af15109570c"
down_revision: str | None = "0641cc32b069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_metadata", schema=None) as batch_op:
        batch_op.add_column(sa.Column("logo_file", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("logo_locked", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("media_metadata", schema=None) as batch_op:
        batch_op.drop_column("logo_locked")
        batch_op.drop_column("logo_file")
