"""小艺云 A2A 端点（POST /api/v1/a2a/agent/message）的端到端测试。

真实应用 + 模拟小艺平台全流程：未启用 404 → 启用后 initialize 拿 session →
message/stream 收到完整 SSE 事件序列（status/artifact/终态 final）→ tasks/cancel →
clearContext 换会话。LLM 用假协议桩（不出网），验签用与华为文档同算法的真实 HMAC。

协议规格：docs/research/xiaoyi-cloud-a2a.md。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_llm.base import BaseLlmProtocol
from movieclaw_llm.models import ChatResponse, ChatStreamEvent, TokenUsage

ENDPOINT = "/api/v1/a2a/agent/message"
ACCESS_KEY = "ak-test-123"
SECRET_KEY = "sk-test-456"


def _sign(ts: str) -> str:
    return base64.b64encode(
        hmac.new(SECRET_KEY.encode(), ts.encode(), hashlib.sha256).digest()
    ).decode()


def _headers(style: str = "plain", ts: str | None = None) -> dict[str, str]:
    ts = ts or str(int(time.time() * 1000))
    sign = _sign(ts)
    if style == "x":
        return {"X-Access-Key": ACCESS_KEY, "X-Sign": sign, "X-Ts": ts}
    return {"accessKey": ACCESS_KEY, "sign": sign, "ts": ts}


def _rpc(method: str, params: dict | None = None, *, rpc_id: str = "req-1", **top) -> dict:
    body: dict = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    body.update(top)
    return body


def _parse_sse(body: str) -> list[dict]:
    """A2A 的 SSE 只用 data: 行；按块拆出 JSON 载荷，顺序即推送顺序。"""
    frames = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        for line in block.splitlines():
            if line.startswith("data: "):
                frames.append(json.loads(line[len("data: "):]))
    return frames


async def _enable(client: TestClient, monkeypatch, **overrides) -> None:
    """写入启用的 A2A 配置（直接进配置存储；测试环境加密器已由 lifespan 初始化）。"""
    from movieclaw_api.settings import get_setting_store
    from movieclaw_api.settings.xiaoyi_a2a import XiaoyiA2aSetting

    store = get_setting_store()
    cfg = XiaoyiA2aSetting(
        enabled=True,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        **overrides,
    )
    await store.set(cfg)


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("MOVIECLAW_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    with TestClient(app) as c:
        yield c
    # 配置存储是进程级单例：本用例写入的缓存必须清掉，否则泄漏到下一个用例
    # （test_disabled 这类「未配置」断言会读到上一条留下的启用配置）
    from movieclaw_api.settings.store import reset_setting_store

    reset_setting_store()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 假 LLM 协议：一步出终答，产出 text_delta 事件流（不出网）
# ---------------------------------------------------------------------------


class _EchoProtocol(BaseLlmProtocol):
    async def chat(self, request, model_id):  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover
        return None

    async def test_connection(self):  # pragma: no cover
        from movieclaw_llm.models import ProviderInfo

        return ProviderInfo(models=["qwen3.7-max"])

    async def chat_stream(self, request, model_id):
        snap = ChatResponse(model=model_id, provider=self.config.name)
        yield ChatStreamEvent(type="start", partial=snap)
        yield ChatStreamEvent(type="thinking_delta", delta="让我想想", partial=snap)
        yield ChatStreamEvent(type="text_delta", delta="找到 3 部影片", partial=snap)
        yield ChatStreamEvent(
            type="done",
            partial=ChatResponse(
                content="找到 3 部影片",
                finish_reason="stop",
                usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
                model=model_id,
                provider=self.config.name,
            ),
        )


def _seed_llm_provider(client: TestClient, monkeypatch) -> None:
    """配一个默认供应商（acquire_llm_router 不再 404），协议换成假桩。"""
    from movieclaw_llm.protocols import PROTOCOLS

    monkeypatch.setitem(PROTOCOLS, "openai_chat", _EchoProtocol)
    r = client.post(
        "/api/v1/llm/providers",
        json={
            "name": "假百炼",
            "provider_type": "bailian",
            "api_key": "***",
            "default_model": "qwen3.7-max",
        },
    )
    assert r.status_code in (200, 201), r.text


# ---------------------------------------------------------------------------
# 端点开关与验签
# ---------------------------------------------------------------------------


async def test_admin_config_put_get_masks_secret(client) -> None:
    """管理面 PUT 落库 + GET 回显明文（配置页展示 + 打码指纹并存）。"""
    r = client.put(
        "/api/v1/a2a/config",
        json={
            "enabled": True,
            "access_key": ACCESS_KEY,
            "secret_key": SECRET_KEY,
            "session_mode": "assigned",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["enabled"] is True
    assert body["access_key"] == ACCESS_KEY
    assert body["secret_key"] == SECRET_KEY  # 明文回显供配置页粘贴
    assert body["secret_key_hint"].endswith("****")

    got = client.get("/api/v1/a2a/config").json()["data"]
    assert got["secret_key"] == SECRET_KEY
    assert got["secret_key_hint"].endswith("****")

    # 启用后端点立即可用（不再是 404）
    r = client.post(ENDPOINT, json=_rpc("initialize"), headers=_headers())
    assert r.status_code == 200


async def test_rotate_generates_working_credentials(client) -> None:
    """rotate 生成一对新 AK/SK，PUT 启用（secret 留空沿用生成值）后立即可用。"""
    r = client.post("/api/v1/a2a/config/rotate")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["access_key"].startswith("mcak_")
    assert data["secret_key"].startswith("mcsk_")
    assert data["secret_key_hint"].endswith("****")

    ak, sk = data["access_key"], data["secret_key"]

    # 启用：secret_key 留空 = 沿用 rotate 刚生成的 secret（明文不回传）
    r = client.put(
        "/api/v1/a2a/config",
        json={"enabled": True, "access_key": ak, "secret_key": "", "session_mode": "assigned"},
    )
    assert r.status_code == 200, r.text

    # 用 rotate 返回的明文算签名，端点应接受（不是 404/401）
    ts = str(int(time.time() * 1000))
    sign = base64.b64encode(
        hmac.new(sk.encode(), ts.encode(), hashlib.sha256).digest()
    ).decode()
    r = client.post(
        ENDPOINT,
        json=_rpc("initialize"),
        headers={"accessKey": ak, "sign": sign, "ts": ts},
    )
    assert r.status_code == 200, r.text

    # GET 回显明文 secret——配置页要把它连同 accessKey 一起粘进小艺开放平台
    got = client.get("/api/v1/a2a/config").json()["data"]
    assert got["secret_key"] == sk
    assert got["secret_key_hint"].endswith("****")


async def test_disabled_returns_404(client) -> None:
    r = client.post(ENDPOINT, json=_rpc("initialize"), headers=_headers())
    assert r.status_code == 404


def test_generated_credentials_fit_platform_limit() -> None:
    """生成的凭据必须落在小艺开放平台 64 字符上限内。

    平台字段超过 64 字符会**静默截断**（实测 69 字符的 secretKey 贴进去只存前 64），
    截断后的值与后端存的不一致，签名永远验不过。这里守住生成侧不再产出超长值。
    """
    from movieclaw_api.settings.xiaoyi_a2a import (
        _CREDENTIAL_MAX_LENGTH,
        generate_access_key,
        generate_api_key,
        generate_secret_key,
    )

    for value in (generate_access_key(), generate_secret_key(), generate_api_key()):
        assert len(value) <= _CREDENTIAL_MAX_LENGTH


async def test_apikey_mode_header_ok(client, monkeypatch) -> None:
    """APIKey 鉴权：令牌放 X-API-Key 头，命中即放行 initialize。"""
    await _enable(client, monkeypatch, auth_mode="apikey", api_key="mcapi_test_apikey")
    r = client.post(
        ENDPOINT,
        json=_rpc("initialize"),
        headers={"X-API-Key": "mcapi_test_apikey"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"]["agentSessionId"]


async def test_apikey_mode_authorization_header_ok(client, monkeypatch) -> None:
    """华为实际用法：令牌放 Authorization 头（Header 域传参），原样命中。"""
    await _enable(client, monkeypatch, auth_mode="apikey", api_key="my-key-123")
    r = client.post(
        ENDPOINT,
        json=_rpc("initialize"),
        headers={"Authorization": "my-key-123"},
    )
    assert r.status_code == 200, r.text


async def test_apikey_mode_bearer_prefix_ok(client, monkeypatch) -> None:
    """兼容 Authorization: Bearer <key> 的写法。"""
    await _enable(client, monkeypatch, auth_mode="apikey", api_key="my-key-123")
    r = client.post(
        ENDPOINT,
        json=_rpc("initialize"),
        headers={"Authorization": "Bearer my-key-123"},
    )
    assert r.status_code == 200, r.text


async def test_apikey_mode_query_ok(client, monkeypatch) -> None:
    """APIKey 鉴权：令牌放 Query 参数也能命中。"""
    await _enable(client, monkeypatch, auth_mode="apikey", api_key="mcapi_test_apikey")
    r = client.post(
        f"{ENDPOINT}?apiKey=mcapi_test_apikey",
        json=_rpc("initialize"),
    )
    assert r.status_code == 200, r.text


async def test_apikey_mode_wrong_key_401(client, monkeypatch) -> None:
    await _enable(client, monkeypatch, auth_mode="apikey", api_key="mcapi_test_apikey")
    r = client.post(
        ENDPOINT,
        json=_rpc("initialize"),
        headers={"X-API-Key": "wrong-key"},
    )
    assert r.status_code == 401


async def test_apikey_mode_rejects_aksk_headers(client, monkeypatch) -> None:
    """apikey 模式下，AK/SK 签名头不应放行（两种鉴权互斥）。"""
    await _enable(client, monkeypatch, auth_mode="apikey", api_key="mcapi_test_apikey")
    r = client.post(ENDPOINT, json=_rpc("initialize"), headers=_headers())
    assert r.status_code == 401


async def test_enabled_bad_signature_401(client, monkeypatch) -> None:
    await _enable(client, monkeypatch)
    ts = str(int(time.time() * 1000))
    bad = {"accessKey": ACCESS_KEY, "sign": "x" + _sign(ts)[1:], "ts": ts}
    r = client.post(ENDPOINT, json=_rpc("initialize"), headers=bad)
    assert r.status_code == 401


async def test_enabled_expired_ts_rejected(client, monkeypatch) -> None:
    await _enable(client, monkeypatch)
    old = str(int(time.time() * 1000) - 16 * 60 * 1000)  # 16 分钟前
    r = client.post(ENDPOINT, json=_rpc("initialize"), headers=_headers(ts=old))
    assert r.status_code == 401


async def test_x_style_headers_accepted(client, monkeypatch) -> None:
    """X- 前缀命名的 header 也认（实测平台用裸名，两种都兼容）。"""
    await _enable(client, monkeypatch)
    r = client.post(ENDPOINT, json=_rpc("initialize"), headers=_headers(style="x"))
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["version"] == "1.0"
    # 自签令牌：形如 "<过期时间hex>.<32 位签名>"，长度守在平台字段 64 上限内
    assert len(result["agentSessionId"]) <= 64


# ---------------------------------------------------------------------------
# JSON-RPC 分发
# ---------------------------------------------------------------------------


async def test_initialize_returns_session(client, monkeypatch) -> None:
    await _enable(client, monkeypatch)
    r = client.post(ENDPOINT, json=_rpc("initialize"), headers=_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == "req-1"
    assert body["result"]["agentSessionTtl"] == "604800"


async def _initialize_session(client) -> str:
    r = client.post(ENDPOINT, json=_rpc("initialize"), headers=_headers())
    assert r.status_code == 200, r.text
    return r.json()["result"]["agentSessionId"]


async def test_mode1_session_header_authenticates_followups(client, monkeypatch) -> None:
    """模式1：initialize 分配的 agent-session-id 由客户端在 header 带回即放行。

    对齐华为平台的实测行为：initialize 带 AK/SK 签名（模式2），之后的请求只带
    ``agent-session-id`` 头、不再重复签名。
    """
    await _enable(client, monkeypatch)
    session_id = await _initialize_session(client)

    # 只带会话 id、不带任何 AK/SK：应通过鉴权并进到方法分发
    r = client.post(ENDPOINT, json=_rpc("bogus/method"), headers={"agent-session-id": session_id})
    assert r.status_code == 200, r.text
    assert r.json()["error"]["code"] == -32601

    # 篡改签名位 → 401
    tampered = ("0" if session_id[0] != "0" else "1") + session_id[1:]
    r = client.post(ENDPOINT, json=_rpc("bogus/method"), headers={"agent-session-id": tampered})
    assert r.status_code == 401


async def test_mode1_expired_session_id_rejected(client, monkeypatch) -> None:
    """过期的 agent-session-id 失效（签名里带过期时间戳）。"""
    await _enable(client, monkeypatch)
    from movieclaw_api.api.routes.xiaoyi_a2a import _session_signature
    from movieclaw_api.settings import get_setting_store
    from movieclaw_api.settings.xiaoyi_a2a import XiaoyiA2aSetting

    cfg = await get_setting_store().get(XiaoyiA2aSetting)
    expired_ms = int((time.time() - 3600) * 1000)
    token = f"{expired_ms:x}.{_session_signature(cfg, expired_ms)[:32]}"
    r = client.post(ENDPOINT, json=_rpc("bogus/method"), headers={"agent-session-id": token})
    assert r.status_code == 401


async def test_mode1_bogus_session_id_rejected(client, monkeypatch) -> None:
    """凭空捏造的会话 id 一律 401（必须带后端密钥签名）。"""
    await _enable(client, monkeypatch)
    r = client.post(
        ENDPOINT,
        json=_rpc("bogus/method"),
        headers={"agent-session-id": "a48dacff40e86b4cec321bc14253f618"},
    )
    assert r.status_code == 401


async def test_mode1_session_works_in_apikey_mode(client, monkeypatch) -> None:
    """APIKey 认证下同样支持模式1：会话 id 用 api_key 签名，与鉴权方式正交。"""
    await _enable(client, monkeypatch, auth_mode="apikey", api_key="mcapi_test_apikey")
    r = client.post(ENDPOINT, json=_rpc("initialize"), headers={"X-API-Key": "mcapi_test_apikey"})
    assert r.status_code == 200, r.text
    session_id = r.json()["result"]["agentSessionId"]

    # 后续只带 session-id（不带 APIKey）也放行
    r = client.post(ENDPOINT, json=_rpc("bogus/method"), headers={"agent-session-id": session_id})
    assert r.status_code == 200, r.text
    assert r.json()["error"]["code"] == -32601

    tampered = ("0" if session_id[0] != "0" else "1") + session_id[1:]
    r = client.post(ENDPOINT, json=_rpc("bogus/method"), headers={"agent-session-id": tampered})
    assert r.status_code == 401


async def test_notifications_initialized_no_body(client, monkeypatch) -> None:
    await _enable(client, monkeypatch)
    r = client.post(ENDPOINT, json=_rpc("notifications/initialized"), headers=_headers())
    assert r.status_code == 200


async def test_unknown_method_32601(client, monkeypatch) -> None:
    await _enable(client, monkeypatch)
    r = client.post(ENDPOINT, json=_rpc("bogus/method"), headers=_headers())
    assert r.json()["error"]["code"] == -32601


async def test_bad_json_32700(client, monkeypatch) -> None:
    await _enable(client, monkeypatch)
    r = client.post(ENDPOINT, content=b"{not json", headers=_headers())
    assert r.json()["error"]["code"] == -32700


async def test_second_phase_methods_rejected(client, monkeypatch) -> None:
    await _enable(client, monkeypatch)
    for method in ("authorize", "deauthorize", "push"):
        r = client.post(ENDPOINT, json=_rpc(method), headers=_headers())
        assert r.json()["error"]["code"] == -32601, method


# ---------------------------------------------------------------------------
# message/stream 全流程（核心）
# ---------------------------------------------------------------------------


async def test_message_stream_full_flow(client, monkeypatch) -> None:
    _seed_llm_provider(client, monkeypatch)
    await _enable(client, monkeypatch)

    payload = _rpc(
        "message/stream",
        params={
            "id": "task-001",
            "sessionId": "xiaoyi-session-A",
            "message": {"role": "user", "parts": [{"kind": "text", "text": "库里的新片"}]},
        },
    )
    with client.stream("POST", ENDPOINT, json=payload, headers=_headers()) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = resp.read().decode()
    frames = _parse_sse(body)

    kinds = [(f["result"]["kind"], f["result"].get("final")) for f in frames]
    # 开场 working → 正文 artifact（reasoning+text）→ 终态 completed(final=True)
    assert kinds[0] == ("status-update", False)
    assert ("artifact-update", False) in kinds
    assert kinds[-1] == ("status-update", True)

    # 终态事件是 completed；最后帧的 taskId 原样回传
    assert frames[-1]["result"]["status"]["state"] == "completed"
    assert all(f["result"]["taskId"] == "task-001" for f in frames)
    assert all(f["id"] == "req-1" for f in frames)

    # 正文以 artifact 的 text part 流出，reasoningText part 也存在
    parts = [p for f in frames if f["result"]["kind"] == "artifact-update"
             for p in f["result"]["artifact"]["parts"]]
    kinds_of_parts = {p["kind"] for p in parts}
    assert "text" in kinds_of_parts


async def test_message_stream_missing_params_32602(client, monkeypatch) -> None:
    await _enable(client, monkeypatch)
    r = client.post(
        ENDPOINT,
        json=_rpc("message/stream", params={"id": "t1"}),  # 缺 sessionId
        headers=_headers(),
    )
    assert r.json()["error"]["code"] == -32602


async def test_sessions_isolated_per_platform_session(client, monkeypatch) -> None:
    """两个平台 sessionId 各自独立 AI 会话——绝不串上下文。"""
    _seed_llm_provider(client, monkeypatch)
    await _enable(client, monkeypatch)

    for sid in ("session-A", "session-B"):
        payload = _rpc(
            "message/stream",
            params={
                "id": f"task-{sid}",
                "sessionId": sid,
                "message": {"role": "user", "parts": [{"kind": "text", "text": "你好"}]},
            },
        )
        with client.stream("POST", ENDPOINT, json=payload, headers=_headers()) as resp:
            body = resp.read().decode()
        frames = _parse_sse(body)
        assert frames
        assert frames[-1]["result"]["status"]["state"] == "completed"

    # 映射表两行、指向不同 AI 会话
    import sqlite3

    from movieclaw_api.core.config import get_settings as gs

    db = gs().database_url.split("///")[-1]
    rows = sqlite3.connect(db).execute(
        "SELECT platform_session_id, agent_session_id FROM xiaoyi_a2a_session"
    ).fetchall()
    assert {r[0] for r in rows} == {"session-A", "session-B"}
    assert len({r[1] for r in rows}) == 2  # 不同 AI 会话


async def test_clear_context_creates_new_session(client, monkeypatch) -> None:
    _seed_llm_provider(client, monkeypatch)
    await _enable(client, monkeypatch)

    # 先跑一轮建立映射
    payload = _rpc(
        "message/stream",
        params={
            "id": "t1",
            "sessionId": "sess-1",
            "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
        },
    )
    with client.stream("POST", ENDPOINT, json=payload, headers=_headers()):
        pass

    import sqlite3

    from movieclaw_api.core.config import get_settings as gs

    db = gs().database_url.split("///")[-1]
    before = sqlite3.connect(db).execute(
        "SELECT agent_session_id FROM xiaoyi_a2a_session WHERE platform_session_id='sess-1'"
    ).fetchone()[0]

    # clearContext（文档示例：sessionId 在顶层）
    r = client.post(
        ENDPOINT, json=_rpc("clearContext", sessionId="sess-1"), headers=_headers()
    )
    assert r.status_code == 200
    assert r.json()["result"]["status"]["state"] == "cleared"

    after = sqlite3.connect(db).execute(
        "SELECT agent_session_id FROM xiaoyi_a2a_session WHERE platform_session_id='sess-1'"
    ).fetchone()[0]
    assert after != before  # 换了新 AI 会话


async def test_cancel_unknown_task_32001(client, monkeypatch) -> None:
    await _enable(client, monkeypatch)
    r = client.post(ENDPOINT, json=_rpc("tasks/cancel", id="no-such"), headers=_headers())
    assert r.json()["error"]["code"] == -32001


async def test_no_llm_provider_yields_failed_stream(client, monkeypatch) -> None:
    """没配模型供应商：流以 failed 终态收尾（不是断流）。"""
    await _enable(client, monkeypatch)
    payload = _rpc(
        "message/stream",
        params={
            "id": "t1",
            "sessionId": "sess-x",
            "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
        },
    )
    with client.stream("POST", ENDPOINT, json=payload, headers=_headers()) as resp:
        body = resp.read().decode()
    frames = _parse_sse(body)
    assert frames[-1]["result"]["status"]["state"] == "failed"
    assert frames[-1]["result"]["final"] is True
