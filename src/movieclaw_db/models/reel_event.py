"""刷片事件：沉浸式竖滑刷片里每一条片段「被怎么看了」的记录。

设计见 ``docs/design/reels.md``。一期只做记录、不做个性化：攒下来的数据用来调
片段长度、挑点规则，以及量「滑到出画面」的等待时间。几个取舍：

- **一行一个事件**，不是一条片段一行：曝光、首帧、离开、看完、接着看是先后发生的，
  App 攒一批再报，服务端原样落库，统计时再按 ``reel_id`` 归并。
- **记原片坐标**：``file_id`` + ``position_ms`` 都是原片时间轴，与片段怎么放
  （一期从原片中间起播，将来可能是预剪好的小文件）无关；``mode`` 记当时是哪种放法，
  将来两种放法可以直接对比。
- **不进观看记录**：刷片不写 ``playback_state`` / ``playback_log``，「继续观看」、
  播放次数都不受影响；这张表是它唯一的落点。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin
from movieclaw_db.models.member_scoped import MemberScopedMixin, register_member_scoped


@register_member_scoped
class ReelEvent(MemberScopedMixin, TimestampMixin, table=True):
    """一行 = 某人在刷片里对某条片段的一个动作。``member_id`` 0 为超管（哨兵）。"""

    __tablename__ = "reel_event"

    id: int | None = Field(default=None, primary_key=True)
    reel_id: str = Field(
        sa_column=Column(Text, nullable=False),
        description="片段标识（rl_<文件id>_<起点毫秒>），同一段在不同次刷片里相同",
    )
    kind: str = Field(
        sa_column=Column(Text, nullable=False),
        description="事件：impression 曝光 / first_frame 出画面 / leave 滑走 / "
        "complete 看完 / continue 接着看 / open 看正片 / fail 放不出",
    )
    mode: str = Field(
        default="seek",
        sa_column=Column(Text, nullable=False, server_default="seek"),
        description="放法：seek=从原片中间起播；clip=预剪好的片段文件（将来）",
    )
    media_item_id: int | None = Field(default=None, index=True, description="条目")
    file_id: int | None = Field(default=None, description="原片文件（台账行 id）")
    position_ms: int | None = Field(default=None, description="事件发生时在原片上的位置")
    watched_ms: int | None = Field(
        default=None, description="这一条累计看了多久（leave / complete）"
    )
    wait_ms: int | None = Field(
        default=None, description="从滑到这一条到出第一个画面等了多久（first_frame）"
    )
    detail: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="补充信息（失败原因、网络类型等），键由 App 决定",
    )
