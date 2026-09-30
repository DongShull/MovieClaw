"""刷片接口的请求 / 响应模型（docs/design/reels.md）。

``play`` 刻意做成**一个**带 ``mode`` 的模型、各放法的字段都可空，而不是按 mode
分成多个模型的联合：生成的 Swift 模型解码一个带可空字段的结构最稳，将来加
``clip``（预剪好的片段文件）只是多几个可空字段，老 App 照常解码、按 ``modes``
声明根本收不到它不会放的条目。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from movieclaw_api.schemas.base import BaseModel


class ReelEpisodeView(BaseModel):
    season: int = Field(description="季号")
    episode: int = Field(description="集号")
    name: str | None = Field(default=None, description="集名")
    overview: str | None = Field(default=None, description="分集简介")


class ReelPersonView(BaseModel):
    name: str = Field(description="姓名")
    tmdb_person_id: int | None = Field(
        default=None, description="TMDB 影人 ID（打开人物页用）；只有姓名时为空"
    )
    avatar_url: str | None = Field(default=None, description="头像（TMDB 图床地址）")


class ReelTitleView(BaseModel):
    """这一条属于哪部片：展示用的信息。图片地址都是不带 /api/v1 的相对路径或完整外链。"""

    media_item_id: int = Field(description="条目 id")
    library_id: int = Field(description="这一条的文件所在的媒体库（分享要用）")
    kind: Literal["movie", "tv"] = Field(description="电影 / 剧集")
    name: str = Field(description="片名")
    year: int | None = Field(default=None, description="年份")
    rating: float | None = Field(default=None, description="评分（0～10）")
    runtime_minutes: int | None = Field(default=None, description="片长；剧集是这一集的时长")
    genres: list[str] = Field(default_factory=list, description="类型，最多 3 个")
    tagline: str | None = Field(default=None, description="宣传语")
    overview: str | None = Field(
        default=None, description="简介（剧集是整剧的，分集简介在 episode 里）"
    )
    favorite: bool = Field(default=False, description="本人收藏了没有（电影 / 整剧）")
    played: bool = Field(default=False, description="本人看过没有（电影看整部，剧集看这一集）")
    directors: list[ReelPersonView] = Field(
        default_factory=list, description="电影是导演、剧集是主创，最多两位"
    )
    poster_url: str | None = Field(default=None, description="海报")
    backdrop_url: str | None = Field(default=None, description="横版剧照")
    logo_url: str | None = Field(default=None, description="片名 Logo（本地资产）")
    episode: ReelEpisodeView | None = Field(default=None, description="剧集：这一段出自哪一集")


class ReelSegmentView(BaseModel):
    """放原片的哪一段（原片时间轴，与怎么放无关）。"""

    file_id: int = Field(description="原片文件（台账行 id）")
    start_ms: int = Field(description="起点（落在关键帧上）")
    end_ms: int = Field(description="终点（落在两句对白之间）")
    duration_ms: int | None = Field(default=None, description="原片总长（剧集是这一集）")
    method: str = Field(
        description="挑法：bitrate 码率最高段 / chapter 章节起点 / position 固定位置"
    )


class ReelByteRangeView(BaseModel):
    offset: int = Field(description="起始字节")
    length: int = Field(description="长度")
    purpose: str = Field(description="head 文件头 / index 索引 / start 起点后约 4 秒")


class ReelSubtitleView(BaseModel):
    ordinal: int = Field(description="内封字幕的同类型序号（embedded:<k> 的 k）")
    language: str | None = None
    title: str | None = None
    codec: str | None = None


class ReelPlayView(BaseModel):
    """怎么放这一条。mode=seek：自研引擎打开原片、从 segment.start_ms 起播。"""

    mode: str = Field(description="放法：seek=从原片中间起播（一期仅此一种）")
    stream_url: str | None = Field(
        default=None, description="seek：原片取流地址（带 /api/v1 的相对路径，含令牌）"
    )
    size_bytes: int | None = Field(
        default=None, description="seek：原片大小（片源字节缓存的键要用）"
    )
    audio_ordinal: int | None = Field(default=None, description="seek：起播音轨的同类型序号")
    subtitle: ReelSubtitleView | None = Field(
        default=None, description="seek：要显示的中文字幕；None 不开"
    )
    prefetch: list[ReelByteRangeView] = Field(
        default_factory=list, description="seek：上一条播放期间应预取的字节范围"
    )


class ReelItemView(BaseModel):
    id: str = Field(description="片段标识（事件上报用）")
    title: ReelTitleView
    cover_url: str | None = Field(default=None, description="封面：起点那一帧；没有时是剧照")
    segment: ReelSegmentView
    play: ReelPlayView


class ReelFeedView(BaseModel):
    seed: int = Field(description="这次刷片的随机种子，翻页时原样带回")
    next_offset: int = Field(description="下一页的 offset")
    has_more: bool = Field(description="后面还有没有")
    items: list[ReelItemView] = Field(default_factory=list)


class ReelGenreView(BaseModel):
    name: str = Field(description="类型名（如「剧情」）")
    count: int = Field(description="能刷到的片有几部")


class ReelEventIn(BaseModel):
    reel_id: str = Field(max_length=64, description="片段标识")
    kind: Literal["impression", "first_frame", "leave", "complete", "continue", "open", "fail"] = (
        Field(
            description="impression 曝光 / first_frame 出画面 / leave 滑走 / complete 看完 / "
            "continue 接着看 / open 看正片 / fail 放不出"
        )
    )
    mode: str = Field(default="seek", max_length=16, description="当时的放法")
    media_item_id: int | None = None
    file_id: int | None = None
    position_ms: int | None = Field(default=None, ge=0, description="原片上的位置")
    watched_ms: int | None = Field(default=None, ge=0, description="这一条累计看了多久")
    wait_ms: int | None = Field(default=None, ge=0, description="滑到这一条到出画面等了多久")
    detail: dict[str, Any] | None = Field(default=None, description="补充信息")


class ReelEventBatch(BaseModel):
    events: list[ReelEventIn] = Field(max_length=200, description="一批事件")


class ReelEventResult(BaseModel):
    accepted: int = Field(description="落库条数")
