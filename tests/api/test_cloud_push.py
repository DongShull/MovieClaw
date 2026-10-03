"""MovieClaw Cloud 与 App 推送的端到端测试（docs/design/cloud-push.md）。

用进程内的假云端和假中继（httpx.MockTransport）按协议应答，从管理员点「连接」开始，
走完：配对 → 续签上报 → App 登记 → 事件推送 → 中继收到密文 → 用 App 的密钥解开。
再覆盖异常：拒绝、过期、解绑、版本不受支持、断开时云端连不上、令牌失效、通道切换。
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.push import crypto
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box

_AUTH = "/api/v1/auth"
_ADMIN = {"username": "admin", "password": "s3cret-pass"}
_MEMBER = {"username": "family", "password": "family-pass-1"}
_CLOUD = "https://cloud.test"
_PUSH = "https://push.test"
_OFFICIAL_TOPIC = "io.movieclaw.app"


# ----------------------------------------------------------------------
# 假云端 + 假中继
# ----------------------------------------------------------------------


class FakeCloud:
    """按云端协议应答：发现文档、配对、续签、解绑。"""

    def __init__(self) -> None:
        self.approval = "pending"  # pending / approved / denied
        self.renew_reply: tuple[int, dict] | None = None  # 覆盖续签的应答
        self.unbind_down = False
        self.revoked = False
        self.secret = "mcs_" + secrets.token_urlsafe(16)
        self.reports: list[dict] = []
        self.push_endpoints = [_PUSH]
        self.requests: list[str] = []
        self.issued = 0

    def grant(self) -> dict:
        self.issued += 1
        return {
            "access_token": f"jwt-{self.issued}",
            "token_type": "Bearer",
            "expires_in": 86400,
            "scope": "push",
            "scopes": ["push"],
            "limits": {"day": 5000, "device_day": 500},
            "capabilities": ["push"],
            "renew_interval": 3600,
            "account": {"display": "a•••@example.com"},
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(f"{request.method} {path}")
        if path == "/.well-known/movieclaw-cloud":
            return httpx.Response(
                200,
                json={
                    "api": _CLOUD,
                    "push_endpoints": [
                        {"url": u, "priority": i + 1} for i, u in enumerate(self.push_endpoints)
                    ],
                    "min_instance_version": "",
                },
            )
        if path == "/v1/instance/device-code":
            form = parse_qs(request.content.decode())
            assert form["scope"] == ["push"]
            assert form["instance_version"][0]
            return httpx.Response(
                200,
                json={
                    "device_code": "dc-" + secrets.token_hex(8),
                    "user_code": "WDJB-MJHT",
                    "verification_uri": "https://movieclaw.test/activate",
                    "verification_uri_complete": "https://movieclaw.test/activate?code=WDJB-MJHT",
                    "expires_in": 600,
                    "interval": 1,
                },
            )
        if path == "/v1/instance/token":
            if self.approval == "pending":
                return httpx.Response(400, json={"error": "authorization_pending"})
            if self.approval == "denied":
                return httpx.Response(400, json={"error": "access_denied"})
            return httpx.Response(
                200,
                json={**self.grant(), "instance_id": "inst-1", "instance_secret": self.secret},
            )
        if path == "/v1/instance/renew":
            assert request.headers["authorization"] == f"Bearer {self.secret}"
            body = json.loads(request.content)
            self.reports.append(body["report"])
            if self.renew_reply is not None:
                status, payload = self.renew_reply
                return httpx.Response(status, json=payload)
            if self.revoked:
                return httpx.Response(
                    401,
                    json={
                        "success": False,
                        "code": "INSTANCE_REVOKED",
                        "message": "这台实例已经解绑",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "code": "OK",
                    "message": "success",
                    "data": {**self.grant(), "instance_id": "inst-1", "notices": []},
                },
            )
        if path == "/v1/instance/unbind":
            if self.unbind_down:
                return httpx.Response(503, json={})
            self.revoked = True
            return httpx.Response(200, json={"success": True, "data": {"revoked": True}})
        return httpx.Response(404, json={})


class FakeRelay:
    """按推送中继协议应答：/v1/info 与 /v1/push。"""

    def __init__(self, *, topics: list[str], mode: str, token: str | None = None) -> None:
        self.topics = topics
        self.mode = mode
        self.token = token
        self.down = False
        self.results: dict[str, str] = {}  # 设备令牌 → 结果码
        self.messages: list[dict] = []
        self.bearers: list[str | None] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("连不上", request=request)
        if request.url.path == "/v1/info":
            return httpx.Response(
                200,
                json={
                    "protocol": 1,
                    "software": "movieclaw-push/test",
                    "aud": "https://push.test",
                    "platforms": ["apns"],
                    "environments": ["production", "development"],
                    "topics": self.topics,
                    "types": {"alert": {}, "background": {}},
                    "auth": {"mode": self.mode},
                    "limits": {"day": 5000, "device_day": 500},
                    "max_batch": 100,
                },
            )
        if request.url.path == "/v1/push":
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ") or None
            if self.mode == "static" and bearer != self.token:
                return httpx.Response(401, json={"error": "unauthorized", "message": "令牌无效"})
            if self.mode == "issuer" and not (bearer or "").startswith("jwt-"):
                return httpx.Response(
                    401, json={"error": "unauthorized", "message": "凭证无效或已过期"}
                )
            messages = json.loads(request.content)["messages"]
            if not messages:
                return httpx.Response(
                    400, json={"error": "bad_request", "message": "messages 不能为空"}
                )
            self.bearers.append(bearer)
            self.messages.extend(messages)
            results = []
            for m in messages:
                code = self.results.get(m["token"], "ok")
                result = {"id": m["id"], "result": code}
                if code == "rate_limited":
                    result.update(retry_after=3600, message="今天的推送已达上限")
                results.append(result)
            return httpx.Response(
                200,
                json={
                    "results": results,
                    "quota": {
                        "day": {
                            "limit": 5000,
                            "used": len(self.messages),
                            "remaining": 4990,
                            "reset_at": 1767312000,
                        }
                    },
                },
            )
        return httpx.Response(404, json={})


class World:
    def __init__(self) -> None:
        self.cloud = FakeCloud()
        self.relays: dict[str, FakeRelay] = {
            "push.test": FakeRelay(topics=[_OFFICIAL_TOPIC], mode="issuer"),
            "push2.test": FakeRelay(topics=[_OFFICIAL_TOPIC], mode="issuer"),
            "relay.lan": FakeRelay(
                topics=["com.yi.movieclaw"], mode="static", token="mcpush_ab12_secret"
            ),
        }
        self.hosts: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.hosts.append(request.url.host)
            if request.url.host == "cloud.test":
                return self.cloud.handle(request)
            relay = self.relays.get(request.url.host)
            if relay is None:
                raise httpx.ConnectError("没有这个地址", request=request)
            return relay.handle(request)

        return httpx.MockTransport(handler)


@pytest.fixture
def world(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    w = World()
    transport = w.transport()
    monkeypatch.setattr(
        "movieclaw_api.services.cloud.client.egress_transport", lambda *a, **k: transport
    )
    monkeypatch.setattr(
        "movieclaw_api.services.push.relay.egress_transport", lambda *a, **k: transport
    )
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'push.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("MOVIECLAW_CLOUD_URL", _CLOUD)
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    from movieclaw_api.services.push import channels, me

    channels.reset_runtime()
    me.reset_state()
    yield w
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


@pytest.fixture
def client(world):  # type: ignore[no-untyped-def]
    from movieclaw_api.app import create_app

    with TestClient(create_app()) as c:
        c.post(f"{_AUTH}/bootstrap", json=_ADMIN)
        c.post(f"{_AUTH}/login", json=_ADMIN)
        yield c


def _data(resp: httpx.Response) -> dict:
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _in_app(client: TestClient, trigger: Callable[[], None]) -> None:
    """在应用自己的事件循环里调业务产生点（推送是在那个循环里起的后台任务）。"""

    async def run() -> None:
        trigger()

    assert client.portal is not None
    client.portal.call(run)


def _wait(predicate: Callable[[], bool], timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("等待超时")


def _connect(client: TestClient, world: World) -> dict:
    status = _data(client.post("/api/v1/cloud/pairing", json={"instance_name": "客厅 NAS"}))
    assert status["state"] == "pairing"
    world.cloud.approval = "approved"
    _wait(lambda: _data(client.get("/api/v1/cloud"))["state"] == "connected")
    _wait(lambda: len(world.cloud.reports) >= 1)
    return _data(client.get("/api/v1/cloud"))


def _as_app(client: TestClient, bearer: str, method: str, url: str, **kwargs) -> httpx.Response:  # type: ignore[no-untyped-def]
    """以 App 的身份请求：只带设备令牌，不带浏览器里的会话 Cookie（真实 App 没有它）。"""
    saved = dict(client.cookies)
    client.cookies.clear()
    try:
        return client.request(method, url, headers={"Authorization": f"Bearer {bearer}"}, **kwargs)
    finally:
        for name, value in saved.items():
            client.cookies.set(name, value)


def _app_login(client: TestClient, who: dict, *, installation: str, name: str) -> str:
    saved = dict(client.cookies)
    client.cookies.clear()
    try:
        resp = client.post(
            f"{_AUTH}/device/login",
            json={**who, "client": {"kind": "ios", "installation_id": installation, "name": name}},
        )
    finally:
        for cookie, value in saved.items():
            client.cookies.set(cookie, value)
    return _data(resp)["token"]


def _register(
    client: TestClient, bearer: str, *, token: str, topic: str = _OFFICIAL_TOPIC
) -> tuple[str, bytes]:
    key = secrets.token_bytes(32)
    key_id = crypto.b64url(secrets.token_bytes(8))
    resp = _as_app(
        client,
        bearer,
        "PUT",
        "/api/v1/push/me/registration",
        json={
            "token": token,
            "topic": topic,
            "environment": "production",
            "types": ["alert"],
            "key_id": key_id,
            "key": crypto.b64url(key),
            "permission": "authorized",
        },
    )
    assert resp.status_code == 200, resp.text
    return key_id, key


def _open(message: dict, key: bytes) -> dict:
    return json.loads(crypto.open_sealed(message["payload"], key=key))


def _create_member(client: TestClient) -> None:
    resp = client.post("/api/v1/members", json={**_MEMBER, "nickname": "家人"})
    assert resp.status_code in (200, 201), resp.text


# ----------------------------------------------------------------------
# 连接
# ----------------------------------------------------------------------


def test_not_connected_sends_nothing(client: TestClient, world: World) -> None:
    status = _data(client.get("/api/v1/cloud"))
    assert status["state"] == "disconnected"
    assert status["connection"] is None and status["pairing"] is None
    assert status["custom_cloud_url"] is True
    channels = _data(client.get("/api/v1/push/channels"))
    official = channels["channels"][0]
    assert official["id"] == "official" and official["state"] == "inactive"
    assert official["status_text"] == "未连接 MovieClaw Cloud"
    assert world.hosts == []  # 未连接时对云端、官方中继不发任何请求


def test_connect_renew_and_report(client: TestClient, world: World) -> None:
    status = _data(client.post("/api/v1/cloud/pairing", json={"instance_name": "客厅 NAS"}))
    pairing = status["pairing"]
    assert pairing["user_code"] == "WDJB-MJHT" and pairing["status"] == "pending"
    assert pairing["qrcode_image"].startswith("data:image/svg+xml;base64,")
    assert pairing["instance_name"] == "客厅 NAS"
    assert "dc-" not in json.dumps(status)  # device_code 不出现在任何响应里

    status = _connect(client, world)
    connection = status["connection"]
    assert connection["account_display"] == "a•••@example.com"
    assert connection["scopes"] == ["push"] and connection["instance_name"] == "客厅 NAS"
    assert status["health"] == "ok"
    report = world.cloud.reports[0]
    assert report["instance_version"] and report["os"] and report["arch"]
    assert status["last_report"]["instance_version"] == report["instance_version"]

    # 关掉统计后只上报版本信息
    _data(client.put("/api/v1/cloud/settings", json={"report_stats": False}))
    _data(client.post("/api/v1/cloud/renew"))
    assert set(world.cloud.reports[-1]) == {"instance_version", "runtime_version", "os", "arch"}

    channels = _data(client.get("/api/v1/push/channels"))
    assert channels["cloud_state"] == "connected"
    assert channels["channels"][0]["state"] == "ok"
    # 已连接时重复连接被拒
    assert client.post("/api/v1/cloud/pairing", json={}).status_code == 409


def test_pairing_denied(client: TestClient, world: World) -> None:
    _data(client.post("/api/v1/cloud/pairing", json={}))
    world.cloud.approval = "denied"
    _wait(lambda: (_data(client.get("/api/v1/cloud"))["pairing"] or {}).get("status") == "denied")
    status = _data(client.get("/api/v1/cloud"))
    assert status["state"] == "pairing" and "拒绝" in status["pairing"]["message"]
    status = _data(client.delete("/api/v1/cloud/pairing"))
    assert status["state"] == "disconnected"


def test_revoked_on_website(client: TestClient, world: World) -> None:
    _connect(client, world)
    world.cloud.revoked = True
    _data(client.post("/api/v1/cloud/renew"))
    status = _data(client.get("/api/v1/cloud"))
    assert status["state"] == "disconnected"
    assert status["last_disconnect"]["reason"] == "revoked"
    notices = _data(client.get("/api/v1/system/notices"))
    items = notices["items"] if isinstance(notices, dict) else notices
    assert any("断开" in n["title"] and n["source"] == "cloud" for n in items)


def test_version_unsupported_keeps_credentials(client: TestClient, world: World) -> None:
    _connect(client, world)
    world.cloud.renew_reply = (
        403,
        {"success": False, "code": "VERSION_UNSUPPORTED", "message": "实例版本 0.1 已不再受支持"},
    )
    status = _data(client.post("/api/v1/cloud/renew"))
    assert status["state"] == "connected" and status["health"] == "unsupported"
    official = _data(client.get("/api/v1/push/channels"))["channels"][0]
    assert official["state"] == "error" and "不再受支持" in official["status_text"]
    # 升级后（云端恢复受理）下一次续签自动恢复
    world.cloud.renew_reply = None
    status = _data(client.post("/api/v1/cloud/renew"))
    assert status["health"] == "ok"


def test_renew_failure_keeps_token(client: TestClient, world: World) -> None:
    _connect(client, world)
    world.cloud.renew_reply = (503, {})
    status = _data(client.post("/api/v1/cloud/renew"))
    assert status["state"] == "connected" and status["health"] == "unreachable"
    assert _data(client.get("/api/v1/push/channels"))["channels"][0]["state"] == "warning"


def test_disconnect_when_cloud_unreachable(client: TestClient, world: World) -> None:
    _connect(client, world)
    world.cloud.unbind_down = True
    resp = client.post("/api/v1/cloud/disconnect", json={})
    assert resp.status_code == 409 and resp.json()["code"] == "CLOUD_UNREACHABLE"
    status = _data(client.post("/api/v1/cloud/disconnect", json={"force": True}))
    assert status["state"] == "disconnected" and status["last_disconnect"] is None


# ----------------------------------------------------------------------
# 登记与推送
# ----------------------------------------------------------------------


def test_registration_requires_app_credentials(client: TestClient, world: World) -> None:
    # 网页会话登记不了
    resp = client.put(
        "/api/v1/push/me/registration", json={"permission": "authorized", "token": None}
    )
    assert resp.status_code == 403
    bearer = _app_login(client, _ADMIN, installation="inst-iphone-1", name="iPhone")
    bad = _as_app(
        client,
        bearer,
        "PUT",
        "/api/v1/push/me/registration",
        json={"permission": "authorized", "token": "a1" * 32},
    )
    assert bad.status_code == 400  # token、key_id、key 要么都有要么都没有
    # 只上报权限状态
    resp = _as_app(
        client, bearer, "PUT", "/api/v1/push/me/registration", json={"permission": "denied"}
    )
    assert _data(resp)["status"] == "permission_denied"


def test_push_end_to_end_through_official_relay(client: TestClient, world: World) -> None:
    _connect(client, world)
    bearer = _app_login(client, _ADMIN, installation="inst-iphone-1", name="iPhone 16 Pro")
    device_token = "ab" * 32
    key_id, key = _register(client, bearer, token=device_token)

    mine = _data(client.get("/api/v1/push/me"))
    assert mine["instance_ready"] is True
    assert mine["ready_devices"] == 1 and mine["attention"] == []
    # 设备页上同一台设备的推送状态
    listed = _data(client.get("/api/v1/auth/devices"))
    phone = next(d for d in listed if d["name"] == "iPhone 16 Pro")
    assert phone["push"] == {"status": "ok", "status_text": "能收到"}
    assert all(d["push"] is None for d in listed if d["kind"] == "web")

    # 测试通知：同步返回结果，中继收到的是密文，用 App 的密钥解得开
    result = _data(client.post("/api/v1/push/me/test"))
    assert result["sent"] == 1 and result["results"][0]["result"] == "ok"
    relay = world.relays["push.test"]
    message = relay.messages[-1]
    assert relay.bearers[-1] == "jwt-2"  # 用的是续签拿到的最新令牌
    assert message["topic"] == _OFFICIAL_TOPIC and message["environment"] == "production"
    assert message["type"] == "alert" and message["aps"] == {"sound": "default"}
    assert message["payload"].startswith(f"v1.{key_id}.")
    assert "测试" not in json.dumps(message, ensure_ascii=False)  # 明文里没有任何内容
    plain = _open(message, key)
    assert plain["title"] == "测试通知" and plain["type"] == "alert"
    assert plain["server"]["name"] == "客厅 NAS" and plain["account"]["id"] == "0"
    assert plain["open"] == "/settings/notifications"

    # 待处理事项 → 推给管理员，同一个问题用同一个 collapse_id
    from movieclaw_api.services.push import events

    _in_app(
        client,
        lambda: events.system_alert(
            dedupe_key="site:mteam",
            source="site",
            title="站点登录失效",
            message="请更新 Cookie",
            payload={},
        ),
    )
    _wait(lambda: len(relay.messages) >= 2)
    alert = relay.messages[-1]
    plain = _open(alert, key)
    assert plain["title"] == "站点登录失效" and plain["open"] == "/settings/sites"
    assert plain["source"] == "server"  # 管理员告警：连了多台服务器时标服务器名
    assert alert["collapse_id"] and len(alert["collapse_id"]) == 16

    # 覆盖视图：官方 App 的设备走官方通道
    # 通道上只汇总设备数，每台设备都有通道时没有「缺口」
    view = _data(client.get("/api/v1/push/channels"))
    assert view["channels"][0]["device_count"] == 1 and view["uncovered"] == []
    # 上报里带上了按平台汇总的设备数（没有设备名）
    _data(client.post("/api/v1/cloud/renew"))
    assert world.cloud.reports[-1]["devices"] == [
        {"platform": "ios", "app_version": "", "count": 1}
    ]
    assert world.cloud.reports[-1]["relay"]["reachable"] is True


def test_same_phone_two_accounts_gets_one_push(client: TestClient, world: World) -> None:
    _connect(client, world)
    _create_member(client)
    token = "cd" * 32
    admin_bearer = _app_login(client, _ADMIN, installation="shared-ipad-1", name="客厅 iPad")
    member_bearer = _app_login(client, _MEMBER, installation="shared-ipad-1", name="客厅 iPad")
    _, admin_key = _register(client, admin_bearer, token=token)
    _register(client, member_bearer, token=token)

    from movieclaw_api.services.push import notify

    async def build(_session, member_id):  # type: ignore[no-untyped-def]
        return notify.AlertContent(title=f"给 {member_id}")

    relay = world.relays["push.test"]
    _in_app(client, lambda: notify.notify("imported", {0, 1}, build))
    _wait(lambda: len(relay.messages) >= 1)
    time.sleep(0.3)
    assert len(relay.messages) == 1
    assert _open(relay.messages[0], admin_key)["title"] == "给 0"


def test_preferences_and_member_view(client: TestClient, world: World) -> None:
    _connect(client, world)
    _create_member(client)
    member_bearer = _app_login(client, _MEMBER, installation="member-phone-1", name="家人的手机")
    _register(client, member_bearer, token="ef" * 32)
    view = _data(_as_app(client, member_bearer, "GET", "/api/v1/push/me"))
    keys = {e["key"] for e in view["events"]}
    assert "system_alert" not in keys and "imported" in keys and view["is_admin"] is False
    view = _data(
        _as_app(
            client,
            member_bearer,
            "PUT",
            "/api/v1/push/me/preferences",
            json={"events": {"imported": False, "system_alert": True}},
        )
    )
    assert next(e for e in view["events"] if e["key"] == "imported")["enabled"] is False

    from movieclaw_api.services.push import notify

    async def build(_session, _member_id):  # type: ignore[no-untyped-def]
        return notify.AlertContent(title="入库")

    relay = world.relays["push.test"]
    _in_app(client, lambda: notify.notify("imported", {1}, build))  # 关掉了：不发
    _in_app(client, lambda: notify.notify("download_started", {1}, build))  # 默认关：不发
    _in_app(client, lambda: notify.notify("new_device", {1}, build))  # 默认开：发
    _wait(lambda: len(relay.messages) >= 1)
    time.sleep(0.3)
    assert len(relay.messages) == 1


def test_unregistered_token_clears_registration(client: TestClient, world: World) -> None:
    _connect(client, world)
    bearer = _app_login(client, _ADMIN, installation="inst-old-1", name="旧手机")
    token = "aa" * 32
    _register(client, bearer, token=token)
    world.relays["push.test"].results[token] = "unregistered"
    result = _data(client.post("/api/v1/push/me/test"))
    assert result["results"][0]["result"] == "unregistered"
    mine = _data(client.get("/api/v1/push/me"))
    assert mine["ready_devices"] == 0 and mine["attention"] == []  # 等 App 下次启动重新登记


def test_official_failover_to_backup_endpoint(client: TestClient, world: World) -> None:
    world.cloud.push_endpoints = [_PUSH, "https://push2.test"]
    _connect(client, world)
    bearer = _app_login(client, _ADMIN, installation="inst-iphone-2", name="iPhone")
    _register(client, bearer, token="bb" * 32)
    world.relays["push.test"].down = True
    result = _data(client.post("/api/v1/push/me/test"))
    assert result["results"][0]["result"] == "ok"
    assert len(world.relays["push2.test"].messages) == 1


def test_new_device_login_notifies_other_devices(client: TestClient, world: World) -> None:
    _connect(client, world)
    bearer = _app_login(client, _ADMIN, installation="inst-iphone-3", name="我的 iPhone")
    _, key = _register(client, bearer, token="cc" * 32)
    relay = world.relays["push.test"]
    _app_login(client, _ADMIN, installation="inst-ipad-9", name="新 iPad")
    _wait(lambda: len(relay.messages) >= 1)
    plain = _open(relay.messages[-1], key)
    assert plain["title"] == "新设备登录了你的账号" and "新 iPad" in plain["body"]
    assert "「客厅 NAS」" in plain["body"] and plain["source"] == "account"
    # 同一台设备重新登录不算新设备
    count = len(relay.messages)
    _app_login(client, _ADMIN, installation="inst-ipad-9", name="新 iPad")
    time.sleep(0.5)
    assert len(relay.messages) == count


# ----------------------------------------------------------------------
# 自建中继
# ----------------------------------------------------------------------


def test_custom_relay_lifecycle(client: TestClient, world: World) -> None:
    probe = _data(client.post("/api/v1/push/relays/probe", json={"url": "http://relay.lan/"}))
    assert probe["reachable"] and probe["auth_mode"] == "static" and probe["error"] is None
    assert probe["url"] == "http://relay.lan" and probe["topics"] == ["com.yi.movieclaw"]
    assert any("没有设备" in w for w in probe["warnings"])

    # 令牌不对、缺令牌都加不进去
    assert client.post("/api/v1/push/relays", json={"url": "http://relay.lan"}).status_code == 400
    wrong = client.post(
        "/api/v1/push/relays", json={"url": "http://relay.lan", "token": "mcpush_xx_wrong"}
    )
    assert wrong.status_code == 400 and "不认这个令牌" in wrong.json()["message"]
    view = _data(
        client.post(
            "/api/v1/push/relays",
            json={"name": "书房中继", "url": "http://relay.lan", "token": "mcpush_ab12_secret"},
        )
    )
    custom = view["channels"][1]
    assert custom["name"] == "书房中继" and custom["state"] == "ok"
    assert custom["token_hint"] == "mcpush_ab12…" and "secret" not in json.dumps(view)

    # 自己打包的 App 走自建中继（没连云也能用）
    bearer = _app_login(client, _ADMIN, installation="inst-self-1", name="自签 iPhone")
    _, key = _register(client, bearer, token="dd" * 32, topic="com.yi.movieclaw")
    result = _data(client.post("/api/v1/push/me/test"))
    assert result["results"][0]["result"] == "ok"
    relay = world.relays["relay.lan"]
    assert relay.bearers[-1] == "mcpush_ab12_secret"
    assert _open(relay.messages[-1], key)["title"] == "测试通知"

    view = _data(client.get("/api/v1/push/channels"))
    assert view["channels"][1]["device_count"] == 1 and view["uncovered"] == []

    # 停用后没有可用通道
    relay_id = custom["id"]
    view = _data(client.patch(f"/api/v1/push/relays/{relay_id}", json={"enabled": False}))
    assert view["channels"][1]["state"] == "inactive"
    assert view["uncovered"] == [{"topic": "com.yi.movieclaw", "device_count": 1}]
    attention = _data(client.get("/api/v1/push/me"))["attention"]
    assert [a["status"] for a in attention] == ["no_channel"]
    _data(client.delete(f"/api/v1/push/relays/{relay_id}"))
    assert len(_data(client.get("/api/v1/push/channels"))["channels"]) == 1


def test_official_channel_switch(client: TestClient, world: World) -> None:
    _connect(client, world)
    bearer = _app_login(client, _ADMIN, installation="inst-iphone-4", name="iPhone")
    _register(client, bearer, token="ee" * 32)
    view = _data(client.put("/api/v1/push/channels/official", json={"enabled": False}))
    assert (
        view["channels"][0]["state"] == "inactive"
        and view["channels"][0]["status_text"] == "已停用"
    )
    result = _data(client.post("/api/v1/push/me/test"))
    assert result["results"] == [] or result["results"][0]["result"] == "no_channel"
    assert _data(client.get("/api/v1/push/me"))["instance_ready"] is False


# ----------------------------------------------------------------------
# 配图签名
# ----------------------------------------------------------------------


def test_push_image_signature(client: TestClient, world: World) -> None:
    import asyncio

    from movieclaw_api.services.push import images

    tmdb = get_settings().tmdb_image_base_url.rstrip("/") + "/w780/abc.jpg"
    path = asyncio.run(images.image_path(tmdb))
    assert path and path.startswith("/api/v1/push/images/")
    assert asyncio.run(images.resolve_image(path.rsplit("/", 1)[-1])) == tmdb
    assert asyncio.run(images.image_path("https://evil.example/x.jpg")) is None
    assert client.get("/api/v1/push/images/forged-token").status_code == 404


# ----------------------------------------------------------------------
# 订阅入库：推给订阅的人，看不到的不推
# ----------------------------------------------------------------------


def test_imported_goes_to_subscribers_who_can_see_it(client: TestClient, world: World) -> None:
    from movieclaw_api.services.subscription.wanted_fulfillment import close_fulfilled_wanted
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        FileSource,
        LibraryFile,
        MediaItem,
        RuleSet,
        Subscription,
        SubscriptionFollower,
        WantedItem,
        WantedStatus,
        utcnow,
    )
    from movieclaw_db.repositories.library_repo import LibraryRepository

    _connect(client, world)
    _create_member(client)
    admin_bearer = _app_login(client, _ADMIN, installation="admin-phone-1", name="管理员的手机")
    member_bearer = _app_login(client, _MEMBER, installation="member-phone-2", name="家人的手机")
    _, admin_key = _register(client, admin_bearer, token="a0" * 32)
    _, member_key = _register(client, member_bearer, token="b0" * 32)
    relay = world.relays["push.test"]
    relay.messages.clear()

    async def seed_and_import(access_mode: str) -> tuple[int, int, int]:
        async with get_database().session() as session:
            library = await LibraryRepository(session).create(
                name=f"剧集库-{access_mode}", kind="tv", root_paths=[f"/media/tv-{access_mode}"]
            )
            library.access_mode = access_mode
            item = MediaItem(
                kind="tv",
                tmdb_id=200 if access_mode == "everyone" else 201,
                title="漫长的季节",
                original_title="The Long Season",
                year=2023,
            )
            rule_set = RuleSet(name=f"默认-{access_mode}", spec={})
            session.add_all([library, item, rule_set])
            await session.commit()
            await session.refresh(item)
            await session.refresh(rule_set)
            subscription = Subscription(
                media_item_id=item.id, kind="tv", rule_set_id=rule_set.id, library_id=library.id
            )  # 管理员发起
            session.add(subscription)
            await session.commit()
            await session.refresh(subscription)
            session.add(SubscriptionFollower(subscription_id=subscription.id, member_id=1))
            session.add(
                WantedItem(
                    subscription_id=subscription.id,
                    media_item_id=item.id,
                    season_number=1,
                    episode_number=7,
                    status=WantedStatus.GRABBED,
                    info_hash=f"hash-{access_mode}",
                    grabbed_at=utcnow(),
                )
            )
            session.add(
                LibraryFile(
                    library_id=library.id,
                    media_item_id=item.id,
                    season_number=1,
                    episode_number=7,
                    file_path=f"/media/tv-{access_mode}/漫长的季节/S01E07.mkv",
                    size_bytes=1,
                    source=FileSource.IMPORTED,
                )
            )
            await session.commit()
            assert await close_fulfilled_wanted(session, item.id) == 1
            return library.id, item.id, subscription.id

    # 对全员开放的库：发起人（管理员）和关注者（成员）都收到，各自用自己的密钥
    assert client.portal is not None
    library_id, item_id, _ = client.portal.call(seed_and_import, "everyone")
    _wait(lambda: len(relay.messages) >= 2)
    by_token = {m["token"]: m for m in relay.messages}
    member_plain = _open(by_token["b0" * 32], member_key)
    assert member_plain["title"] == "漫长的季节 更新了"
    assert member_plain["body"] == "第 1 季第 7 集已入库，点开就能看"
    assert member_plain["open"] == f"/library/{library_id}/item/{item_id}?season=1&episode=7"
    assert member_plain["account"] == {"id": "1", "name": "家人"}
    assert "source" not in member_plain  # 内容类不标来源：点开时 App 自动切过去
    assert _open(by_token["a0" * 32], admin_key)["title"] == "漫长的季节 更新了"

    # 只对选中成员开放、家人不在名单里：家人看不到，就不推给家人
    relay.messages.clear()
    client.portal.call(seed_and_import, "selected")
    _wait(lambda: len(relay.messages) >= 1)
    time.sleep(0.5)
    assert [m["token"] for m in relay.messages] == ["a0" * 32]


def test_episode_label() -> None:
    from movieclaw_api.services.push.events import episode_label

    assert episode_label([(0, 0)]) == ""
    assert episode_label([(2, 7)]) == "第 2 季第 7 集"
    assert episode_label([(0, 3)]) == "特别篇第 3 集"
    assert episode_label([(1, 1), (1, 2), (1, 3)]) == "第 1 季 3 集"
    assert episode_label([(1, 8), (2, 1)]) == "2 集"


# ----------------------------------------------------------------------
# 媒体库有新片
# ----------------------------------------------------------------------


def test_library_new_arrivals(client: TestClient, world: World) -> None:
    from datetime import timedelta

    from movieclaw_api.services.push import arrivals
    from movieclaw_api.settings import get_setting_store
    from movieclaw_api.settings.cloud import ArrivalsProgress
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        FileSource,
        LibraryFile,
        MediaItem,
        RuleSet,
        Subscription,
        utcnow,
    )
    from movieclaw_db.repositories.library_repo import LibraryRepository

    _connect(client, world)
    _create_member(client)
    admin_bearer = _app_login(client, _ADMIN, installation="arr-admin-1", name="管理员手机")
    member_bearer = _app_login(client, _MEMBER, installation="arr-member-1", name="家人手机")
    _register(client, admin_bearer, token="c1" * 32)
    _, member_key = _register(client, member_bearer, token="d1" * 32)
    relay = world.relays["push.test"]
    assert client.portal is not None

    async def seed_libraries() -> tuple[int, int, int]:
        async with get_database().session() as session:
            repo = LibraryRepository(session)
            movies = await repo.create(name="电影", kind="movie", root_paths=["/m"])
            shows = await repo.create(name="剧集", kind="tv", root_paths=["/t"])
            fresh = await repo.create(name="刚建的库", kind="movie", root_paths=["/f"])
            old = utcnow() - timedelta(days=3)
            movies.created_at = shows.created_at = old  # 老库；fresh 是刚建的
            session.add_all([movies, shows])
            await session.commit()
            return movies.id, shows.id, fresh.id

    movies_id, shows_id, fresh_id = client.portal.call(seed_libraries)
    # 家人打开「媒体库有新片」，只关心电影库和刚建的库
    view = _data(
        _as_app(
            client,
            member_bearer,
            "PUT",
            "/api/v1/push/me/preferences",
            json={"events": {"library_new": True}, "library_ids": [movies_id, fresh_id]},
        )
    )
    assert view["library_ids"] == [movies_id, fresh_id]
    assert {lib["name"] for lib in view["libraries"]} >= {"电影", "剧集", "刚建的库"}

    async def start_progress() -> None:
        await get_setting_store().set(
            ArrivalsProgress(started_at=utcnow() - timedelta(seconds=1), marks={})
        )

    client.portal.call(start_progress)

    async def add_files(
        specs: list[tuple[int, str, str, tuple[int, int], FileSource]],
    ) -> list[int]:
        ids = []
        async with get_database().session() as session:
            for library_id, kind, title, (season, episode), source in specs:
                item = (
                    await session.execute(
                        __import__("sqlmodel").select(MediaItem).where(MediaItem.title == title)
                    )
                ).scalar_one_or_none()
                if item is None:
                    item = MediaItem(
                        kind=kind,
                        tmdb_id=1000 + abs(hash(title)) % 100000,
                        title=title,
                        original_title=title,
                        year=2024,
                    )
                    session.add(item)
                    await session.commit()
                    await session.refresh(item)
                session.add(
                    LibraryFile(
                        library_id=library_id,
                        media_item_id=item.id,
                        season_number=season,
                        episode_number=episode,
                        file_path=f"/x/{library_id}/{title}/{season}-{episode}-{len(ids)}-{utcnow().timestamp()}.mkv",
                        size_bytes=1,
                        source=source,
                    )
                )
                ids.append(item.id)
            await session.commit()
        return ids

    def run_check() -> int:
        async def go() -> int:
            return await arrivals.check_once(utcnow() + timedelta(minutes=10))

        return client.portal.call(go)

    # ① 电影库来了一部新片 → 家人收到单条；管理员没打开这项，收不到
    (movie_id,) = client.portal.call(
        add_files, [(movies_id, "movie", "流浪地球 2", (0, 0), FileSource.IMPORTED)]
    )
    assert run_check() == 1
    _wait(lambda: len(relay.messages) >= 1)
    time.sleep(0.3)
    assert [m["token"] for m in relay.messages] == ["d1" * 32]
    plain = _open(relay.messages[-1], member_key)
    assert plain["title"] == "新片：流浪地球 2" and "已加入「电影」" in plain["body"]
    assert plain["open"].startswith(f"/library/{movies_id}/item/{movie_id}")

    # ② 同一部又来一个更好的版本（洗版）→ 不算新片
    relay.messages.clear()
    client.portal.call(add_files, [(movies_id, "movie", "流浪地球 2", (0, 0), FileSource.IMPORTED)])
    assert run_check() == 0

    # ③ 一批三部 → 合成一条
    client.portal.call(
        add_files,
        [
            (movies_id, "movie", "奥本海默", (0, 0), FileSource.SCANNED),
            (movies_id, "movie", "沙丘 2", (0, 0), FileSource.SCANNED),
            (movies_id, "movie", "首尔之春", (0, 0), FileSource.SCANNED),
        ],
    )
    assert run_check() == 1
    _wait(lambda: len(relay.messages) >= 1)
    plain = _open(relay.messages[-1], member_key)
    assert plain["title"] == "「电影」新增 3 部" and "奥本海默" in plain["body"]
    assert plain["open"] == f"/library/{movies_id}"

    # ④ 家人自己订阅了的片 → 已有「入库完成」，这里不重复
    relay.messages.clear()

    async def subscribe_as_member(title: str) -> None:
        async with get_database().session() as session:
            item = MediaItem(
                kind="movie", tmdb_id=900001, title=title, original_title=title, year=2024
            )
            rule_set = RuleSet(name=f"规则-{title}", spec={})
            session.add_all([item, rule_set])
            await session.commit()
            await session.refresh(item)
            await session.refresh(rule_set)
            session.add(
                Subscription(
                    media_item_id=item.id,
                    kind="movie",
                    rule_set_id=rule_set.id,
                    created_by_member_id=1,
                )
            )
            await session.commit()

    client.portal.call(subscribe_as_member, "我订阅的片")
    client.portal.call(add_files, [(movies_id, "movie", "我订阅的片", (0, 0), FileSource.IMPORTED)])
    run_check()
    time.sleep(0.5)
    assert relay.messages == []

    # ⑤ 没勾选的剧集库、刚建的库的首次扫描 → 都不推
    client.portal.call(
        add_files,
        [
            (shows_id, "tv", "漫长的季节", (1, 7), FileSource.IMPORTED),
            (fresh_id, "movie", "首次扫描出来的", (0, 0), FileSource.SCANNED),
        ],
    )
    run_check()
    time.sleep(0.5)
    assert relay.messages == []

    # ⑥ 还在陆续入库（5 分钟内有新行）就先不发
    client.portal.call(add_files, [(movies_id, "movie", "刚到的", (0, 0), FileSource.IMPORTED)])

    async def check_now() -> int:
        return await arrivals.check_once(utcnow())

    assert client.portal.call(check_now) == 0


def test_new_version_pushed_once(client: TestClient, world: World) -> None:
    from movieclaw_api.schemas.app_update import UpdateCheckView
    from movieclaw_api.services import app_update

    _connect(client, world)
    _create_member(client)
    admin_bearer = _app_login(client, _ADMIN, installation="ver-admin-1", name="管理员手机")
    member_bearer = _app_login(client, _MEMBER, installation="ver-member-1", name="家人手机")
    _, admin_key = _register(client, admin_bearer, token="e1" * 32)
    _register(client, member_bearer, token="f1" * 32)
    relay = world.relays["push.test"]
    relay.messages.clear()

    def check(version: str) -> None:
        view = UpdateCheckView(
            current_version="0.30.0",
            latest_version=version,
            update_available=True,
            compatible=True,
            requires_runtime=17,
            changelog="",
            published_at="",
            latest_known_bad=False,
        )

        async def go() -> None:
            await app_update._record_app_check(view)

        assert client.portal is not None
        client.portal.call(go)

    check("0.31.0")
    _wait(lambda: len(relay.messages) >= 1)
    time.sleep(0.3)
    assert [m["token"] for m in relay.messages] == ["e1" * 32]  # 只推管理员
    plain = _open(relay.messages[-1], admin_key)
    assert plain["title"] == "MovieClaw 0.31.0 可以更新了" and plain["open"] == "/settings/app"

    check("0.31.0")  # 每小时的检查不会反复推同一个版本
    time.sleep(0.5)
    assert len(relay.messages) == 1
    check("0.31.1")
    _wait(lambda: len(relay.messages) >= 2)

    # 管理员关掉这一项就不推
    _data(client.put("/api/v1/push/me/preferences", json={"events": {"new_version": False}}))
    check("0.32.0")
    time.sleep(0.5)
    assert len(relay.messages) == 2
    # 成员看不到这个开关
    keys = {
        e["key"] for e in _data(_as_app(client, member_bearer, "GET", "/api/v1/push/me"))["events"]
    }
    assert "new_version" not in keys


def test_manual_download_imported_once(client: TestClient, world: World) -> None:
    """手动下载入库推「入库完成」给点下载的人；他开着「媒体库有新片」也不重复。"""
    from datetime import timedelta

    from movieclaw_api.services.push import arrivals
    from movieclaw_api.services.push import events as push_events
    from movieclaw_api.settings import get_setting_store
    from movieclaw_api.settings.cloud import ArrivalsProgress
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import FileSource, LibraryFile, MediaItem, utcnow
    from movieclaw_db.repositories.library_repo import LibraryRepository

    _connect(client, world)
    _create_member(client)
    member_bearer = _app_login(client, _MEMBER, installation="manual-m-1", name="家人手机")
    _, key = _register(client, member_bearer, token="a2" * 32)
    _data(
        _as_app(
            client,
            member_bearer,
            "PUT",
            "/api/v1/push/me/preferences",
            json={"events": {"library_new": True}},
        )
    )
    relay = world.relays["push.test"]
    assert client.portal is not None

    async def seed() -> tuple[int, int]:
        await get_setting_store().set(
            ArrivalsProgress(started_at=utcnow() - timedelta(seconds=1), marks={})
        )
        async with get_database().session() as session:
            library = await LibraryRepository(session).create(
                name="电影", kind="movie", root_paths=["/m"]
            )
            library.created_at = utcnow() - timedelta(days=3)
            item = MediaItem(
                kind="movie", tmdb_id=777001, title="我下载的片", original_title="Mine", year=2024
            )
            session.add_all([library, item])
            await session.commit()
            await session.refresh(item)
            session.add(
                LibraryFile(
                    library_id=library.id,
                    media_item_id=item.id,
                    season_number=0,
                    episode_number=0,
                    file_path="/m/我下载的片 (2024)/我下载的片 (2024).mkv",
                    size_bytes=1,
                    source=FileSource.IMPORTED,
                )
            )
            await session.commit()
            return library.id, item.id

    library_id, item_id = client.portal.call(seed)
    _in_app(
        client,
        lambda: push_events.manual_imported(
            member_ids={1},
            item_id=item_id,
            library_id=library_id,
            title="我下载的片",
            year=2024,
            kind="movie",
            since=utcnow() - timedelta(minutes=5),
            image_url=None,
        ),
    )
    _wait(lambda: len(relay.messages) >= 1)
    plain = _open(relay.messages[-1], key)
    assert plain["title"] == "我下载的片 已入库"
    assert plain["open"].startswith(f"/library/{library_id}/item/{item_id}")

    async def check() -> int:
        return await arrivals.check_once(utcnow() + timedelta(minutes=10))

    client.portal.call(check)
    time.sleep(0.5)
    assert len(relay.messages) == 1  # 「媒体库有新片」不再推这部
