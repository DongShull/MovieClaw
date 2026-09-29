"""playback_metric 加播放体验打点字段（docs/design/playback-qoe.md §5.1）

原来一次网页播放结束时整行上报一份快照；App 的自研引擎之后要回答「这次播放有没有打扰用户」：
点下到首帧、每次跳转到出画、卡顿 / 冻帧 / 报错、规格有没有可避免的损失、有没有猜错音轨字幕。
改为一次播放一行、按播放编号（attempt_id）合并：服务端在会话接口先建「已开始」的行，App 离开时
补全；逐条明细进一个 JSON 列，失败时附带的引擎日志尾巴单放一列。

回退兼容：纯新增可空 / 带默认值的列与索引，不改、不删任何既有列。旧代码回退后不读不写新列，
网页播放器的旧上报照样落库（新列取默认值）。无运行时依赖变更，不 bump runtime-version。
遥测只落本地，绝不外发（硬边界 3）。

Revision ID: c4d9e2a7b613
Revises: bac523145ec4
Create Date: 2026-09-28 22:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d9e2a7b613"
down_revision: str | None = "bac523145ec4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "playback_metric"

#: (列名, 类型, 默认值)；默认值为 None 的列可空
_COLUMNS: list[tuple[str, sa.types.TypeEngine, str | None]] = [
    ("attempt_id", sa.String(length=64), None),
    ("status", sa.String(length=16), ""),
    ("outcome", sa.String(length=24), ""),
    ("origin", sa.String(length=16), ""),
    ("client", sa.String(length=16), ""),
    ("lab_scenario", sa.String(length=64), ""),
    ("metric_version", sa.Integer(), "1"),
    ("media_item_id", sa.Integer(), None),
    ("season_number", sa.Integer(), None),
    ("episode_number", sa.Integer(), None),
    ("source_class", sa.String(length=24), ""),
    ("route", sa.String(length=24), ""),
    ("network_class", sa.String(length=16), ""),
    ("interface", sa.String(length=16), ""),
    ("app_version", sa.String(length=32), ""),
    ("first_frame_ms", sa.Integer(), None),
    ("playing_ms", sa.Integer(), None),
    ("user_wait_ms", sa.Integer(), "0"),
    ("seek_in_count", sa.Integer(), "0"),
    ("seek_in_p90_ms", sa.Integer(), None),
    ("seek_in_max_ms", sa.Integer(), None),
    ("seek_out_count", sa.Integer(), "0"),
    ("seek_out_p90_ms", sa.Integer(), None),
    ("seek_out_max_ms", sa.Integer(), None),
    ("freeze_count", sa.Integer(), "0"),
    ("freeze_ms", sa.Integer(), "0"),
    ("reconnect_count", sa.Integer(), "0"),
    ("reconnect_ms", sa.Integer(), "0"),
    ("interrupt_count", sa.Integer(), "0"),
    ("error_kind", sa.String(length=48), ""),
    ("error_category", sa.String(length=24), ""),
    ("error_stage", sa.String(length=16), ""),
    ("avoidable_loss", sa.Boolean(), None),
    ("misguess_count", sa.Integer(), "0"),
    ("undisturbed", sa.Boolean(), None),
    ("ended_at", sa.DateTime(), None),
    ("detail", sa.JSON(), "{}"),
    ("log_tail", sa.Text(), ""),
]


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        for name, type_, default in _COLUMNS:
            batch.add_column(
                sa.Column(
                    name,
                    type_,
                    nullable=default is None,
                    server_default=None if default is None else default,
                )
            )
    op.create_index("ix_playback_metric_attempt_id", _TABLE, ["attempt_id"], unique=True)
    op.create_index("ix_playback_metric_status", _TABLE, ["status"], unique=False)
    op.create_index("ix_playback_metric_undisturbed", _TABLE, ["undisturbed"], unique=False)
    op.create_index("ix_playback_metric_created_at", _TABLE, ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_playback_metric_created_at", table_name=_TABLE)
    op.drop_index("ix_playback_metric_undisturbed", table_name=_TABLE)
    op.drop_index("ix_playback_metric_status", table_name=_TABLE)
    op.drop_index("ix_playback_metric_attempt_id", table_name=_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        for name, _type, _default in reversed(_COLUMNS):
            batch.drop_column(name)
