"""add site_torrent.match_text（身份匹配检索文本）+ 预筛覆盖索引

发布预测此前每轮都把近 90 天的全部种子索引（NAS 上 5.3 万行）读进 Python，逐行
pydantic 校验、逐行身份匹配，只为找出在追剧那几十行——NAS 上一轮 9–15 秒，放进
线程也照样和接口抢解释器锁，撞上时起播相关接口慢 10–100 倍。

检索文本 = 主标题、副标题、NER 中外文片名各自归一化（NFKC + casefold + 只留
字母数字）后用 \\x1f 拼接。任何别名能命中一行，归一化后的别名就一定是该行检索
文本的子串，所以发布预测可以先在 SQLite 里按别名子串预筛：扫描在 C 层完成、
不占 Python 解释器，只把几十行可能相关的种子交给匹配内核细查。覆盖索引
(publish_time, match_text) 让预筛只读索引页，不读带 attrs 大 JSON 的整行。

存量回填：本迁移一次性算好全部存量行（NAS 5.8 万行约数秒，发生在启动迁移阶段，
此时还没开始服务请求）。归一化口径冻结在下面的 ``_normalize``——迁移不 import
应用代码；运行期的口径在 movieclaw_matcher.identity.match_text，二者一致由测试
守护（tests/api/test_torrent_match_text.py）。

回退兼容：纯新增一个可空列与一个索引。旧代码不认识它，回退后不读不写；回退期间
旧代码写入的行 match_text 为 NULL，新代码查询时把 NULL 行一律当作"必须细查"，
不会漏配。无运行时依赖变更，不 bump runtime-version。

Revision ID: bac523145ec4
Revises: 5b8e2c4f9a17
Create Date: 2026-09-27 18:00:00.000000
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bac523145ec4"
down_revision: str | None = "5b8e2c4f9a17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 回填分批：每批读写这么多行，内存与单次事务都有界
_BATCH = 2000
#: 字段分隔符：不是字母数字，归一化后的别名不可能跨字段命中
_SEPARATOR = "\x1f"


def _normalize(text: str) -> str:
    """与 movieclaw_matcher.identity.normalize_title 同口径（冻结在写下时的事实）。"""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _match_text(title: str | None, subtitle: str | None, attrs_json: str | None) -> str:
    try:
        attrs = json.loads(attrs_json) if attrs_json else {}
    except ValueError:
        attrs = {}
    if not isinstance(attrs, dict):
        attrs = {}
    parts = [
        title or "",
        subtitle or "",
        *_strings(attrs.get("titles_zh")),
        *_strings(attrs.get("titles_en")),
    ]
    return _SEPARATOR.join(_normalize(part) for part in parts if part)


def upgrade() -> None:
    op.add_column("site_torrent", sa.Column("match_text", sa.Text(), nullable=True))

    bind = op.get_bind()
    last_id = 0
    while True:
        rows = bind.execute(
            sa.text(
                "SELECT id, title, subtitle, attrs FROM site_torrent "
                "WHERE id > :last ORDER BY id LIMIT :batch"
            ),
            {"last": last_id, "batch": _BATCH},
        ).fetchall()
        if not rows:
            break
        bind.execute(
            sa.text("UPDATE site_torrent SET match_text = :text WHERE id = :id"),
            [
                {"id": row_id, "text": _match_text(title, subtitle, attrs)}
                for row_id, title, subtitle, attrs in rows
            ],
        )
        last_id = rows[-1][0]

    op.create_index("ix_site_torrent_publish_match", "site_torrent", ["publish_time", "match_text"])


def downgrade() -> None:
    op.drop_index("ix_site_torrent_publish_match", table_name="site_torrent")
    op.drop_column("site_torrent", "match_text")
