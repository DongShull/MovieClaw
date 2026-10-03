"""下载器用途（订阅 / 刷流分开指定）接口测试。

覆盖：默认两条链路都跟随默认下载器、设置后回显解析出的名称、拒绝不存在的
下载器、可清回跟随默认、指定下载器被删除后名称回显为 null（前端据此提示
该配置已失效）。

真实的 create_downloader 被替换为假下载器，不发真实请求。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import movieclaw_api.services.downloader_config as downloader_service
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_downloader import DownloaderInfo
from movieclaw_downloader.models import DownloaderConfig


class _FakeDownloader:
    """假适配器：连接测试一律成功，避免测试依赖真实网络。

    只实现本文件会走到的方法；``close`` 必须留着 —— 保存下载器后的异步连接
    测试收尾时会调它（``services.downloader_config.verify_downloader``）。
    """

    def __init__(self, config: DownloaderConfig) -> None:
        self.config = config

    async def test_connection(self) -> DownloaderInfo:
        return DownloaderInfo(type=self.config.type, version="v5.0.2")

    async def close(self) -> None:
        return None


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 每个测试用独立临时 SQLite 库与密钥文件，保证隔离
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    # 下载器用途落 app_setting（SettingStore 带按 namespace 的内存缓存），
    # 必须显式重置：否则上一个用例设的值会漏进下一个用例（见 test_scrape_settings
    # 的同位做法）。
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()

    monkeypatch.setattr(downloader_service, "create_downloader", _FakeDownloader)
    import movieclaw_api.services.download_tasks as download_tasks_service

    monkeypatch.setattr(download_tasks_service, "create_downloader", _FakeDownloader)

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    # 本文件只测下载器用途配置业务，登录鉴权用依赖覆盖绕过（鉴权本身在 test_auth 覆盖）
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    with TestClient(app) as c:  # with 块内触发 lifespan：建库、迁移、初始化加密器
        yield c

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


def _create_downloader(c, name: str, client_type: str, port: int) -> int:
    r = c.post(
        "/api/v1/downloaders",
        json={
            "name": name,
            "client_type": client_type,
            "url": f"http://192.168.1.10:{port}",
            "username": "admin",
            "password": "s3cret",
            "save_path": "/downloads",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"]


def test_usage_defaults_to_following_default(client) -> None:
    """从未配置时两条链路都跟随默认下载器（字段为 null）。"""
    r = client.get("/api/v1/downloaders/usage")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["subscription_downloader_id"] is None
    assert data["subscription_downloader_name"] is None
    assert data["boost_downloader_id"] is None
    assert data["boost_downloader_name"] is None


def test_set_usage_and_read_back_with_names(client) -> None:
    """分别指定订阅与刷流的下载器，回显应带上解析出的名称。"""
    qb_id = _create_downloader(client, "qb-mc", "qbittorrent", 8082)
    tr_id = _create_downloader(client, "tr-mc", "transmission", 9092)

    r = client.put(
        "/api/v1/downloaders/usage",
        json={"subscription_downloader_id": qb_id, "boost_downloader_id": tr_id},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["subscription_downloader_id"] == qb_id
    assert data["subscription_downloader_name"] == "qb-mc"
    assert data["boost_downloader_id"] == tr_id
    assert data["boost_downloader_name"] == "tr-mc"

    # 落库后重新读取应一致（走 SettingStore 的缓存与持久化链路）
    again = client.get("/api/v1/downloaders/usage")
    assert again.status_code == 200
    assert again.json()["data"] == data


def test_usage_rejects_unknown_downloader(client) -> None:
    """指定一台不存在的下载器应被拒绝，且不落库。"""
    r = client.put(
        "/api/v1/downloaders/usage",
        json={"subscription_downloader_id": 9999},
    )
    assert r.status_code == 400, r.text
    assert "9999" in r.json()["message"]

    # 校验失败不应留下半截配置
    data = client.get("/api/v1/downloaders/usage").json()["data"]
    assert data["subscription_downloader_id"] is None


def test_usage_can_be_cleared(client) -> None:
    """传 null 应回到「跟随默认下载器」。"""
    qb_id = _create_downloader(client, "qb-mc", "qbittorrent", 8082)
    client.put("/api/v1/downloaders/usage", json={"boost_downloader_id": qb_id})
    assert client.get("/api/v1/downloaders/usage").json()["data"]["boost_downloader_id"] == qb_id

    r = client.put(
        "/api/v1/downloaders/usage",
        json={"subscription_downloader_id": None, "boost_downloader_id": None},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["boost_downloader_id"] is None
    assert data["boost_downloader_name"] is None


def test_usage_name_is_none_when_downloader_deleted(client) -> None:
    """指定的下载器被删除后，id 仍回显、名称变 null（前端据此提示配置失效）。"""
    qb_id = _create_downloader(client, "qb-mc", "qbittorrent", 8082)
    client.put("/api/v1/downloaders/usage", json={"boost_downloader_id": qb_id})

    assert client.delete(f"/api/v1/downloaders/{qb_id}").status_code == 200

    data = client.get("/api/v1/downloaders/usage").json()["data"]
    assert data["boost_downloader_id"] == qb_id
    assert data["boost_downloader_name"] is None
