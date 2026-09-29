"""轨记忆只记用户的选择：清掉存量里「就是默认挑选」的记录

``playback_state.audio_track`` / ``subtitle_track`` 过去是「正在放的轨」——网页、App、
Jellyfin 客户端每次进度上报都带着当时的轨，默认挑选的结果（包括「文件没有默认字幕、
所以没开」记成的 ``off``）也被当成记忆存下。自本版起只记和默认挑选不同的选择（见
``movieclaw_playback.state.apply_track_selection``），这里把存量里「记着的就是默认挑选」
的值置回 NULL，好让以后调整默认策略（按媒体库语言选字幕、按原声选音轨）时这些条目
跟得上，而不是被一条自动落下的旧值钉死。

**只清「清了之后什么都不变」的那部分**，判断全在内存里做、按下面冻结的旧规则：

- 字幕：记着 ``off`` 且这一集所有文件都没有默认字幕；或记着的轨在每个有它的文件里
  都正是默认挑选（在某个文件里已失效的引用，旧代码本来就回落默认，不妨碍）；
- 音轨：记着的内封轨在每个有它的文件里都是默认音轨；
- 这一集有原盘文件（BDMV / VIDEO_TS / ISO）就整行不动：盘内的轨服务端读不到，
  App 的自研引擎直接按记忆里的序号起播，记忆对它是真值。

于是旧代码读到 NULL 回落的默认挑选，与读到原值的结果完全相同——向前兼容，回退
版本不受影响。唯一的差别在剧集沿用：同一部剧里「最近一集」原本常是一条自动记录
（换算出来等于没有），清掉后会轮到更早那集用户真正换过的选择，这正是本意。

旧规则冻结在本文件里而不是 import 现行代码：默认策略以后还会改，那时补跑这个迁移
（从更老的版本直接升级上来）必须按产生这些记录的旧规则判断，不能按新规则。

downgrade 不恢复：被清掉的值与默认挑选相同，恢复与否对任何版本都没有区别。

Revision ID: f4823bbbae60
Revises: 0af15109570c
Create Date: 2026-09-29 22:00:00.000000
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "f4823bbbae60"
down_revision: str | None = "0af15109570c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

_DISC_CONTAINERS = {"bluray", "dvd", "iso"}
_OFF = "off"
_EMBEDDED = "embedded:"
_EXTERNAL = "external:"
#: 一次 IN 查询带的条目数（SQLite 默认变量上限 999，留余量）
_BATCH = 500


def _json_list(raw: Any) -> list | None:
    """JSON 列的原始值 → list；SQL NULL、文本 'null'、解析不了的都当「不知道」。

    迁移随应用启动执行，任何一行的脏数据都不能让它抛异常（抛了应用就起不来）。
    """
    if raw is None:
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str | bytes) else raw
    except ValueError:
        return None
    return value if isinstance(value, list) else None


def _is_ai_generated(title: Any) -> bool:
    if not isinstance(title, str) or not title:
        return False
    marker = title.strip().lower()
    return marker == "ai" or marker.startswith("ai-")


def _default_subtitle(subtitle_streams: list | None, external_subtitles: list | None) -> str | None:
    """冻结的 pick_default_subtitle：外挂 → AI 生成 → default 旗标 → 非 forced → 稳定序，
    只在「外挂 || default || forced」里挑，全不命中不开字幕。"""
    candidates: list[tuple[bool, bool, bool, bool, int, str]] = []
    order = 0
    for k, raw in enumerate(subtitle_streams or []):
        raw = raw if isinstance(raw, dict) else {}
        default, forced = bool(raw.get("default")), bool(raw.get("forced"))
        candidates.append((False, False, default, forced, order, f"{_EMBEDDED}{k}"))
        order += 1
    for entry in external_subtitles or []:
        if not isinstance(entry, dict) or not entry.get("filename"):
            continue
        candidates.append(
            (
                True,
                _is_ai_generated(entry.get("title")),
                bool(entry.get("default")),
                bool(entry.get("forced")),
                order,
                f"{_EXTERNAL}{entry['filename']}",
            )
        )
        order += 1
    eligible = [c for c in candidates if c[0] or c[2] or c[3]]
    if not eligible:
        return None
    eligible.sort(key=lambda c: (not c[0], not c[1], not c[2], c[3], c[4]))
    return eligible[0][5]


def _default_audio(audio_streams: list | None) -> str | None:
    """冻结的默认音轨：认得出编码的轨里标了默认的，否则第一条。"""
    streams = [s if isinstance(s, dict) else {} for s in audio_streams or []]
    usable = [i for i, t in enumerate(streams) if t.get("codec")] or list(range(len(streams)))
    index = next((i for i in usable if streams[i].get("default")), usable[0] if usable else None)
    return None if index is None else f"{_EMBEDDED}{index}"


def _embedded_index(ref: str) -> int | None:
    if not ref.startswith(_EMBEDDED):
        return None
    try:
        return int(ref[len(_EMBEDDED):])
    except ValueError:
        return None


def _subtitle_exists(ref: str, file: dict) -> bool:
    k = _embedded_index(ref)
    if k is not None:
        return 0 <= k < len(file["subtitle_streams"] or [])
    if ref.startswith(_EXTERNAL):
        name = ref[len(_EXTERNAL):]
        externals = file["external_subtitles"] or []
        return any(isinstance(e, dict) and e.get("filename") == name for e in externals)
    return False


def subtitle_is_automatic(ref: str, files: list[dict]) -> bool:
    """记着的字幕换成 NULL 后，旧代码在这一集每个文件上放出来的都一样吗。"""
    if not files:
        return False
    for file in files:
        if file["subtitle_streams"] is None:
            # 还没探测过内封字幕：App 的自研引擎会自己读出内封轨（可能带默认旗标），
            # 记忆对它仍有作用，判断不了就不动
            return False
        default = _default_subtitle(file["subtitle_streams"], file["external_subtitles"])
        if ref == _OFF:
            if default is not None:
                return False
        elif _subtitle_exists(ref, file) and ref != default:
            return False
    return True


def audio_is_automatic(ref: str, files: list[dict]) -> bool:
    """记着的音轨换成 NULL 后，旧代码在这一集每个文件上放出来的都一样吗。"""
    k = _embedded_index(ref)
    if k is None or not files:
        return False
    for file in files:
        streams = file["audio_streams"] or []
        if k < len(streams) and ref != _default_audio(streams):
            return False
    return True


def clean_automatic_track_memory(bind: sa.engine.Connection) -> int:
    """把存量里「就是默认挑选」的轨记忆置回 NULL，返回改动的行数。"""
    rows = bind.execute(
        sa.text(
            "SELECT id, media_item_id, season_number, episode_number, audio_track, subtitle_track "
            "FROM playback_state WHERE audio_track IS NOT NULL OR subtitle_track IS NOT NULL"
        )
    ).fetchall()
    if not rows:
        return 0

    files_by_unit: dict[tuple[int, int, int], list[dict]] = defaultdict(list)
    item_ids = sorted({row.media_item_id for row in rows})
    query = sa.text(
        "SELECT media_item_id, season_number, episode_number, container, "
        "audio_streams, subtitle_streams, external_subtitles "
        "FROM library_file WHERE state = 'in_place' AND media_item_id IN :ids"
    ).bindparams(sa.bindparam("ids", expanding=True))
    for start in range(0, len(item_ids), _BATCH):
        for f in bind.execute(query, {"ids": item_ids[start : start + _BATCH]}):
            files_by_unit[(f.media_item_id, f.season_number, f.episode_number)].append(
                {
                    "container": f.container,
                    "audio_streams": _json_list(f.audio_streams),
                    "subtitle_streams": _json_list(f.subtitle_streams),
                    "external_subtitles": _json_list(f.external_subtitles),
                }
            )

    updates = []
    for row in rows:
        files = files_by_unit.get((row.media_item_id, row.season_number, row.episode_number), [])
        if any((f["container"] or "") in _DISC_CONTAINERS for f in files):
            continue
        audio = row.audio_track
        subtitle = row.subtitle_track
        if audio is not None and audio_is_automatic(audio, files):
            audio = None
        if subtitle is not None and subtitle_is_automatic(subtitle, files):
            subtitle = None
        if (audio, subtitle) != (row.audio_track, row.subtitle_track):
            updates.append({"id": row.id, "audio": audio, "subtitle": subtitle})

    if updates:
        bind.execute(
            sa.text(
                "UPDATE playback_state SET audio_track = :audio, subtitle_track = :subtitle "
                "WHERE id = :id"
            ),
            updates,
        )
    return len(updates)


def upgrade() -> None:
    cleaned = clean_automatic_track_memory(op.get_bind())
    logger.info("轨记忆口径订正：%d 条观看记录里的自动挑选已清除，改由默认策略决定", cleaned)


def downgrade() -> None:
    # 不恢复：见模块说明
    pass
