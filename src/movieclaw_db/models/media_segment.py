"""片头 / 片尾识别台账：一个媒体库文件一行（docs/design/skip-intro.md）。

一行记两件事：

1. **指纹**：这个文件的片头窗 / 片尾窗音频指纹算过没有、成没成。指纹本身是缓存
   目录里的二进制文件（``settings.audio_fingerprint_dir``/``{文件 id}.fp``，可清理），
   这里只记状态。``source_size`` 是算指纹时片源的大小——洗版原地替换、文件被改写后
   大小对不上，就当没算过。
2. **识别结果**：所在季整季比对后，这个文件上的片头、片尾、其他重复段
   （``segments``，毫秒）。``algo_version`` 落后于当前算法版本的行会被回填作业重算。

为什么单独一张表而不是 library_file 上加列：识别是按季批量写的（一集入库，整季
重算），写入频繁、与台账主表的扫描 / 入库写入无关；台账主表是全项目最热的表，
不往上堆派生数据。外键级联删除：文件从库里消失，记录随之消失（SQLite 的行 id
会被复用，裸整数会让新文件继承前任的结果）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Column, ForeignKey, Integer, Text
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class MediaSegmentState(TimestampMixin, table=True):
    """一个文件的指纹状态与片头片尾识别结果。"""

    __tablename__ = "media_segment"

    library_file_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("library_file.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        description="媒体库文件",
    )
    fingerprint_status: str = Field(
        default="pending",
        sa_column=Column(Text, nullable=False, server_default="pending"),
        description="指纹：pending 未算 / ok 已算 / failed 算不了（无音轨、读不了等，见 error）",
    )
    error: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True), description="指纹失败的原因（中文）"
    )
    source_size: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
        description="算指纹时片源的字节数；与台账大小不一致说明片源变了，要重算",
    )
    fingerprinted_at: datetime | None = Field(default=None, description="指纹算好的时间")
    algo_version: int | None = Field(
        default=None, description="识别结果用的算法版本；落后于当前版本的会被重算"
    )
    analyzed_at: datetime | None = Field(
        default=None, description="所在季上一次整季识别的时间；None = 还没识别过"
    )
    segments: list[dict[str, Any]] | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="识别结果：[{type: intro|outro|other, start_ms, end_ms, support, to_end}]",
    )
