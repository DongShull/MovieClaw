"""HTTP 安全边界的守护测试：安全响应头 + 生产环境关闭接口文档。

两条都是「配错了不会报错、只会静悄悄敞开」的那类设置，所以必须由测试钉住：

1. 安全响应头（middleware.py::SecurityHeadersMiddleware）——后端所有响应都要
   带 nosniff / DENY / no-referrer。少了 nosniff，图片代理直出的第三方字节
   就可能被浏览器嗅探成 HTML 当同源文档执行；少了 frame 相关的两条，
   后台页面能被第三方站点 iframe 套住做点击劫持。
2. ``APP_ENV`` 默认值——它决定 /docs、/redoc 与 openapi.json 是否对外开放。
   镜像与 entrypoint 都不设这个变量，一旦默认值退回 local，等于所有容器部署
   把完整接口面白送给匿名访问者。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box


def _build_client(tmp_path, monkeypatch, *, app_env: str | None = None) -> TestClient:
    """按指定 APP_ENV 装配一个隔离的应用实例。app_env=None 表示不设该变量，
    走代码里的默认值——这正是 Docker 部署的真实形态。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    if app_env is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", app_env)
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()

    from movieclaw_api.app import create_app

    return TestClient(create_app())


@pytest.fixture
def client(tmp_path, monkeypatch):
    with _build_client(tmp_path, monkeypatch) as c:
        yield c
    reset_setting_store()
    reset_secret_box()


_EXPECTED_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}


def test_api_responses_carry_security_headers(client: TestClient) -> None:
    """公开接口的响应必须带齐安全头。"""
    resp = client.get("/api/v1/health")

    assert resp.status_code == 200
    for name, value in _EXPECTED_HEADERS.items():
        assert resp.headers.get(name) == value, f"缺少或错误的安全头：{name}"
    assert "frame-ancestors 'none'" in resp.headers.get("content-security-policy", "")


def test_security_headers_also_cover_error_and_jellyfin_responses(client: TestClient) -> None:
    """401 与 Jellyfin 命名空间同样要覆盖到。

    前者走异常处理器产出响应，后者不经 /api/v1 前缀——两条都是容易漏掉的路径，
    而 Jellyfin 命名空间恰恰直出图片和视频，正是 nosniff 最需要生效的地方。
    """
    for path in ("/api/v1/members", "/System/Info"):
        resp = client.get(path)
        assert resp.status_code == 401
        assert resp.headers.get("x-content-type-options") == "nosniff", path

    public = client.get("/System/Info/Public")
    assert public.status_code == 200
    assert public.headers.get("x-content-type-options") == "nosniff"


def test_app_env_defaults_to_production(monkeypatch) -> None:
    """代码默认值必须是 production。

    刻意绕开 ``.env``（``_env_file=None``）只验代码里写死的那个默认值：
    镜像里没有 .env，容器拿到的就是它。开发机的 .env 写 APP_ENV=local
    不该让这条守护失效。
    """
    from movieclaw_api.core.config import Settings

    monkeypatch.delenv("APP_ENV", raising=False)

    assert Settings(_env_file=None).app_env == "production"


def test_openapi_closed_in_production_and_open_for_local_dev(tmp_path, monkeypatch) -> None:
    """生产环境不挂接口文档路由，显式 local 才开放。"""
    with _build_client(tmp_path, monkeypatch, app_env="production") as prod_client:
        for path in ("/api/v1/openapi.json", "/docs", "/redoc"):
            assert prod_client.get(path).status_code == 404, f"{path} 在生产环境下不应可达"

    reset_setting_store()
    reset_secret_box()

    with _build_client(tmp_path, monkeypatch, app_env="local") as dev_client:
        assert dev_client.get("/api/v1/openapi.json").status_code == 200
        assert dev_client.get("/docs").status_code == 200

    reset_setting_store()
    reset_secret_box()
