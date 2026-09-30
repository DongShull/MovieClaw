"""刷片：沉浸式上下滑动看片段（docs/design/reels.md）。

- ``GET /reels``：一页片段。第一页不带 ``seed``，服务端生成后随响应返回，翻页时
  原样带回；``modes`` 声明 App 会放的方式（一期只有 ``seek``）。
- ``GET /reels/genres``：能刷到的类型（顶部「全部」下拉的选项），``GET /reels?genre=`` 只刷这一类。
- ``POST /reels/events``：App 攒一批刷片事件报上来，只落 ``reel_event`` 表，
  不写观看记录。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.api.deps import require_login
from movieclaw_api.schemas.reels import ReelEventBatch, ReelEventResult, ReelFeedView, ReelGenreView
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.reels.feed import build_feed, list_genres, record_events
from movieclaw_db.engine import get_session

router = APIRouter(prefix="/reels", tags=["reels"])


@router.get(
    "",
    response_model=ApiResponse[ReelFeedView],
    summary="刷片：取一页片段",
    operation_id="reels.feed",
    openapi_extra={"x-cli-hidden": True},
)
async def get_reel_feed(
    seed: Annotated[int | None, Query(ge=0, description="随机种子；第一页不传")] = None,
    offset: Annotated[int, Query(ge=0, description="从抽样顺序的第几部开始")] = 0,
    limit: Annotated[int, Query(ge=1, le=20, description="这一页最多几条")] = 10,
    modes: Annotated[str, Query(description="App 会放的方式，逗号分隔；一期只有 seek")] = "seek",
    genre: Annotated[
        str | None, Query(max_length=32, description="只刷这个类型（如「剧情」）；不传是全部")
    ] = None,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ReelFeedView]:
    page = await build_feed(
        session,
        principal,
        seed=seed,
        offset=offset,
        limit=limit,
        modes={m.strip() for m in modes.split(",") if m.strip()},
        genre=genre.strip() if genre else None,
    )
    return ok(
        ReelFeedView(
            seed=page.seed,
            next_offset=page.next_offset,
            has_more=page.has_more,
            items=page.items,  # type: ignore[arg-type]
        )
    )


@router.get(
    "/genres",
    response_model=ApiResponse[list[ReelGenreView]],
    summary="刷片：能刷到的类型",
    operation_id="reels.genres",
    openapi_extra={"x-cli-hidden": True},
)
async def get_reel_genres(
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[ReelGenreView]]:
    genres = await list_genres(session, principal)
    return ok([ReelGenreView(name=name, count=count) for name, count in genres])


@router.post(
    "/events",
    response_model=ApiResponse[ReelEventResult],
    summary="刷片：上报事件",
    operation_id="reels.events",
    openapi_extra={"x-cli-hidden": True},
)
async def post_reel_events(
    payload: ReelEventBatch,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ReelEventResult]:
    member_id = principal.member_id if principal.member_id is not None else 0
    accepted = await record_events(session, member_id, [e.model_dump() for e in payload.events])
    return ok(ReelEventResult(accepted=accepted))
