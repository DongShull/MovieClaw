"""CLI API 令牌（PAT）与 Bearer 鉴权、spec 偏斜通道的端到端测试。

覆盖 docs/design/cli.md §8.1 / §6.2 / §2.1 的后端侧：
- 手工令牌创建（明文仅一次）/ 设备列表（无明文）/ 注销（立即失效）；
- require_login 双通道：Cookie 与 Bearer 等效，坏令牌 401；
- Agent 短时效签名令牌：有效可用、改密（轮换签名密钥）后全体失效；
- /spec 受鉴权 + ETag 304；/health 携带 spec_hash；响应头带偏斜指纹。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services import auth as auth_service
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box

_AUTH = "/api/v1/auth"
_ADMIN = {"username": "admin", "password": "s3cret-pass"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    # 订阅路由构造服务时要求 TMDB Key 存在；本测试不发真实 TMDB 请求
    monkeypatch.setenv("TMDB_API_KEY", "test-key-not-used")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()

    from movieclaw_api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        c.post(f"{_AUTH}/bootstrap", json=_ADMIN)
        yield c

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


def _bearer_client(client: TestClient, token: str) -> TestClient:
    """一个不带 Cookie、只带 Bearer 头的新客户端（模拟远程 CLI）。"""
    fresh = TestClient(client.app)
    fresh.headers["Authorization"] = f"Bearer {token}"
    return fresh


def test_pat_full_lifecycle(client: TestClient) -> None:
    created = client.post(f"{_AUTH}/tokens", json={"name": "nas-cron"})
    assert created.status_code == 200
    body = created.json()["data"]
    token = body["token"]
    assert token.startswith("mclaw_")

    # 「我的设备」列表只有元信息，绝不含明文/哈希
    listed = [
        d for d in client.get(f"{_AUTH}/devices").json()["data"] if d["kind"] == "manual"
    ]
    assert [t["name"] for t in listed] == ["nas-cron"]
    assert "token" not in listed[0] and "token_hash" not in listed[0]

    # Bearer 通道与 Cookie 等效
    cli = _bearer_client(client, token)
    assert cli.get("/api/v1/subscriptions").status_code == 200

    # 注销后立即失效，其余接口回到 401
    assert client.delete(f"{_AUTH}/devices/{body['id']}").status_code == 200
    assert cli.get("/api/v1/subscriptions").status_code == 401


def test_bad_bearer_token_rejected(client: TestClient) -> None:
    cli = _bearer_client(client, "mclaw_forged-token")
    assert cli.get("/api/v1/subscriptions").status_code == 401


def test_agent_token_lifecycle(tmp_path, monkeypatch) -> None:
    """Agent 短时效令牌：签发可验、身份归因带会话 id、轮换签名密钥即全体失效。

    直接测服务层（单事件循环，不经 TestClient）——改密接口内部调用的正是
    同一个 rotate_session_secret，机制等价。
    """
    import asyncio

    from movieclaw_api.exceptions import UnauthorizedException

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()

    async def flow() -> None:
        from pathlib import Path

        from movieclaw_api.settings import init_setting_store
        from movieclaw_db.crypto import init_secret_box
        from movieclaw_db.engine import dispose_db, init_db
        from movieclaw_db.migrations import run_migrations

        settings = get_settings()
        init_db(settings.database_url)
        await run_migrations()
        init_secret_box(settings.master_key, Path(settings.secret_key_file))
        init_setting_store()
        token = await auth_service.issue_agent_token("sess-1")
        principal = await auth_service.verify_bearer_token(token)
        # Principal 化后字符串形态保持旧格式（日志归因兼容），Agent 令牌等价管理员
        assert str(principal) == "agent:sess-1"
        assert principal.kind == "agent" and principal.is_admin

        await auth_service.rotate_session_secret()
        with pytest.raises(UnauthorizedException):
            await auth_service.verify_bearer_token(token)
        await dispose_db()

    try:
        asyncio.run(flow())
    finally:
        reset_setting_store()
        reset_secret_box()
        get_settings.cache_clear()


def test_spec_endpoint_requires_auth_and_supports_etag(client: TestClient) -> None:
    anonymous = TestClient(client.app)
    assert anonymous.get("/api/v1/spec").status_code == 401

    first = client.get("/api/v1/spec")
    assert first.status_code == 200
    assert "paths" in first.json()
    etag = first.headers["ETag"]

    cached = client.get("/api/v1/spec", headers={"If-None-Match": etag})
    assert cached.status_code == 304


def test_health_and_response_headers_carry_spec_hash(client: TestClient) -> None:
    health = client.get("/api/v1/health")
    spec_hash = health.json()["spec_hash"]
    assert spec_hash and health.headers["X-Movieclaw-Spec-Hash"] == spec_hash
    # 业务响应同样捎带指纹（CLI 零成本偏斜检测的来源）
    assert client.get("/api/v1/subscriptions").headers["X-Movieclaw-Spec-Hash"] == spec_hash
