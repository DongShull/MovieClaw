#!/usr/bin/env python3
"""iOS 性能实验室的 API 侧工具（``ios_lab.sh`` 调用，也可单独用）。

实验室的进程编排、数据目录与端口见 ``ios_lab.sh``；这里是所有「要跟后端说话」的步骤。
一律像 App 一样用设备令牌说话（``POST /api/v1/auth/device/login`` 换令牌，之后
``Authorization: Bearer``），请求的路径与查询参数逐字照搬 App 的实际请求
（``LibraryHomeView.swift`` / ``SubscriptionsView.swift`` 及其 ``Endpoints.swift``），
这样预热、压测与验收命中的都是 App 真正会命中的缓存键与查询。

子命令
------
- ``prepare-settings``：首次启动**之前**用后端自己的 SettingStore 关掉 Jellyfin 兼容层——
  否则后端一启动就去抢 UDP 7359（本机别的实例正占着，也不该碰）；
- ``setup``：经真实 API 完成初始化：引导创建管理员（``POST /auth/bootstrap``）、建成员、
  把图片与 TMDB 的出网代理指到假图床（``PUT /network/config``）、登记假 qBittorrent 为
  默认下载器、建 24 个合集、写管理员的首页行清单（``PUT /ui/preferences``）；
- ``stats``：数据集统计（直接数库 + 经接口数两个首页各块）；
- ``warm``：按 App 的请求形态把两个首页用到的图片全部请求一遍（预热服务端图片缓存）；
- ``bench``：串行压测两个首页的接口：服务刚启动后的第一次（冷）+ 热态 P50/P95、响应体积，
  并从访问日志对回服务端耗时；
- ``verify``：经链路模拟代理（默认 18601 与 18602）用 **curl** 验收每个接口与每类图片。

用法::

    python scripts/perf/ios_lab.py setup
    python scripts/perf/ios_lab.py bench --repeat 15
    python scripts/perf/ios_lab.py verify --via 18601 --via 18602
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx

LAB = Path(os.environ.get("MC_LAB_DIR", str(Path.home() / "workspace" / ".mc-perf-lab")))
DIRECT = os.environ.get("MC_LAB_ORIGIN", "http://127.0.0.1:18600")
PASSWORD = "perf-lab-2026"
EGRESS_PROXY = "http://127.0.0.1:18603"
QBT_URL = "http://127.0.0.1:18604"
QBT_NAME = "实验室 qBittorrent"
ROW_LIMIT = 20  # App 首页每行取 20 个（LibraryHomeView.rowCount）


def _die(message: str) -> None:
    raise SystemExit(f"错误：{message}")


# ---------------------------------------------------------------------------
# 客户端：与 App 同一套设备令牌
# ---------------------------------------------------------------------------


class Api:
    """带设备令牌的同步客户端。``origin`` 可以是后端直连口，也可以是链路模拟代理口。"""

    def __init__(
        self, origin: str = DIRECT, user: str | None = None, *, timeout: float = 180
    ) -> None:
        self.origin = origin.rstrip("/")
        self.http = httpx.Client(base_url=self.origin + "/api/v1", timeout=timeout)
        self.token: str | None = None
        if user:
            self.login(user)

    def login(self, user: str) -> str:
        # 固定的 installation_id：重复登录只替换本工具自己的那枚令牌，不会堆出一串设备
        body = {
            "username": user,
            "password": PASSWORD,
            "client": {
                "kind": "ios",
                "installation_id": f"perf-lab-tool-{user}",
                "name": f"性能实验室工具（{user}）",
                "platform": "perf-lab",
                "client_version": "perf-lab",
            },
        }
        data = self._unwrap(self.http.post("/auth/device/login", json=body))
        self.token = data["token"]
        self.http.headers["Authorization"] = f"Bearer {self.token}"
        return self.token

    @staticmethod
    def _unwrap(response: httpx.Response):
        try:
            payload = response.json()
        except ValueError:
            _die(
                f"{response.request.method} {response.request.url} → "
                f"{response.status_code}（非 JSON）"
            )
        if response.status_code >= 400 or not payload.get("success", False):
            _die(
                f"{response.request.method} {response.request.url.path} → {response.status_code} "
                f"{payload.get('message')} {payload.get('details') or ''}"
            )
        return payload.get("data")

    def get(self, path: str, **params):
        return self._unwrap(
            self.http.get(path, params={k: v for k, v in params.items() if v is not None})
        )

    def post(self, path: str, body: dict | None = None):
        return self._unwrap(self.http.post(path, json=body or {}))

    def put(self, path: str, body: dict):
        return self._unwrap(self.http.put(path, json=body))


# ---------------------------------------------------------------------------
# prepare-settings：首启前关掉 Jellyfin 兼容层（走后端自己的配置内核）
# ---------------------------------------------------------------------------


def cmd_prepare_settings(_args: argparse.Namespace) -> None:
    from movieclaw_api.core.config import get_settings
    from movieclaw_api.settings import get_setting_store, init_setting_store
    from movieclaw_api.settings.schemas import JellyfinCompatSetting
    from movieclaw_db.crypto import init_secret_box
    from movieclaw_db.engine import get_database, init_db

    async def run() -> None:
        settings = get_settings()
        init_db(settings.database_url, cache_mb=settings.db_cache_mb)
        init_secret_box(settings.master_key, Path(settings.secret_key_file))
        init_setting_store()
        store = get_setting_store()
        current = await store.get(JellyfinCompatSetting)
        await store.set(current.model_copy(update={"enabled": False}))
        await get_database().dispose()

    asyncio.run(run())
    print("已关闭 Jellyfin 兼容层（jellyfin.compat.enabled=false）：实验室后端不会去监听 UDP 7359")


# ---------------------------------------------------------------------------
# setup：经真实 API 初始化
# ---------------------------------------------------------------------------

# 规则驱动的合集：(名字, 所属库, 规则, 合集内默认排序)。规则与库收藏范围同构
_RULE_COLLECTIONS = [
    ("科幻宇宙", "电影", [("genres", [878])], "rating"),
    ("动作大片", "电影", [("genres", [28])], "release_date"),
    ("高分电影 8.5+", "电影", [("rating_gte", [8.5])], "rating"),
    ("2020 年代新片", "电影", [("decades", ["2020s"])], "added_at"),
    ("喜剧时光", "电影", [("genres", [35])], "title"),
    ("悬疑惊悚", "电影", [("genres", [53])], "rating"),
    ("4K HDR 精选", "4K 电影", [("hdr", [True])], "added_at"),
    ("2160p 收藏", "4K 电影", [("resolutions", ["2160p"])], "title"),
    ("华语高分", "华语电影", [("rating_gte", [8.0])], "rating"),
    ("犯罪剧集", "剧集", [("genres", [80])], "added_at"),
    ("国产剧", "剧集", [("origin_countries", ["CN"])], "added_at"),
    ("美剧高分", "美剧", [("rating_gte", [8.0])], "rating"),
    ("奇幻动画", "动画", [("genres", [10765])], "rating"),
    ("纪录精选", "纪录片", [("genres", [99])], "release_date"),
    ("千禧年代经典", "经典老片", [("decades", ["2000s", "1990s"])], "release_date"),
    ("日韩高分", "日韩剧", [("rating_gte", [8.0])], "rating"),
]
# 名单驱动的合集：(名字, 所属库 / None=跨库, 取材的库, 部数, 排序, 可见性)
_LIST_COLLECTIONS = [
    ("周末片单", "电影", ["电影"], 30, "added_at", "household"),
    ("诺兰全集", "电影", ["电影"], 8, "release_date", "household"),
    ("年度最佳 2025", "电影", ["电影", "4K 电影"], 20, "rating", "household"),
    ("家庭电影夜", None, ["电影", "华语电影", "4K 电影"], 24, "title", "household"),
    ("想看清单", None, ["电影", "华语电影", "纪录片"], 40, "added_at", "household"),
    ("下饭剧", "剧集", ["剧集"], 25, "title", "household"),
    ("童年回忆", "动画", ["动画"], 15, "release_date", "household"),
    ("私藏", None, ["电影", "经典老片"], 12, "title", "private"),
]


def cmd_setup(_args: argparse.Namespace) -> None:
    api = Api(DIRECT)
    if not api.get("/auth/bootstrap")["initialized"]:
        api.post("/auth/bootstrap", {"username": "admin", "password": PASSWORD})
        print("✓ 管理员 admin 已创建（首次引导接口 POST /auth/bootstrap）")
    api.login("admin")

    members = api.get("/members")
    member = next((m for m in members if m["username"] == "member"), None)
    if member is None:
        member = api.post(
            "/members", {"username": "member", "password": PASSWORD, "nickname": "家人"}
        )
    member = api.put(
        f"/members/{member['id']}",
        {"nickname": "家人", "allow_subscribe": True, "all_libraries": True},
    )
    print(f"✓ 成员 member（id={member['id']}）：可浏览全部库、可订阅")

    current = api.get("/network/config")
    api.put(
        "/network/config",
        {
            "proxy_mode": "manual",
            "proxy_url": EGRESS_PROXY,
            "proxy_services": ["image", "tmdb"],
            "tmdb_api_base_url": current["tmdb_api_base_url"],
            "tmdb_image_base_url": current["tmdb_image_base_url"],
            "douban_api_base_url": current["douban_api_base_url"],
        },
    )
    print(
        f"✓ 网络出口：手动代理 {EGRESS_PROXY}，走代理的服务 = image + tmdb"
        f"（图床默认地址 {current['mirror_defaults'].get('tmdb_image_base_url')}）"
    )

    downloaders = api.get("/downloaders")
    row = next((d for d in downloaders if d["name"] == QBT_NAME), None)
    if row is None:
        row = api.post(
            "/downloaders",
            {
                "name": QBT_NAME,
                "client_type": "qbittorrent",
                "url": QBT_URL,
                "username": "admin",
                "password": "adminadmin",
            },
        )
    for _ in range(60):  # 连接测试在后台跑：轮询到出结果
        row = api.get(f"/downloaders/{row['id']}")
        if row["status"] in ("active", "failed"):
            break
        time.sleep(0.25)
    if not row["is_default"]:
        api.post(f"/downloaders/{row['id']}/default")
    print(
        f"✓ 下载器「{QBT_NAME}」{QBT_URL}：{row['status']} "
        f"{row.get('version') or row.get('last_error') or ''}"
    )

    collections = _ensure_collections(api)
    _home_rows(api, collections)


def _ensure_collections(api: Api) -> dict[str, dict]:
    libraries = {lib["name"]: lib for lib in api.get("/libraries", scope="all")}
    existing = {
        c["name"]: c for c in api.get("/collections", include_empty="true", include_hidden="true")
    }
    rng = random.Random(20260929)
    pools: dict[str, list[int]] = {}

    def pool(name: str) -> list[int]:
        if name not in pools:
            items = api.get(f"/libraries/{libraries[name]['id']}/items", sort="rating", limit=200)
            pools[name] = [item["media_item_id"] for item in items]
        return pools[name]

    for name, library, rules, sort in _RULE_COLLECTIONS:
        if name not in existing:
            existing[name] = api.post(
                "/collections",
                {
                    "name": name,
                    "library_id": libraries[library]["id"],
                    "sort": sort,
                    "rules": [{"field": f, "op": "any_of", "values": v} for f, v in rules],
                },
            )
    for name, library, sources, count, sort, visibility in _LIST_COLLECTIONS:
        if name not in existing:
            candidates = [i for source in sources for i in pool(source)]
            existing[name] = api.post(
                "/collections",
                {
                    "name": name,
                    "library_id": libraries[library]["id"] if library else None,
                    "sort": sort,
                    "visibility": visibility,
                    "item_ids": rng.sample(candidates, k=min(count, len(candidates))),
                },
            )
    user = [c for c in existing.values() if c.get("builtin") is None]
    print(
        f"✓ 合集 {len(user)} 个（规则驱动 {len(_RULE_COLLECTIONS)}、"
        f"名单驱动 {len(_LIST_COLLECTIONS)}，"
        "另有每库一个内置「我的收藏」）"
    )
    return existing


def _home_rows(api: Api, collections: dict[str, dict]) -> None:
    """管理员的首页行：出厂布局之外加 4 行合集、1 行「评分最高的电影」，藏起「演唱会」库行。"""
    libraries = {lib["name"]: lib["id"] for lib in api.get("/libraries", scope="all")}
    order = [
        "电影",
        "剧集",
        "动画",
        "4K 电影",
        "华语电影",
        "美剧",
        "日韩剧",
        "纪录片",
        "经典老片",
        "综艺",
        "儿童",
        "演唱会",
    ]
    rows: list[dict] = [
        {"id": "up-next"},
        {"id": "favorites"},
        {"id": "libraries"},
        {"id": "row:weekend", "collection_id": collections["周末片单"]["id"]},
    ]
    for name in order:
        rows.append(
            {"id": f"lib:{libraries[name]}", **({"hidden": True} if name == "演唱会" else {})}
        )
        if name == "电影":
            rows.append(
                {
                    "id": "row:top-movies",
                    "library_id": libraries["电影"],
                    "sort": "rating",
                    "name": "评分最高的电影",
                }
            )
        if name == "日韩剧":
            rows.append({"id": "row:scifi", "collection_id": collections["科幻宇宙"]["id"]})
    rows += [
        {"id": "row:family", "collection_id": collections["家庭电影夜"]["id"]},
        {"id": "row:watchlist", "collection_id": collections["想看清单"]["id"]},
    ]
    prefs = api.get("/ui/preferences")
    prefs["home"] = {"rows": rows}
    api.put("/ui/preferences", prefs)
    print(f"✓ 管理员首页行 {len(rows)} 行（含 4 行合集、1 行自加库行，「演唱会」库行隐藏）")


# ---------------------------------------------------------------------------
# App 的请求形态：首页行合并（HomeRows.build 的 Python 版）与图片地址
# ---------------------------------------------------------------------------


def image_url(raw: str | None, variant: str | None = None) -> str | None:
    """同 App 的 ServerAddress.imageURL：远程图走 /images/proxy，相对路径直连（资产可带预设）。"""
    if not raw:
        return None
    if raw.startswith(("http://", "https://")):
        query = {"url": raw, **({"variant": variant} if variant else {})}
        return "/images/proxy?" + urlencode(query)
    path = raw.replace("\\", "/")
    if path.startswith("/api/v1/"):
        path = path[len("/api/v1") :]
    if variant and path.startswith("/images/assets/"):
        path += ("&" if "?" in path else "?") + f"variant={variant}"
    return path


def original_tmdb(raw: str | None) -> str | None:
    """同 App 的 originalTMDBImageURL：TMDB 图升级到 original 档（Hero 用）。"""
    return re.sub(r"/t/p/w\d+/", "/t/p/original/", raw) if raw else None


@dataclass
class Row:
    kind: str  # up-next / favorites / libraries / library / collection
    ident: str
    path: str | None = None  # 这一行要打的条目请求


def home_rows(prefs: dict, libraries: list[dict], collections: list[dict]) -> list[Row]:
    """把首页偏好与可见库、合集合并成要渲染的行（只保留可见行），口径同 LibraryHomeRows.swift。"""
    visible = [lib for lib in libraries if lib.get("viewer_access", True)]
    lib_by_id = {lib["id"]: lib for lib in visible}
    col_by_id = {col["id"]: col for col in collections}

    def library_row(
        ident: str, lib: dict, sort: str | None, order: str | None, unwatched: bool
    ) -> Row:
        sort = sort or "added_at"
        params = {"sort": sort, **({"order": order} if order else {}), "limit": ROW_LIMIT}
        watch = "seen" if sort == "last_played" else "unwatched" if unwatched else None
        if watch:
            params["w"] = watch
        return Row("library", ident, f"/libraries/{lib['id']}/items?{urlencode(params)}")

    defaults = [
        Row("up-next", "up-next"),
        Row("favorites", "favorites"),
        Row("libraries", "libraries"),
    ]
    defaults += [
        library_row(f"lib:{lib['id']}", lib, None, None, False)
        for lib in visible
        if not lib.get("exclude_from_home")
    ]
    saved = prefs.get("home", {}).get("rows") or []
    if not saved:
        return defaults
    rows: list[Row] = []
    seen: set[str] = set()
    for pref in saved:
        ident, hidden = pref["id"], pref.get("hidden") is True
        if ident in seen:
            continue
        row: Row | None = None
        if ident in ("up-next", "favorites", "libraries"):
            row = Row(ident, ident)
        elif ident.startswith("lib:"):
            lib = lib_by_id.get(int(ident[4:]))
            if lib and not lib.get("exclude_from_home"):
                row = library_row(
                    ident, lib, pref.get("sort"), pref.get("order"), bool(pref.get("unwatched"))
                )
        elif pref.get("collection_id") in col_by_id:
            col = col_by_id[pref["collection_id"]]
            params = {
                "limit": ROW_LIMIT,
                "sort": pref.get("sort") or col["sort"],
                **({"order": pref["order"]} if pref.get("order") else {}),
            }
            row = Row("collection", ident, f"/collections/{col['id']}/items?{urlencode(params)}")
        elif pref.get("library_id") in lib_by_id:
            row = library_row(
                ident,
                lib_by_id[pref["library_id"]],
                pref.get("sort"),
                pref.get("order"),
                bool(pref.get("unwatched")),
            )
        if row is None:
            continue
        seen.add(ident)
        if not hidden:
            rows.append(row)
    for row in defaults:  # 没存过的内置行、库行补在后面
        if row.ident not in seen:
            seen.add(row.ident)
            rows.append(row)
    return rows


def library_home_requests(api: Api) -> tuple[list[tuple[str, str]], dict]:
    """媒体库首页一次完整加载的全部接口请求（标签, 路径）与返回数据。"""
    prefs = api.get("/ui/preferences")
    libraries = api.get("/libraries", scope="all")
    collections = api.get("/collections")
    rows = home_rows(prefs, libraries, collections)
    names = {lib["id"]: lib["name"] for lib in libraries}
    requests = [
        ("ui/preferences", "/ui/preferences"),
        ("libraries?scope=all", "/libraries?scope=all"),
        ("collections", "/collections"),
        ("playback/up-next", f"/playback/up-next?limit={ROW_LIMIT}"),
        (
            "playback/favorites",
            f"/playback/favorites?limit={ROW_LIMIT}&offset=0"
            "&unwatched_first=true&sort=favorited_at",
        ),
    ]
    fetched: set[str] = set()
    for row in rows:
        if row.path and row.path not in fetched:
            fetched.add(row.path)
            match = re.match(r"/(libraries|collections)/(\d+)/", row.path)
            label = (
                f"库「{names.get(int(match.group(2)), '?')}」{row.ident}"
                if match and match.group(1) == "libraries"
                else f"合集行 {row.ident}"
            )
            requests.append((f"{label} {row.path.split('?', 1)[1]}", row.path))
    # 「我的媒体库」行的库卡片封面：每个可见库一次「最近添加 20」（与默认库行共用缓存键）
    for lib in libraries:
        path = f"/libraries/{lib['id']}/items?sort=added_at&limit={ROW_LIMIT}"
        if lib.get("viewer_access", True) and path not in fetched:
            fetched.add(path)
            requests.append((f"库卡片「{lib['name']}」sort=added_at", path))
    return requests, {
        "prefs": prefs,
        "libraries": libraries,
        "collections": collections,
        "rows": rows,
    }


def subscriptions_requests(is_admin: bool) -> list[tuple[str, str]]:
    requests = [
        ("subscriptions", "/subscriptions"),
        ("today-arrivals?window=week", "/subscriptions/today-arrivals?window=week"),
        ("recent-arrivals", "/subscriptions/recent-arrivals"),
    ]
    if is_admin:
        requests += [
            ("downloaders/tasks", "/downloaders/tasks"),
            ("automation-readiness", "/subscriptions/automation-readiness"),
        ]
    return requests


# ---------------------------------------------------------------------------
# 两个首页用到的图片（App 的图片地址口径）
# ---------------------------------------------------------------------------


def library_home_images(api: Api) -> dict[str, list[str]]:
    requests, ctx = library_home_requests(api)
    images: dict[str, list[str]] = defaultdict(list)
    for lib in ctx["libraries"]:
        items = api.get(f"/libraries/{lib['id']}/items", sort="added_at", limit=ROW_LIMIT)
        if items or lib.get("custom_cover"):
            images["库封面 /libraries/{id}/cover"].append(f"/libraries/{lib['id']}/cover")
    for item in api.get("/playback/up-next", limit=ROW_LIMIT)["items"]:
        raw = item.get("episode_still_url") if item["kind"] == "tv" else item.get("backdrop_url")
        if raw:
            images["接下来继续 landscape-card"].append(image_url(raw, "landscape-card"))
        elif item["kind"] != "tv" and item.get("poster_url"):
            variant = (
                "landscape-card" if (item.get("poster_aspect") or 0.66) >= 1 else "poster-card"
            )
            images["接下来继续 海报兜底"].append(image_url(item["poster_url"], variant))
    favorites = api.get(
        "/playback/favorites",
        limit=ROW_LIMIT,
        offset=0,
        unwatched_first="true",
        sort="favorited_at",
    )
    for item in favorites["items"]:
        images["收藏行 poster-card"].append(image_url(item.get("poster_url"), "poster-card"))
    for _label, path in requests[5:]:
        for item in api.http.get(path).json()["data"]:
            images["库行 / 合集行 poster-card"].append(
                image_url(item.get("poster_url"), "poster-card")
            )
    return {k: list(dict.fromkeys(u for u in v if u)) for k, v in images.items()}


def subscription_images(api: Api) -> dict[str, list[str]]:
    subs = api.get("/subscriptions")
    week = api.get("/subscriptions/today-arrivals", window="week")
    recent = api.get("/subscriptions/recent-arrivals")
    media = {sub["id"]: sub["media"] for sub in subs}
    now = datetime.now().astimezone()

    def imported(card: dict) -> datetime:
        return datetime.fromisoformat(card["imported_at"].replace("Z", "+00:00"))

    # Hero 候选（SubscriptionsHomeModel.heroSlides 的顺序）：在途 → 48 小时内刚到 → 今天 →
    # 更早到的 → 最近一个有预告的日子；一部作品一张、最多 5 张。App 还会预取下一张，
    # 这里多热 3 部，覆盖轮播与数据变化的余量
    pipeline = [a["subscription_id"] for a in week if a["status"] in ("grabbed", "downloaded")]
    fresh = [
        c["subscription_id"]
        for c in sorted(recent, key=imported, reverse=True)
        if (now - imported(c)).total_seconds() <= 48 * 3600
    ]
    older = [
        c["subscription_id"]
        for c in sorted(recent, key=imported, reverse=True)
        if (now - imported(c)).total_seconds() > 48 * 3600
    ]
    today = [a["subscription_id"] for a in week if a["days_ahead"] == 0 and a["status"] == "wanted"]
    ahead = [a["days_ahead"] for a in week if a["days_ahead"] > 0]
    upcoming = [a["subscription_id"] for a in week if ahead and a["days_ahead"] == min(ahead)]
    hero = list(dict.fromkeys(pipeline + fresh + today + older + upcoming))[:8]
    images: dict[str, list[str]] = defaultdict(list)
    for sid in hero:
        brief = media[sid]
        images["Hero 原图剧照 original"].append(
            image_url(original_tmdb(brief.get("backdrop_url")) or brief.get("poster_url"))
        )
        if brief.get("logo_url"):
            images["片名 Logo（透明 PNG）"].append(image_url(brief["logo_url"]))
    for card in recent:
        brief = card["media"]
        images["刚刚入库 landscape-card"].append(
            image_url(
                card.get("still_url") or brief.get("backdrop_url") or brief.get("poster_url"),
                "landscape-card",
            )
        )
        if brief.get("logo_url"):
            images["片名 Logo（透明 PNG）"].append(image_url(brief["logo_url"]))
    for arrival in week:
        brief = media.get(arrival["subscription_id"], {})
        images["日程 landscape-card"].append(
            image_url(brief.get("backdrop_url") or brief.get("poster_url"), "landscape-card")
        )
    for sub in subs:
        images["剧集 / 电影海报行 poster-card"].append(
            image_url(sub["media"].get("poster_url"), "poster-card")
        )
    return {k: list(dict.fromkeys(u for u in v if u)) for k, v in images.items()}


async def _fetch_all(
    api: Api, urls: list[str], concurrency: int = 6
) -> list[tuple[str, int, int, float, str]]:
    """并发取图（URLSession 对 HTTP/1.1 单主机默认 6 条连接）。

    返回 (url, 状态, 字节, 毫秒, 类型)。
    """
    gate = asyncio.Semaphore(concurrency)
    headers = {"Authorization": f"Bearer {api.token}"}
    results = []
    async with httpx.AsyncClient(
        base_url=api.origin + "/api/v1",
        headers=headers,
        timeout=120,
        limits=httpx.Limits(max_connections=concurrency),
    ) as client:

        async def one(url: str) -> None:
            async with gate:
                started = time.perf_counter()
                response = await client.get(url)
                results.append(
                    (
                        url,
                        response.status_code,
                        len(response.content),
                        (time.perf_counter() - started) * 1000,
                        response.headers.get("content-type", ""),
                    )
                )

        await asyncio.gather(*(one(url) for url in urls))
    return results


def cmd_warm(args: argparse.Namespace) -> None:
    for user in args.user:
        api = Api(args.origin, user)
        groups = {**library_home_images(api), **subscription_images(api)}
        total_bytes, total = 0, 0
        print(f"\n【预热 {user}】按 App 的图片地址逐类请求一遍（6 并发）")
        print(
            f"{'类别':<34}{'张数':>6}{'失败':>6}{'合计':>10}{'P50(ms)':>10}{'P95(ms)':>10}{'最慢(ms)':>10}"
        )
        for label, urls in groups.items():
            results = asyncio.run(_fetch_all(api, urls))
            times = sorted(r[3] for r in results)
            failed = [r for r in results if r[1] != 200]
            size = sum(r[2] for r in results)
            total_bytes, total = total_bytes + size, total + len(results)
            print(
                f"{label:<34}{len(results):>6}{len(failed):>6}{size / 1024 / 1024:>9.1f}M"
                f"{_pct(times, 50):>10.0f}{_pct(times, 95):>10.0f}"
                f"{times[-1] if times else 0:>10.0f}"
            )
            for url, status, *_ in failed[:3]:
                print(f"    失败 {status}: {url}")
        print(
            f"合计 {total} 张、{total_bytes / 1024 / 1024:.1f} MB"
            "（第一次请求 = 冷缓存回源 / 生成缩略图的耗时）"
        )
    cache = LAB / "data" / "cache" / "images"
    if cache.exists():
        files = [p for p in cache.rglob("*") if p.is_file() and p.suffix != ".json"]
        print(
            f"服务端图片缓存：{len(files)} 个文件、"
            f"{sum(p.stat().st_size for p in files) / 1024 / 1024:.1f} MB"
        )


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def cmd_stats(args: argparse.Namespace) -> None:
    db = sqlite3.connect(f"file:{LAB / 'data' / 'movieclaw.db'}?mode=ro", uri=True)

    def q(sql: str) -> list[tuple]:
        return db.execute(sql).fetchall()

    def one(sql: str) -> int:
        return q(sql)[0][0]

    in_place = one(
        "select count(distinct media_item_id) from library_file where state = 'in_place'"
    )
    print("【数据库】")
    print(
        f"  媒体库 {one('select count(*) from library')} 个；"
        f"条目 {one('select count(*) from media_item'):,}（有在位文件 {in_place:,}）；"
        f"台账文件 {one('select count(*) from library_file'):,}；"
        f"分集档案 {one('select count(*) from media_episode'):,}"
    )
    for name, kind, items, episodes, files in q(
        "select name, kind, stats_item_count, stats_episode_count, stats_file_count "
        "from library order by sort_order"
    ):
        print(f"    {name:<8}{kind:<6} 作品 {items:>5}  分集 {episodes:>5}  文件 {files:>5}")
    print(
        f"  合集：用户建 {q('select count(*) from collection where builtin is null')[0][0]}，"
        f"内置 {q('select count(*) from collection where builtin is not null')[0][0]}"
    )
    print(
        "  订阅："
        + "，".join(
            f"{k}/{s} {n}"
            for k, s, n in q("select kind, status, count(*) from subscription group by 1, 2")
        )
    )
    print(
        "  工单："
        + "，".join(
            f"{s} {n:,}" for s, n in q("select status, count(*) from wanted_item group by 1")
        )
    )
    print(
        "  观看状态："
        + "，".join(
            f"member_id={m}: {n} 行（收藏 {f}、续播点 {p}、已看 {w}）"
            for m, n, f, p, w in q(
                "select member_id, count(*), sum(is_favorite), sum(position_ms>0), sum(played) "
                "from playback_state group by 1"
            )
        )
    )
    for user in args.user:
        api = Api(args.origin, user)
        libs = api.get("/libraries", scope="all")
        cols = api.get("/collections")
        fav = api.get("/playback/favorites", limit=1, offset=0)
        up = api.get("/playback/up-next", limit=ROW_LIMIT)["items"]
        subs = api.get("/subscriptions")
        week = api.get("/subscriptions/today-arrivals", window="week")
        recent = api.get("/subscriptions/recent-arrivals")
        by_day = Counter(a["days_ahead"] for a in week)
        print(f"【接口视角：{user}】")
        print(
            f"  /libraries?scope=all {len(libs)} 个库"
            f"（作品合计 {sum(lib['stats']['item_count'] for lib in libs):,}、"
            f"文件合计 {sum(lib['stats']['file_count'] for lib in libs):,}）；"
            f"/collections {len(cols)} 个"
        )
        print(
            f"  收藏 {fav['total']} 部；接下来继续 {len(up)} 张"
            f"（剧集 {sum(1 for i in up if i['kind'] == 'tv')}、"
            f"带剧照 {sum(1 for i in up if i.get('episode_still_url'))}、"
            f"电影带背景 {sum(1 for i in up if i['kind'] == 'movie' and i.get('backdrop_url'))}）"
        )
        print(
            f"  订阅 {len(subs)} 条（"
            + "，".join(
                f"{k} {n}"
                for k, n in Counter(
                    f"{s['media']['kind']}/{s['status']}" for s in subs
                ).most_common()
            )
            + "）"
        )
        print(
            f"  一周预告 {len(week)} 条（按距今天数："
            + " ".join(f"+{d}:{n}" for d, n in sorted(by_day.items()))
            + f"）；刚刚入库 {len(recent)} 张"
        )
        if user == "admin":
            tasks = api.get("/downloaders/tasks")
            print(
                f"  下载任务 {len(tasks['items'])} 个；来源 "
                + "，".join(f"{s['name']}={s['status']}" for s in tasks["sources"])
            )
    size = subprocess.run(["du", "-sh", str(LAB)], capture_output=True, text=True).stdout.split()[0]
    print(f"【磁盘】{LAB} 共 {size}")


# ---------------------------------------------------------------------------
# bench
# ---------------------------------------------------------------------------


def _pct(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))]


_ACCESS = re.compile(
    r"\| movieclaw_api\.access \| method=(\w+) path=(\S+) status_code=(\d+) duration_ms=([\d.]+)"
)


def _log_path() -> Path:
    return LAB / "data" / "logs" / f"movieclaw-{date.today().isoformat()}.log"


def _server_times(offset: int, requests: list[str]) -> list[float | None]:
    """从访问日志里按顺序对回每个请求的服务端耗时（响应头发出时刻，毫秒）。

    访问日志不记查询串，按「路径相同、先后顺序一致」逐个配对；中途有别的客户端
    （比如开着的模拟器在轮询）插进来的行会被跳过，不会错配到别人头上。
    """
    try:
        with _log_path().open("r", encoding="utf-8") as fh:
            fh.seek(offset)
            lines = fh.read().splitlines()
    except OSError:
        return [None] * len(requests)
    entries = [(m.group(2), float(m.group(4))) for m in map(_ACCESS.search, lines) if m]
    result: list[float | None] = []
    cursor = 0
    for path in requests:
        want = "/api/v1" + path.split("?", 1)[0]
        found = None
        for index in range(cursor, len(entries)):
            if entries[index][0] == want:
                found, cursor = entries[index][1], index + 1
                break
        result.append(found)
    return result


@dataclass
class Case:
    label: str
    path: str
    first_ms: float = 0.0
    first_bytes: int = 0
    status: int = 0
    samples: list[float] = field(default_factory=list)
    server_first: float | None = None
    server_samples: list[float] = field(default_factory=list)
    bytes: int = 0


def cmd_bench(args: argparse.Namespace) -> None:
    """串行压测。第一轮就是一次完整的首页加载（按 App 的先后：偏好 → 库 → 合集 → 两行
    内置 → 各库 / 合集行 → 订阅页五个接口 [→ 图片]），行清单由这一轮的响应现算，
    所以首页结构相关的接口在第一轮之前一个都不会被碰过——配合 ios_lab.sh bench
    先重启后端，第一轮就是真正的冷启动首次请求。"""
    api = Api(args.origin, args.user)
    client = api.http
    log = _log_path()
    offset = log.stat().st_size if log.exists() else 0
    cases: list[Case] = []
    order: list[str] = []

    def run(label: str, path: str) -> httpx.Response:
        started = time.perf_counter()
        response = client.get(path)
        cases.append(
            Case(
                label,
                path,
                first_ms=(time.perf_counter() - started) * 1000,
                first_bytes=len(response.content),
                status=response.status_code,
            )
        )
        order.append(path)
        return response

    prefs = run("媒体库首页 · ui/preferences", "/ui/preferences").json()["data"]
    libraries = run("媒体库首页 · libraries?scope=all", "/libraries?scope=all").json()["data"]
    collections = run("媒体库首页 · collections", "/collections").json()["data"]
    run("媒体库首页 · playback/up-next", f"/playback/up-next?limit={ROW_LIMIT}")
    run(
        "媒体库首页 · playback/favorites",
        f"/playback/favorites?limit={ROW_LIMIT}&offset=0&unwatched_first=true&sort=favorited_at",
    )
    names = {lib["id"]: lib["name"] for lib in libraries}
    fetched: set[str] = set()
    for row in home_rows(prefs, libraries, collections):
        if row.path and row.path not in fetched:
            fetched.add(row.path)
            match = re.match(r"/(libraries|collections)/(\d+)/", row.path)
            what = (
                f"库「{names.get(int(match.group(2)), '?')}」"
                if match and match.group(1) == "libraries"
                else "合集行 "
            )
            run(f"媒体库首页 · {what}{row.ident} {row.path.split('?', 1)[1]}", row.path)
    for lib in libraries:
        path = f"/libraries/{lib['id']}/items?sort=added_at&limit={ROW_LIMIT}"
        if lib.get("viewer_access", True) and path not in fetched:
            fetched.add(path)
            run(f"媒体库首页 · 库卡片「{lib['name']}」sort=added_at", path)
    subs: list[dict] = []
    for label, path in subscriptions_requests(args.user == "admin"):
        response = run(f"订阅首页 · {label}", path)
        if path == "/subscriptions":
            subs = response.json()["data"]
    if args.images:
        # 每类图片挑一张代表；取代表用的条目请求不计时、不进访问日志配对
        top = client.get(
            f"/libraries/{libraries[0]['id']}/items", params={"sort": "rating", "limit": 1}
        )
        order.append(top.request.url.raw_path.decode().removeprefix("/api/v1"))
        cases.append(Case("（取代表条目，不计）", order[-1], status=top.status_code))
        with_logo = next(s for s in subs if s["media"].get("logo_url"))
        for label, path in [
            (
                "图片 · 本地海报资产 poster-card",
                image_url(top.json()["data"][0]["poster_url"], "poster-card"),
            ),
            (f"图片 · 库封面「{libraries[0]['name']}」", f"/libraries/{libraries[0]['id']}/cover"),
            (
                "图片 · 代理 TMDB 海报 w500 poster-card",
                image_url(subs[-1]["media"]["poster_url"], "poster-card"),
            ),
            (
                "图片 · 代理 Hero 原图 original",
                image_url(original_tmdb(with_logo["media"]["backdrop_url"])),
            ),
            ("图片 · 代理片名 Logo PNG", image_url(with_logo["media"]["logo_url"])),
        ]:
            run(label, path)

    for case in cases:  # 热态：每个请求连打 repeat 次
        for _ in range(args.repeat):
            started = time.perf_counter()
            response = client.get(case.path)
            case.samples.append((time.perf_counter() - started) * 1000)
            case.bytes = len(response.content)
            order.append(case.path)
    time.sleep(0.3)  # 让最后几行访问日志落盘
    server = _server_times(offset, order)
    for index, case in enumerate(cases):
        case.server_first = server[index]
    cursor = len(cases)
    for case in cases:
        case.server_samples = [s for s in server[cursor : cursor + args.repeat] if s is not None]
        cursor += args.repeat
    cases = [c for c in cases if not c.label.startswith("（")]

    print(
        f"\n【接口压测】{args.origin}（{args.user}；串行；首次 = 这一轮页面加载里的第一次请求，"
        f"热态 = 每个再打 {args.repeat} 次；服务端 = 访问日志里的耗时，即响应头发出时刻）"
    )
    header = (
        f"{'请求':<58}{'状态':>5}{'体积KB':>9}{'首次ms':>9}{'服务端':>8}"
        f"{'P50':>8}{'P95':>8}{'服务P50':>9}{'服务P95':>9}"
    )
    print(header)
    print("-" * 124)
    for case in cases:
        label = case.label if len(case.label) <= 56 else case.label[:55] + "…"
        pad = 58 - (len(label.encode("gbk", "replace")) - len(label))
        print(
            f"{label:<{pad}}{case.status:>5}{(case.bytes or case.first_bytes) / 1024:>9.1f}"
            f"{case.first_ms:>9.1f}{(case.server_first or 0):>8.1f}{_pct(case.samples, 50):>8.1f}"
            f"{_pct(case.samples, 95):>8.1f}{_pct(case.server_samples, 50):>9.1f}"
            f"{_pct(case.server_samples, 95):>9.1f}"
        )
    out = LAB / "bench" / f"bench-{args.user}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            [
                {
                    "label": c.label,
                    "path": c.path,
                    "status": c.status,
                    "bytes": c.bytes or c.first_bytes,
                    "first_ms": round(c.first_ms, 2),
                    "server_first_ms": c.server_first,
                    "p50_ms": round(_pct(c.samples, 50), 2),
                    "p95_ms": round(_pct(c.samples, 95), 2),
                    "server_p50_ms": round(_pct(c.server_samples, 50), 2),
                    "server_p95_ms": round(_pct(c.server_samples, 95), 2),
                }
                for c in cases
            ],
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\n结果已写入 {out}")


# ---------------------------------------------------------------------------
# verify：经链路模拟代理用 curl 验收
# ---------------------------------------------------------------------------


def _curl(url: str, token: str, out: Path) -> dict:
    fmt = "%{http_code} %{size_download} %{time_starttransfer} %{time_total} %{content_type}"
    result = subprocess.run(
        ["curl", "-s", "-o", str(out), "-w", fmt, "-H", f"Authorization: Bearer {token}", url],
        capture_output=True,
        text=True,
        check=False,
    )
    code, size, ttfb, total, ctype = (result.stdout.split(" ", 4) + [""] * 5)[:5]
    return {
        "status": int(code or 0),
        "bytes": int(size or 0),
        "ttfb": float(ttfb or 0) * 1000,
        "total": float(total or 0) * 1000,
        "type": ctype,
    }


def _summary(path: str, data) -> str:
    """把接口返回压成一句人话，证明数据是「对的」而不只是 200。"""
    if path.startswith("/ui/preferences"):
        return f"home.rows {len(data['home']['rows'])} 行"
    if path.startswith("/libraries?"):
        return f"{len(data)} 个库，作品合计 {sum(lib['stats']['item_count'] for lib in data):,}"
    if path == "/collections":
        return f"{len(data)} 个合集（{sum(1 for c in data if c.get('builtin'))} 个内置）"
    if path.startswith("/playback/up-next"):
        return (
            f"{len(data['items'])} 张（剧集 {sum(1 for i in data['items'] if i['kind'] == 'tv')}，"
            f"续播中 {sum(1 for i in data['items'] if i['position_ms'] > 0)}）"
        )
    if path.startswith("/playback/favorites"):
        return f"本页 {len(data['items'])} / 共 {data['total']}"
    if path == "/subscriptions":
        return (
            f"{len(data)} 条（"
            + "，".join(
                f"{k} {n}"
                for k, n in Counter(
                    f"{s['media']['kind']}/{s['status']}" for s in data
                ).most_common(4)
            )
            + " …）"
        )
    if path.startswith("/subscriptions/today-arrivals"):
        days = Counter(a["days_ahead"] for a in data)
        return f"{len(data)} 条，按天 " + " ".join(f"+{d}:{n}" for d, n in sorted(days.items()))
    if path.startswith("/subscriptions/recent-arrivals"):
        return f"{len(data)} 张（带剧照 {sum(1 for c in data if c.get('still_url'))}）"
    if path.startswith("/downloaders/tasks"):
        return f"{len(data['items'])} 个任务，来源 " + ",".join(
            s["status"] for s in data["sources"]
        )
    if path.startswith("/subscriptions/automation-readiness"):
        return f"status={data.get('status')} error_count={data.get('error_count')}"
    if isinstance(data, list):
        return f"{len(data)} 个条目，带海报 {sum(1 for i in data if i.get('poster_url'))}"
    return "ok"


def cmd_verify(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="mc-lab-verify-") as tmpdir:
        _verify(args, Path(tmpdir))


def _verify(args: argparse.Namespace, tmp: Path) -> None:
    from PIL import Image

    for via in args.via:
        origin = via if via.startswith("http") else f"http://127.0.0.1:{via}"
        api = Api(origin, args.user)
        print(f"\n【验收】{origin}（{args.user}，设备令牌 Bearer；curl 计时：首字节 / 总耗时）")
        lib_requests, _ctx = library_home_requests(api)
        requests = [("媒体库 · " + label, path) for label, path in lib_requests]
        requests += [
            ("订阅 · " + label, path)
            for label, path in subscriptions_requests(args.user == "admin")
        ]
        for label, path in requests:
            body = tmp / "body.json"
            r = _curl(f"{origin}/api/v1{path}", api.token or "", body)
            try:
                summary = _summary(path, json.loads(body.read_text("utf-8"))["data"])
            except (ValueError, KeyError, TypeError) as exc:
                summary = f"（解析失败 {exc}）"
            label = label if len(label) <= 44 else label[:43] + "…"
            print(
                f"  {r['status']} {r['bytes'] / 1024:>8.1f}KB "
                f"{r['ttfb']:>7.0f}/{r['total']:>6.0f}ms  {label:<46}{summary}"
            )
        samples = {**library_home_images(api), **subscription_images(api)}
        print("  —— 图片（每类取前 2 张）——")
        for label, urls in samples.items():
            for url in urls[:2]:
                out = tmp / "img.bin"
                r = _curl(f"{origin}/api/v1{url}", api.token or "", out)
                try:
                    with Image.open(out) as img:
                        alpha = " +alpha" if "A" in img.mode else ""
                        dims = f"{img.width}x{img.height} {img.format}{alpha}"
                except OSError:
                    dims = "（不是图片）"
                short = url if len(url) <= 70 else url[:69] + "…"
                print(
                    f"  {r['status']} {r['bytes'] / 1024:>8.1f}KB "
                    f"{r['ttfb']:>7.0f}/{r['total']:>6.0f}ms  "
                    f"{label:<26}{dims:<22}{short}"
                )


# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare-settings", help="首启前关掉 Jellyfin 兼容层")
    sub.add_parser("setup", help="经 API 初始化账号、出网代理、下载器、合集、首页行")
    for name in ("stats", "warm"):
        p = sub.add_parser(name)
        p.add_argument("--origin", default=DIRECT)
        p.add_argument(
            "--user", action="append", default=None, help="账号（可多次给），默认 admin + member"
        )
    p = sub.add_parser("bench", help="串行压测两个首页的接口")
    p.add_argument("--origin", default=DIRECT)
    p.add_argument("--user", default="admin")
    p.add_argument("--repeat", type=int, default=15)
    p.add_argument(
        "--images", action="store_true", help="同时测 5 类代表图片（冷 = 服务端图片缓存状态决定）"
    )
    p = sub.add_parser("verify", help="经链路模拟代理用 curl 验收")
    p.add_argument(
        "--via", action="append", default=None, help="代理端口或完整地址，默认 18601 与 18602"
    )
    p.add_argument("--user", default="admin")
    args = parser.parse_args()
    if getattr(args, "user", None) is None and args.cmd in ("stats", "warm"):
        args.user = ["admin", "member"]
    if args.cmd == "verify" and not args.via:
        args.via = ["18601", "18602"]
    {
        "prepare-settings": cmd_prepare_settings,
        "setup": cmd_setup,
        "stats": cmd_stats,
        "warm": cmd_warm,
        "bench": cmd_bench,
        "verify": cmd_verify,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
