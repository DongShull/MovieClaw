"""外部库源钩子（services/library/source_providers.py）的端到端测试。

覆盖：插件登记一个假来源后——建库向导的来源列表（/libraries/source-providers）、
外部来源建库（空根 + source_config 校验）、库视图的来源展示字段、能力位
按插件档案生效、连接配置的编辑与非法输入拒绝、内置来源不接受 source_config。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library import source_providers as sp
from movieclaw_api.services.library.profile import (
    IgnoreProfile,
    LibraryProfile,
    MediaKind,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    with TestClient(
        app, headers={"Accept-Language": "zh-Hans"}
    ) as c:  # with 块内触发 lifespan：迁移
        yield c
    get_settings.cache_clear()


def _profile(source: str, kind: MediaKind) -> LibraryProfile:
    """一个形态的能力档案：不刮削、可订阅，Jellyfin 类型随形态。"""
    return LibraryProfile(
        kind=kind,
        source=source,
        scraped=False,
        naming=False,
        ignore_rules=IgnoreProfile.PLAIN,
        write_nfo=False,
        subscribable=True,
        jellyfin_type="Movie" if kind is MediaKind.MOVIE else "Series",
        jellyfin_collection="movies" if kind is MediaKind.MOVIE else "tvshows",
        default_aspect=2 / 3,
        media_exts=frozenset({".mp4"}),
        playable=True,
        jellyfin_exposed=True,
    )


def _fake_provider(source: str = "testsrc") -> sp.LibrarySourceProvider:
    """一个最小但完整的外部库源：电影/剧集各一份档案、连接配置两个必填栏。"""
    return sp.LibrarySourceProvider(
        source=source,
        title="测试媒体服务器",
        profiles={
            MediaKind.MOVIE.value: _profile(source, MediaKind.MOVIE),
            MediaKind.TV.value: _profile(source, MediaKind.TV),
        },
        config_schema={
            "type": "object",
            "required": ["base_url"],
            "properties": {
                "base_url": {"type": "string"},
                "library_id": {"type": "string"},
            },
        },
        icon="testsrc",
        help="测试用外部库源",
    )


@pytest.fixture
def provider(client):
    """应用起来之后（内核注册表已绑定空表）再挂假来源，模拟插件加载。"""
    registry = sp.Registry(sp.LIBRARY_SOURCE_PROVIDERS)
    registry.add("test:testsrc", _fake_provider(), entry_id="entry-test")
    unbind = sp.bind_source_providers(registry)
    yield _fake_provider()
    unbind()


# ---------------------------------------------------------------------------
# 来源列表（建库向导数据源）
# ---------------------------------------------------------------------------


def test_source_providers_endpoint_lists_registered(client, provider) -> None:
    r = client.get("/api/v1/libraries/source-providers")
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data) == 1
    row = data[0]
    assert row["source"] == "testsrc"
    assert row["title"] == "测试媒体服务器"
    assert row["kind_support"] == ["movie", "tv"]
    assert row["capabilities"]["movie"]["subscribable"] is True
    assert row["capabilities"]["movie"]["naming"] is False
    assert row["capabilities"]["tv"]["jellyfin_collection"] == "tvshows"
    assert row["config_schema"]["required"] == ["base_url"]


def test_source_providers_empty_without_plugin(client) -> None:
    # 没有插件登记时列表为空：主线不内置任何外部来源
    assert client.get("/api/v1/libraries/source-providers").json()["data"] == []


# ---------------------------------------------------------------------------
# 外部来源建库
# ---------------------------------------------------------------------------


def test_create_external_library_without_roots(client, provider) -> None:
    """外部库源允许空根（内容在远程服务器上），连接配置按 schema 校验后落库。"""
    r = client.post(
        "/api/v1/libraries",
        json={
            "name": "飞牛电影",
            "kind": "movie",
            "source": "testsrc",
            "root_paths": [],
            "source_config": {"base_url": "http://fnos:5666", "library_id": "lib-1"},
        },
    )
    assert r.status_code == 200
    view = r.json()["data"]
    assert view["source"] == "testsrc"
    assert view["source_title"] == "测试媒体服务器"
    assert view["source_icon"] == "testsrc"
    assert view["source_configured"] is True
    assert view["capabilities"]["scraped"] is False
    assert view["capabilities"]["jellyfin_collection"] == "movies"
    assert view["root_paths"] == []


def test_create_external_library_rejects_bad_config(client, provider) -> None:
    r = client.post(
        "/api/v1/libraries",
        json={
            "name": "坏配置",
            "kind": "movie",
            "source": "testsrc",
            "source_config": {"library_id": "lib-1"},  # 缺必填的 base_url
        },
    )
    assert r.status_code == 400
    assert "连接配置不合规范" in r.json()["message"]


def test_create_builtin_library_rejects_source_config(client) -> None:
    r = client.post(
        "/api/v1/libraries",
        json={
            "name": "本地电影",
            "kind": "movie",
            "root_paths": ["/media/movies"],
            "source_config": {"base_url": "http://x"},
        },
    )
    assert r.status_code == 400
    assert "不能保存外部连接配置" in r.json()["message"]


def test_create_with_unregistered_source_rejected(client) -> None:
    # 来源未登记（插件停用/卸载）：与未知组合一样走 400，不静默落库
    r = client.post(
        "/api/v1/libraries",
        json={"name": "幽灵库", "kind": "movie", "source": "ghostsrc", "root_paths": []},
    )
    assert r.status_code == 400
    assert "不支持的媒体库类型组合" in r.json()["message"]


# ---------------------------------------------------------------------------
# 连接配置编辑
# ---------------------------------------------------------------------------


def _create_external(client) -> dict:
    r = client.post(
        "/api/v1/libraries",
        json={
            "name": "外部剧集",
            "kind": "tv",
            "source": "testsrc",
            "source_config": {"base_url": "http://fnos:5666", "library_id": "lib-2"},
        },
    )
    assert r.status_code == 200
    return r.json()["data"]


def test_update_source_config(client, provider) -> None:
    lib = _create_external(client)
    r = client.put(
        f"/api/v1/libraries/{lib['id']}",
        json={
            "kind": "tv",
            "name": "外部剧集",
            "root_paths": [],
            "source_config": {"base_url": "http://fnos2:5666", "library_id": "lib-9"},
        },
    )
    assert r.status_code == 200
    assert r.json()["data"]["source_configured"] is True

    # None = 不改动：配置保持
    r = client.put(
        f"/api/v1/libraries/{lib['id']}",
        json={"kind": "tv", "name": "外部剧集", "root_paths": []},
    )
    assert r.status_code == 200
    assert r.json()["data"]["source_configured"] is True


def test_update_builtin_library_ignores_unset_source_config(client) -> None:
    """内置库编辑不受影响：不带 source_config 的老请求体照常保存。"""
    r = client.post(
        "/api/v1/libraries",
        json={"name": "普通库", "kind": "movie", "root_paths": ["/media/m"]},
    )
    assert r.status_code == 200
    lib = r.json()["data"]
    r = client.put(
        f"/api/v1/libraries/{lib['id']}",
        json={"kind": "movie", "name": "普通库改名", "root_paths": ["/media/m"]},
    )
    assert r.status_code == 200
    assert r.json()["data"]["source_title"] is None


def test_plugin_unloaded_library_still_readable(client, provider) -> None:
    """插件卸载后：来源不再登记，但存量库照常可读、可改名（能力位回落报错
    由 profile_for 的 400 承担，列表不崩）。"""
    lib = _create_external(client)
    # 模拟插件卸载：解绑注册表（插件内核关闭时也是这个状态）
    sp._bound = None
    r = client.get("/api/v1/libraries")
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()["data"]}
    assert rows[lib["id"]]["source_title"] is None
    # 编辑（不碰来源配置）也照常
    r = client.put(
        f"/api/v1/libraries/{lib['id']}",
        json={"kind": "tv", "name": "外部剧集改名", "root_paths": []},
    )
    assert r.status_code == 200
