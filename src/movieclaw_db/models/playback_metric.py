from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Column, Index, Text
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class PlaybackMetric(TimestampMixin, table=True):
    """一次播放的体验记录（docs/design/playback-qoe.md；最初是 web-player.md §8 的网页播放快照）。

    **2026-09-28 起一次播放一行、按播放编号合并**（playback-qoe.md §2）：App 在用户点下时
    生成 ``attempt_id``，服务端在会话接口先建「已开始」的行，App 离开这次播放时补全；没报结束
    的行由清扫标成「未收尾」。网页播放器仍按旧字段整行上报（``attempt_id`` 为空、
    ``metric_version`` 为 1）。

    北极星从「直通率」改为「无打扰播放率」（``undisturbed``），直通率保留为「对」的分项。
    分组与统计要用的字段放标量列；逐条明细（起播分段、跳转列表、中断、规格快照、时间线……）
    放 ``detail`` 这一个 JSON 列，失败时附带的引擎日志尾巴单放 ``log_tail``，免得列表查询
    把它读出来。

    **为什么要落库**：没有它就答不出这个播放器最重要的那个问题——
    **直通率**（档 0 + 档 1 占全部播放的比例）。这一个数同时代表画质
    （没重编码 = 无损）、速度（秒开）和服务器负担（不烧 GPU），其它指标各自
    只覆盖一个侧面。降档次数则是「决策引擎判错了多少」的直接度量。

    **只落本地**（硬边界 3）：写进自己的数据库、设置页可看、可导出，
    **绝不上报任何外部服务**，也不提供「匿名统计」开关。

    一次播放一行，不存逐事件流水——统计需要的是分布，不是回放。指标口径按
    CTA-2066，不自创，这样「卡顿率」在这里和在别处是同一个东西。
    """

    __tablename__ = "playback_metric"
    #: 按时间窗口统计与按时间清理都走 created_at
    __table_args__ = (Index("ix_playback_metric_created_at", "created_at"),)

    id: int | None = Field(default=None, primary_key=True)
    member_id: int = Field(default=0, index=True, description="归属成员；0=超管（哨兵）")
    library_file_id: int | None = Field(
        default=None, index=True, description="播的哪个文件；文件被删后置空不影响历史统计"
    )

    #: 最终采用的档位（0 直连 / 1 remux / 2 音频单转 / 3 硬件 / 4 软件）。
    #: 直通率就是 tier <= 1 的占比。还没定档就结束（协商阶段失败、出画前退出）记 -1。
    tier: int = Field(index=True)
    #: 降档前的档位；非空即表示上一档播失败了——决策判错的直接证据。
    degraded_from: int | None = Field(default=None)
    #: 播放引擎：direct / hls.js / native-hls。iOS 与桌面的表现差异靠它区分。
    engine: str = Field(default="")
    #: 硬件后端名；软件转码或直通为空。
    hw_backend: str = Field(default="")

    ttff_ms: int | None = Field(default=None, description="点击播放→首帧渲染（毫秒）")
    rebuffer_ms: int = Field(default=0, description="卡顿总时长，不含 seek 等待")
    rebuffer_count: int = Field(default=0)
    seek_count: int = Field(default=0)
    dropped_frames: int | None = Field(default=None)
    total_frames: int | None = Field(default=None)
    watched_ms: int = Field(default=0, description="实际观看时长，卡顿率的分母")

    # —— 播放体验打点（docs/design/playback-qoe.md §5.1）——
    #: 播放编号：App 在用户点下那一刻生成；重连、原位重开、降级、换画质都沿用同一个。
    #: 旧的网页上报为空
    attempt_id: str | None = Field(default=None, unique=True, index=True, max_length=64)
    #: 记录状态：started（服务端已建、App 还没报结束）/ finished / unreported（超时没收尾）/
    #: abnormal（异常退出）
    status: str = Field(default="", index=True, max_length=16)
    #: 这次播放的结局：watched / exited / exit_before_start / failed / abnormal_exit
    outcome: str = Field(default="", max_length=24)
    #: 怎么开始的：tap / auto_next / deeplink
    origin: str = Field(default="", max_length=16)
    #: 客户端：ios / web / jellyfin
    client: str = Field(default="", max_length=16)
    #: 实验室场景名；空 = 真实使用。统计默认排除实验室的播放
    lab_scenario: str = Field(default="", max_length=64)
    #: 口径版本：只比较同口径的记录。旧网页上报为 1，playback-qoe.md 的口径为 2
    metric_version: int = Field(default=1)
    media_item_id: int | None = Field(default=None)
    season_number: int | None = Field(default=None)
    episode_number: int | None = Field(default=None)
    #: 片源类型（分组用）：写入时由台账算好存下，文件删了也不丢
    source_class: str = Field(default="", max_length=24)
    #: 通路：loopback / software / remote_bypass / server_transcode / direct（网页）
    route: str = Field(default="", max_length=24)
    #: 网络：home / away / unknown
    network_class: str = Field(default="", max_length=16)
    #: 接口：wifi / cellular / wired / other
    interface: str = Field(default="", max_length=16)
    app_version: str = Field(default="", max_length=32)

    #: 快：点下 → 首帧出画 / 开始走（毫秒，已扣除用户自己的等待）
    first_frame_ms: int | None = Field(default=None)
    playing_ms: int | None = Field(default=None)
    #: 停在用户手里的等待（确认转码、换画质提示），不算进起播
    user_wait_ms: int = Field(default=0)
    seek_in_count: int = Field(default=0)
    seek_in_p90_ms: int | None = Field(default=None)
    seek_in_max_ms: int | None = Field(default=None)
    seek_out_count: int = Field(default=0)
    seek_out_p90_ms: int | None = Field(default=None)
    seek_out_max_ms: int | None = Field(default=None)

    #: 稳：冻帧、断线重连、按北极星口径计的非自愿中断次数，最后一次错误
    freeze_count: int = Field(default=0)
    freeze_ms: int = Field(default=0)
    reconnect_count: int = Field(default=0)
    reconnect_ms: int = Field(default=0)
    interrupt_count: int = Field(default=0)
    error_kind: str = Field(default="", max_length=48)
    error_category: str = Field(default="", max_length=24)
    error_stage: str = Field(default="", max_length=16)

    #: 对：是否有可避免的规格损失（服务端按规则表判，None = 没有规格快照）、猜错次数
    avoidable_loss: bool | None = Field(default=None)
    misguess_count: int = Field(default=0)

    #: 北极星：这次播放是否全程无打扰（None = 口径 1 的旧记录，不参与）
    undisturbed: bool | None = Field(default=None, index=True)
    ended_at: datetime | None = Field(default=None)
    #: 逐条明细（playback-qoe.md §5.1）
    detail: dict = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, server_default="{}")
    )
    #: 失败 / 异常退出 / 冻帧时附带的引擎日志尾巴（≤ 32 KB，引擎已脱敏）
    log_tail: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
