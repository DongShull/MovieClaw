"""「生成章节」改为默认关闭：存量库一并关掉

docs/design/video-chapters.md §4.5：章节场景图每个文件要 seek 抓帧 8～12 次，
首轮回填在 NAS 上动辄数小时，章节横排与 Jellyfin 合成章节也随之全开——
这对多数库是白付的 CPU 与读取量。改为默认关闭、按库自行打开（用户决策
2026-09-29）。

``library.extract_chapter_images`` 自 d4e5f6a7b8c9 起默认开，存量库几乎都是
"没选过、被默认打开"的，无从区分谁是主动开的，所以**全部置为关**；想要章节
的库在编辑库里重新打开即可——已生成的图原样保留，打开后立即恢复显示，并在
后台只补缺的那部分。

只改数据、不重建列默认值：SQLite 改列默认值只能整表重建，而 ``library`` 被
十张表外键引用；ORM 写库时总是显式带上这一列（模型默认值已改为 False），
数据库层的 ``DEFAULT 1`` 永远用不到。

向前兼容：旧代码读到 False 就不生成章节，行为与"用户手动关掉"一致。
回退（downgrade）不恢复旧值：迁移前哪些库是用户主动打开的已无从得知。

Revision ID: 0641cc32b069
Revises: c4d9e2a7b613
Create Date: 2026-09-29 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0641cc32b069"
down_revision: str | None = "c4d9e2a7b613"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE library SET extract_chapter_images = 0"))


def downgrade() -> None:
    # 不恢复：见模块说明
    pass
