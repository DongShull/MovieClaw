"""成员权限 v2 P0 的越权修复（docs/design/member-permissions-v2.md §2.1）。

每条用例对应设计稿里的一个编号，先确认「越权的那一方被拦」，再留一条对照组
确认「该能做的人仍然能做」——防止把门修成了全关：

- S1：订阅详情 / 活动记录只对发起人与关注者可见；
- S2：全家合集只有超管与创建者能改，内置合集只有超管能改；
- S3：成员改订阅时不能换规则组与目标库，洗版也不能借机换组；
- U5：手动选种要订阅 + 一键下载两项能力，且只能投给自己发起的订阅；
- U6：在途下载对成员开放，但种子名、下载器名、下载器报错一律置空；
- 站点：成员提交下载（一键下载 / 手动选种）同样受站点白名单约束，且下载链接
  必须属于该站点——否则服务端取种时会把站点凭据发往成员指定的任意地址。

身份切换走依赖覆盖（与 test_collections_api 同一手法）：鉴权本身在
test_member_auth 里压，这里只看各接口拿到成员主体后的授权判定。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import Principal
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import Library, Member, RuleSet
from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient

_KEY = "0123456789abcdef0123456789abcdef"

_ROUTES = {
    "/3/movie/100": {
        "id": 100,
        "title": "测试电影",
        "original_title": "Test Movie",
        "release_date": "2024-01-01",
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
}


def _fake_tmdb() -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _ROUTES.get(request.url.path)
        if payload is None:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=payload)

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


_ADMIN = Principal(kind="admin", name="admin")


def _member(member_id: int, **caps: bool) -> Principal:
    """成员主体；能力开关默认与新建成员一致（订阅开、搜索与一键下载关、全部站点）。"""
    member = Member(
        id=member_id,
        username=f"m{member_id}",
        password_hash="x",
        allow_subscribe=caps.get("subscribe", True),
        allow_search=caps.get("search", False),
        allow_direct_download=caps.get("direct_download", False),
        all_sites=caps.get("all_sites", True),
    )
    return Principal(
        kind="member", name=f"m{member_id}", member_id=member_id, is_admin=False, member=member
    )


class _As:
    """可切换的当前主体：``as_(principal)`` 之后的请求都以它的身份发出。"""

    def __init__(self) -> None:
        self.principal = _ADMIN

    def __call__(self, principal: Principal) -> None:
        self.principal = principal


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "perm.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("TMDB_API_KEY", "test-key-not-used")
    get_settings.cache_clear()

    async def _seed() -> dict:
        from movieclaw_api.services.media_library import MediaLibraryService
        from movieclaw_api.services.subscription import SubscriptionService

        init_db(get_settings().database_url, echo=False)
        await run_migrations()
        async with get_database().session() as session:
            library = Library(name="电影", kind="movie", root_paths=[str(tmp_path / "media")])
            other_rules = RuleSet(name="另一组", spec={})
            session.add_all(
                [
                    library,
                    other_rules,
                    Member(id=1, username="m1", password_hash="x"),
                    Member(id=2, username="m2", password_hash="x"),
                ]
            )
            await session.commit()
            service = SubscriptionService(session, MediaLibraryService(session, _fake_tmdb()))
            subscription = await service.create(MediaKind.MOVIE, 100, member_id=1)
            await session.commit()
            seeded = {
                "library": library.id,
                "other_rules": other_rules.id,
                "subscription": subscription.id,
                "rule_set": subscription.rule_set_id,
                "item": subscription.media_item_id,
            }
        await dispose_db()
        return seeded

    seeded = asyncio.run(_seed())

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app

    as_ = _As()
    app = create_app()
    app.dependency_overrides[require_login] = lambda: as_.principal
    with TestClient(app) as client:
        yield client, as_, seeded, db_path
    get_settings.cache_clear()


def _row(db_path: Path, sql: str, *args) -> tuple:
    with sqlite3.connect(db_path) as db:
        return db.execute(sql, args).fetchone()


# ---------------------------------------------------------------------------
# S1：订阅详情只对发起人与关注者可见
# ---------------------------------------------------------------------------


def test_subscription_detail_visible_only_to_creator_and_followers(env) -> None:
    client, as_, seeded, db_path = env
    sub = seeded["subscription"]

    as_(_member(2))
    assert client.get(f"/api/v1/subscriptions/{sub}").status_code == 404
    assert client.get(f"/api/v1/subscriptions/{sub}/activities").status_code == 404

    as_(_member(1))
    detail = client.get(f"/api/v1/subscriptions/{sub}")
    assert detail.status_code == 200
    assert detail.json()["data"]["can_manage"] is True
    assert client.get(f"/api/v1/subscriptions/{sub}/activities").status_code == 200

    # 关注之后就能看了（与「我的订阅」列表同一口径）
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO subscription_follower (created_at, updated_at, subscription_id, member_id)"
            " VALUES (CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, 2)",
            (sub,),
        )
    as_(_member(2))
    detail = client.get(f"/api/v1/subscriptions/{sub}")
    assert detail.status_code == 200
    # 关注者只能取消关注：前端据此隐藏调整类按钮，不让人点了再被拒
    assert detail.json()["data"]["can_manage"] is False

    as_(_ADMIN)
    assert client.get(f"/api/v1/subscriptions/{sub}").json()["data"]["can_manage"] is True


# ---------------------------------------------------------------------------
# S3：成员改订阅 / 洗版都不能换规则组与目标库
# ---------------------------------------------------------------------------


def test_member_patch_cannot_change_rule_set_or_library(env) -> None:
    client, as_, seeded, db_path = env
    sub = seeded["subscription"]

    as_(_member(1))
    resp = client.patch(
        f"/api/v1/subscriptions/{sub}",
        json={"rule_set_id": seeded["other_rules"], "library_id": seeded["library"]},
    )
    assert resp.status_code == 200, resp.text
    assert _row(db_path, "SELECT rule_set_id, library_id FROM subscription WHERE id=?", sub) == (
        seeded["rule_set"],
        None,
    )

    # 对照组：超管照常能换
    as_(_ADMIN)
    resp = client.patch(
        f"/api/v1/subscriptions/{sub}",
        json={"rule_set_id": seeded["other_rules"], "library_id": seeded["library"]},
    )
    assert resp.status_code == 200, resp.text
    assert _row(db_path, "SELECT rule_set_id, library_id FROM subscription WHERE id=?", sub) == (
        seeded["other_rules"],
        seeded["library"],
    )


def test_member_upgrade_run_keeps_current_rule_set(env, monkeypatch) -> None:
    import movieclaw_api.services.subscription as subscription_services

    client, as_, seeded, _db_path = env
    seen: list[int | None] = []

    async def fake_round(_session, _subscription_id, *, rule_set_id=None):  # noqa: ANN001
        seen.append(rule_set_id)
        return {
            "target_label": "1080p",
            "rule_set_id": rule_set_id or seeded["rule_set"],
            "summary": "ok",
            "counts": {},
            "units": [],
        }

    monkeypatch.setattr(subscription_services, "run_upgrade_round", fake_round)
    sub = seeded["subscription"]
    payload = {"rule_set_id": seeded["other_rules"]}

    as_(_member(1))
    assert client.post(f"/api/v1/subscriptions/{sub}/upgrade-runs", json=payload).status_code == 200
    as_(_ADMIN)
    assert client.post(f"/api/v1/subscriptions/{sub}/upgrade-runs", json=payload).status_code == 200
    assert seen == [None, seeded["other_rules"]]


# ---------------------------------------------------------------------------
# U5：手动选种
# ---------------------------------------------------------------------------


def test_member_grab_needs_both_capabilities_and_ownership(env, monkeypatch) -> None:
    from movieclaw_api.services.subscription import manual_grab

    client, as_, seeded, _db_path = env

    async def fake_grab(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return []

    monkeypatch.setattr(manual_grab, "grab_manual", fake_grab)
    sub = seeded["subscription"]
    payload = {
        "site_id": "demo",
        "torrent_id": "1",
        "title": "Test.Movie.2024.1080p",
        "download_url": "https://example.invalid/t/1",
    }
    url = f"/api/v1/subscriptions/{sub}/selected-torrent-downloads"

    as_(_member(1))  # 默认成员：没有一键下载
    assert client.post(url, json=payload).status_code == 403
    as_(_member(1, subscribe=False, direct_download=True))  # 没有订阅
    assert client.post(url, json=payload).status_code == 403
    as_(_member(2, direct_download=True))  # 能力齐了，但不是发起人
    assert client.post(url, json=payload).status_code == 403

    as_(_member(1, direct_download=True, all_sites=False))  # 站点白名单里没有 demo
    assert client.post(url, json=payload).status_code == 403

    as_(_member(1, direct_download=True))
    resp = client.post(url, json=payload)
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# U6：在途下载对成员开放、但只给进度
# ---------------------------------------------------------------------------


def test_active_downloads_open_to_members_without_admin_details(env, monkeypatch) -> None:
    from movieclaw_api.services import download_progress

    client, as_, seeded, _db_path = env

    async def fake_snapshot(_session, _subscription_id):  # noqa: ANN001
        return [
            {
                "info_hash": "abc",
                "name": "Test.Movie.2024.1080p.WEB-DL",
                "progress": 0.5,
                "size_bytes": 1024,
                "dlspeed_bytes": 100,
                "eta_seconds": 60,
                "state": "error",
                "error_message": "无法写入 /volume1/downloads",
                "downloader_name": "家里的 qBittorrent",
                "units": [],
            }
        ]

    monkeypatch.setattr(download_progress, "subscription_download_snapshot", fake_snapshot)
    url = f"/api/v1/subscriptions/{seeded['subscription']}/active-downloads"

    as_(_member(2))
    assert client.get(url).status_code == 404

    as_(_member(1))
    row = client.get(url).json()["data"][0]
    assert (row["progress"], row["eta_seconds"], row["state"]) == (0.5, 60, "error")
    assert row["name"] is None and row["downloader_name"] is None
    assert row["error_message"] is None

    as_(_ADMIN)
    row = client.get(url).json()["data"][0]
    assert row["downloader_name"] == "家里的 qBittorrent"


# ---------------------------------------------------------------------------
# S2：合集归属
# ---------------------------------------------------------------------------


def _create_collection(client: TestClient, seeded: dict, **payload) -> dict:
    body = {"library_id": seeded["library"], "item_ids": [seeded["item"]], **payload}
    resp = client.post("/api/v1/collections", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_household_collection_only_creator_or_admin_can_change(env) -> None:
    client, as_, seeded, _db_path = env
    as_(_member(1))
    created = _create_collection(client, seeded, name="一号的片单")
    cid = created["id"]
    assert created["manageable"] is True and created["editable"] is True

    # 二号看得到，但不能改、不能删、不能动名单
    as_(_member(2))
    view = client.get(f"/api/v1/collections/{cid}").json()["data"]
    assert view["manageable"] is False and view["editable"] is False
    assert client.put(f"/api/v1/collections/{cid}", json={"name": "被改了"}).status_code == 403
    assert (
        client.put(f"/api/v1/collections/{cid}", json={"visibility": "private"}).status_code
        == 403
    )
    assert client.put(f"/api/v1/collections/{cid}/order", json={"item_ids": []}).status_code == 403
    assert (
        client.delete(f"/api/v1/collections/{cid}/items/{seeded['item']}").status_code == 403
    )
    assert client.delete(f"/api/v1/collections/{cid}").status_code == 403

    # 超管能改名，但不能把成员建的共享合集收成自己的私有
    as_(_ADMIN)
    assert client.put(f"/api/v1/collections/{cid}", json={"name": "改个名"}).status_code == 200
    assert (
        client.put(f"/api/v1/collections/{cid}", json={"visibility": "private"}).status_code
        == 403
    )

    # 创建者自己可以删
    as_(_member(1))
    assert client.delete(f"/api/v1/collections/{cid}").status_code == 200


def test_builtin_collection_is_admin_only(env) -> None:
    client, as_, _seeded, _db_path = env
    as_(_ADMIN)
    builtin = next(
        row
        for row in client.get("/api/v1/collections?include_empty=true").json()["data"]
        if row["builtin"]
    )

    url = f"/api/v1/collections/{builtin['id']}"

    as_(_member(1))
    assert client.put(url, json={"hidden": True}).status_code == 403
    assert client.delete(url).status_code == 403

    as_(_ADMIN)
    assert client.put(url, json={"hidden": True}).status_code == 200


def test_private_to_household_records_the_owner_as_creator(env) -> None:
    """迁移前的私有合集没记创建者：转为共享时以归属人为创建者，转完仍能自己管。"""
    client, as_, seeded, db_path = env
    as_(_member(1))
    created = _create_collection(client, seeded, name="私藏", visibility="private")
    cid = created["id"]
    with sqlite3.connect(db_path) as db:  # 模拟迁移前的老数据
        db.execute("UPDATE collection SET created_by_member_id=0 WHERE id=?", (cid,))

    resp = client.put(f"/api/v1/collections/{cid}", json={"visibility": "household"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["manageable"] is True
    assert _row(db_path, "SELECT created_by_member_id FROM collection WHERE id=?", cid) == (1,)


def test_deleting_member_hands_household_collections_to_admin(env) -> None:
    client, as_, seeded, db_path = env
    as_(_member(1))
    cid = _create_collection(client, seeded, name="一号的片单")["id"]

    as_(_ADMIN)
    assert client.delete("/api/v1/members/1").status_code == 200
    # 合集留下、转归超管：SQLite 会复用成员 id，不转的话下一个新成员会凭空管得了它
    assert _row(db_path, "SELECT created_by_member_id FROM collection WHERE id=?", cid) == (0,)


# ---------------------------------------------------------------------------
# 站点：下载链接必须属于该站点
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("download_url", "allowed"),
    [
        ("12345", True),  # M-Team 式种子 ID：只会拼到站点自己的 API 上
        ("download.php?id=1", True),  # 相对路径
        ("https://pt.example.com/download.php?id=1", True),
        ("https://www.example.com/download.php?id=1", True),  # 网页域名
        ("https://cdn.pt.example.com/t/1", True),  # 站点子域名
        ("https://download.example.com/t/1", True),  # 同主域名的另一个子域名
        ("https://evil.test/collect", False),
        ("//evil.test/collect", False),  # 协议相对地址同样会被拼成外部主机
        ("https://pt.example.com.evil.test/x", False),  # 后缀伪装
        ("https://notexample.com/x", False),
    ],
)
def test_download_url_must_belong_to_the_site(monkeypatch, download_url, allowed) -> None:
    from movieclaw_api.exceptions import ForbiddenException
    from movieclaw_api.services import site_access
    from movieclaw_api.services.site_visibility import assert_download_url_on_site

    class _Site:
        base_url = "https://pt.example.com"
        web_base_url = "https://www.example.com"

    class _Access:
        async def get(self, _site_id):  # noqa: ANN001
            return _Site()

    monkeypatch.setattr(site_access, "get_site_access", lambda: _Access())

    async def check(principal: Principal) -> bool:
        try:
            await assert_download_url_on_site(principal, "demo", download_url)
        except ForbiddenException:
            return False
        return True

    assert asyncio.run(check(_member(1))) is allowed
    # 超管行为不变：不校验
    assert asyncio.run(check(_ADMIN)) is True
