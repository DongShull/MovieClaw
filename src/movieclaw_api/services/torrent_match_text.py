"""site_torrent.match_text（身份匹配检索文本）的写入维护。

检索文本是派生列：主标题、副标题、NER 中外文片名各自归一化后拼接，口径定义在
``movieclaw_matcher.identity.match_text``。发布预测靠它在 SQLite 里做别名子串预筛
（见 subscription/release_forecast.py），所以它必须始终与行的源字段一致。

为什么放在服务层、用 ORM 事件维护：

- db 层只声明列与索引、不含行为——与 attrs 由消费方调用 movieclaw_enrich 算好
  传入是同一分层，db 层不反向依赖匹配内核；
- 必须按行的**最终状态**计算：合并刷新时副标题只补空、attrs 覆盖写，观测里带的
  副标题可能被丢弃，调用方按观测算好传进来会和库里的行对不上；
- 写入路径有新建、合并刷新、富化回填好几处，挂在插入与更新事件上，源字段一变
  就跟着重算，以后新增写入路径也不会漏。

注册时机：本模块被导入即注册，对整个进程生效。所有会写种子索引的模块都显式导入
它。万一有行在注册前写入，它的 match_text 为 NULL，查询端把 NULL 行一律当作
"必须细查"，不会因此漏配，下次被更新时自动补齐。
"""

from __future__ import annotations

from sqlalchemy import event, inspect

from movieclaw_db.models.site_torrent import SiteTorrent
from movieclaw_matcher.identity import match_text

# 检索文本只取决于这三列：任一变化才需要重算
_SOURCE_COLUMNS = ("title", "subtitle", "attrs")


def torrent_match_text(title: str | None, subtitle: str | None, attrs: object) -> str:
    """由种子索引一行的源字段算检索文本；attrs 结构异常时只用标题与副标题。"""
    fields = attrs if isinstance(attrs, dict) else {}
    return match_text(
        title or "",
        subtitle or "",
        _strings(fields.get("titles_zh")),
        _strings(fields.get("titles_en")),
    )


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


@event.listens_for(SiteTorrent, "before_insert")
def _fill_on_insert(_mapper, _connection, target: SiteTorrent) -> None:
    target.match_text = torrent_match_text(target.title, target.subtitle, target.attrs)


@event.listens_for(SiteTorrent, "before_update")
def _refresh_on_update(_mapper, _connection, target: SiteTorrent) -> None:
    state = inspect(target)
    if target.match_text is None or any(
        state.attrs[name].history.has_changes() for name in _SOURCE_COLUMNS
    ):
        target.match_text = torrent_match_text(target.title, target.subtitle, target.attrs)
