"""文件来源快照（docs/design/library-duplicate-files.md §2）。

回答用户在文件区最常问的一句话：**这个文件是怎么进库的**。台账早就存着
``source`` / ``site_id`` / ``torrent_id``，但它们是给机器用的键，不是给人看的
话；而"哪条监听规则、硬链还是复制、哪个下载器、是首次投递还是洗版投递、
是自动还是人工选种"这些入库现场知道的事，此前一样也没落。

设计原则与 ``trash_context`` 相同：**存展示用快照、不外键**。订阅可以取消、
监听规则可以删、种子表会滚动，快照文案永远能读。快照只有三个键，就是
文件区要显示的三样：

    {"kind": "subscription", "label": "订阅《九门》自动投递",
     "detail": "HDSky · Nine.Gates…-CHDWEB · qBittorrent · 硬链接入库"}

本模块是媒体库自己的部分：监听识别与扫描发现的构造、展示时的取值规则（``origin_of``）。
订阅投递 / 手动下载的文案，以及给旧行（``origin IS NULL``）读时推导等价快照，要读订阅与
下载的表，归获取领域（``services/acquisition_origin.py``，经
``acquisition.current().describe_origins`` 取）。
"""

from __future__ import annotations

import logging
from typing import Any

from movieclaw_api.i18n.stored import reference
from movieclaw_db.models import FileSource, LibraryFile
from movieclaw_db.models.import_watch import ImportWatch

logger = logging.getLogger("movieclaw_api.library.origin")

KIND_SUBSCRIPTION = "subscription"
KIND_MANUAL_DOWNLOAD = "manual_download"
KIND_WATCH_IMPORT = "watch_import"
KIND_SCAN = "scan"

# 扫描触发方式 → 文案。扫描本身分不清"文件是谁放进来的"（那正是它与入库
# 管线的区别），能说的只有"哪一轮扫描第一次看见它"
SCAN_TRIGGER_LABELS = {
    "manual": "手动扫描",
    "watch": "目录监听",
    "scheduled": "定时对账",
}
_STRATEGY_LABELS = {"hardlink": "硬链接入库", "copy": "复制入库"}


def _join(*parts: str | None) -> str | None:
    """detail 行：有值的段按固定顺序用「 · 」拼起来；一个都没有为 None。"""
    text = " · ".join(str(p).strip() for p in parts if p and str(p).strip())
    return text or None


def _snapshot(
    kind: str,
    label: str,
    detail: str | None,
    *,
    label_ref: dict | None = None,
    detail_ref: dict | None = None,
) -> dict[str, Any]:
    result = {"kind": kind, "label": label, "detail": detail}
    for name, ref in (("label", label_ref), ("detail", detail_ref)):
        if ref is not None and (name != "detail" or detail is not None):
            result[f"{name}_key"] = ref["message_key"]
            result[f"{name}_params"] = ref["params"]
    return result


def _detail_reference(*parts: str | dict | None) -> dict:
    parts = [p.strip() if isinstance(p, str) else p for p in parts if p]
    return reference(
        "library.origin.detail",
        parts={
            "message_list": [p for p in parts if p],
            "separator_key": "library.origin.separator",
        },
    )


def _strategy_reference(strategy: str | None) -> dict | None:
    return reference(f"library.origin.{strategy}") if strategy in _STRATEGY_LABELS else None


# ---------------------------------------------------------------------------
# 写：监听识别 / 扫描现场的构造（订阅投递 / 手动下载的在 acquisition_origin）
# ---------------------------------------------------------------------------


def watch_import_origin(rule: ImportWatch) -> dict[str, Any]:
    """监听目录识别入库：没匹配到任何下载任务，只知道从哪个目录、怎么搬进来的。"""
    detail = _join(
        rule.source_path,
        _STRATEGY_LABELS.get(rule.strategy or ""),
        "未匹配到任何下载任务",
    )
    return _snapshot(
        KIND_WATCH_IMPORT,
        "监听目录自动识别入库",
        detail,
        label_ref=reference("library.origin.watchImport"),
        detail_ref=_detail_reference(
            rule.source_path,
            _strategy_reference(rule.strategy),
            reference("library.origin.unmatched"),
        ),
    )


def scan_origin(trigger: str) -> dict[str, Any]:
    """存量扫描发现：文件不是本系统放进来的，只记哪一轮扫描第一次看见它。

    ``trigger`` 取 ``SCAN_TRIGGER_LABELS`` 的键；未知值原样写进 detail。
    """
    how = SCAN_TRIGGER_LABELS.get(trigger, trigger)
    trigger_keys = {"manual": "scanManual", "watch": "scanWatch", "scheduled": "scanScheduled"}
    return _snapshot(
        KIND_SCAN,
        "存量扫描发现（非本系统入库）",
        f"{how}发现",
        label_ref=reference("library.origin.scan"),
        detail_ref=reference(
            "library.origin.scanFound",
            trigger=(
                reference(f"library.origin.{trigger_keys[trigger]}")
                if trigger in trigger_keys
                else trigger
            ),
        ),
    )


# ---------------------------------------------------------------------------
# 读：旧行的读时推导
# ---------------------------------------------------------------------------


def _legacy_fallback(row: LibraryFile) -> dict[str, Any]:
    """旧行的粗文案（没有获取领域、或它也推导不出时）。站点名、种子标题由获取领域推导（describe_origins）。"""
    if row.source == FileSource.SCANNED:
        return _snapshot(
            KIND_SCAN,
            "存量扫描发现（非本系统入库）",
            None,
            label_ref=reference("library.origin.scan"),
        )
    if row.site_id or row.torrent_id:
        return _snapshot(
            KIND_WATCH_IMPORT,
            "手动或监听导入",
            _join(row.site_id),
            label_ref=reference("library.origin.manualOrWatch"),
        )
    return _snapshot(
        KIND_WATCH_IMPORT,
        "监听目录导入",
        "来源种子未记录",
        label_ref=reference("library.origin.legacyWatch"),
        detail_ref=reference("library.origin.missingTorrent"),
    )


def origin_of(row: LibraryFile, derived: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """展示用：优先落库快照，其次读时推导，最后按 source 的粗文案。"""
    if row.origin:
        return row.origin
    if row.id is not None and row.id in derived:
        return derived[row.id]
    return _legacy_fallback(row)
