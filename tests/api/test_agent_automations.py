"""Agent 自动化的 API 面：CRUD、策略端点与守护测试（防自我增殖）。

对应 docs/design/agent-automations.md 的验收：
- 五端点 + 策略 + run 历史 API 通（P0）；
- automation 身份的令牌对 automations.* 一律 403（P6 守护）；
- run-now 与节拍共用闸门（P2），无供应商配置时失败落 run 留痕（L5）。
"""

from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.api.deps import require_login
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import Principal

_ADMIN = Principal(kind="admin", name="tester")
_AUTOMATION = Principal(
    kind="agent", name="agent:abc123", agent_session_id="abc123", automation_id=7
)

_BASE = "/api/v1/agent/automations"


_db_path = ""  # 夹具里回填：原生 sqlite3 播种用


@pytest.fixture
def client(tmp_path, monkeypatch):
    global _db_path
    _db_path = str(tmp_path / "auto.db")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{_db_path}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("AGENT_SESSIONS_DIR", str(tmp_path / "agent-sessions"))
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(tmp_path / "agent-workspace"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    from movieclaw_api.services.agent_attachments import reset_agent_attachment_store
    from movieclaw_api.services.agent_sessions import reset_agent_session_store
    from movieclaw_api.settings import reset_setting_store

    reset_agent_session_store()
    reset_agent_attachment_store()
    reset_setting_store()  # 自动化策略是进程级缓存：用例间必须隔离

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app

    app = create_app()
    app.dependency_overrides[require_login] = lambda: _ADMIN
    with TestClient(app, headers={"Accept-Language": "zh-Hans"}) as c:
        yield c
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    reset_agent_session_store()
    reset_agent_attachment_store()
    reset_setting_store()


def _create(client, **overrides) -> dict:
    payload = {
        "name": "每日入库汇总",
        "prompt": "汇总昨天入库的影片，按类型分组输出。",
        "trigger_type": "interval",
        "trigger_expr": "3600",
    }
    payload.update(overrides)
    return client.post(_BASE, json=payload)


# ---------------------------------------------------------------------------
# 守护测试：automation 身份禁止管理 automations（P6 验收）
# ---------------------------------------------------------------------------
_ALL_METHODS = [
    ("get", _BASE),
    ("post", _BASE),
    ("put", f"{_BASE}/policy"),
    ("get", f"{_BASE}/policy"),
    ("put", f"{_BASE}/1"),
    ("delete", f"{_BASE}/1"),
    ("post", f"{_BASE}/1/run-now"),
    ("get", f"{_BASE}/1/runs"),
]


@pytest.mark.parametrize("method,path", _ALL_METHODS)
def test_automation_identity_forbidden_on_all_endpoints(client, method, path):
    """automation 令牌调任何 automations 端点都得 403——「自动化建自动化」的环
    在路由层掐断（docs/design/agent-automations.md §2.3）。"""
    app = client.app
    app.dependency_overrides[require_login] = lambda: _AUTOMATION
    try:
        if method in ("get", "delete"):
            resp = getattr(client, method)(path)
        else:
            resp = getattr(client, method)(path, json={})
        assert resp.status_code == 403, resp.text
        assert "自动化" in resp.json()["message"]
    finally:
        app.dependency_overrides[require_login] = lambda: _ADMIN


def test_normal_agent_identity_still_allowed_to_list(client):
    """普通网页会话签发的 agent 令牌不受影响——Agent 替用户建自动化是关键闭环。"""
    agent = Principal(kind="agent", name="agent:web1", agent_session_id="web1")
    client.app.dependency_overrides[require_login] = lambda: agent
    try:
        resp = client.get(_BASE)
        assert resp.status_code == 200
        assert resp.json()["data"] == []
    finally:
        client.app.dependency_overrides[require_login] = lambda: _ADMIN


# ---------------------------------------------------------------------------
# CRUD 与策略
# ---------------------------------------------------------------------------
def test_create_and_list_roundtrip(client) -> None:
    resp = _create(client)
    assert resp.status_code == 201, resp.text
    row = resp.json()["data"]
    assert row["name"] == "每日入库汇总"
    assert row["enabled"] is True
    assert row["trigger_type"] == "interval"
    assert row["max_runs_per_day"] == 12
    assert row["max_tokens_per_run"] == 30000
    assert row["delivery_channels"] == "push"
    assert row["next_run_at"]

    listed = client.get(_BASE).json()["data"]
    assert [item["id"] for item in listed] == [row["id"]]
    assert listed[0]["last_run"] is None


def test_create_validates_trigger_and_quiet_hours(client) -> None:
    resp = _create(client, trigger_type="cron", trigger_expr="not a cron")
    assert resp.status_code == 400
    assert resp.json()["message_key"] == "errors.agent.automationTriggerInvalid"

    resp = _create(client, trigger_type="interval", trigger_expr="10")
    assert resp.status_code == 400  # interval 至少 60s

    resp = _create(client, quiet_hours="25:00-07:00")
    assert resp.status_code == 400
    assert resp.json()["message_key"] == "errors.agent.automationQuietHoursInvalid"

    resp = _create(client, delivery_channels="push,webhook")
    assert resp.status_code == 400
    assert resp.json()["message_key"] == "errors.agent.automationChannelsInvalid"

    resp = _create(client, prompt="   ")
    assert resp.status_code == 400
    assert resp.json()["message_key"] == "errors.agent.automationPromptRequired"


def test_create_cron_computes_next_run(client) -> None:
    resp = _create(client, trigger_type="cron", trigger_expr="0 8 * * *")
    assert resp.status_code == 201
    # 下次到期必须是未来的一个 08:00（调度器时区 Asia/Shanghai = UTC+8）
    assert resp.json()["data"]["next_run_at"].endswith("00:00")


def test_update_renames_and_rerolls_schedule(client) -> None:
    row = _create(client).json()["data"]
    resp = client.put(
        f"{_BASE}/{row['id']}",
        json={"name": "每周报告", "trigger_type": "cron", "trigger_expr": "30 9 * * 1"},
    )
    assert resp.status_code == 200
    updated = resp.json()["data"]
    assert updated["name"] == "每周报告"
    assert updated["trigger_expr"] == "30 9 * * 1"

    # 熔断恢复通道：停用告警熄灯、失败计数清零（无告警时静默成功）
    assert client.put(f"{_BASE}/{row['id']}", json={"enabled": False}).status_code == 200
    assert client.put(f"{_BASE}/{row['id']}", json={"enabled": True}).status_code == 200


def test_delete_removes_definition(client) -> None:
    row = _create(client).json()["data"]
    assert client.delete(f"{_BASE}/{row['id']}").status_code == 200
    assert client.get(_BASE).json()["data"] == []
    assert client.delete(f"{_BASE}/{row['id']}").status_code == 404


def test_policy_show_and_update(client) -> None:
    shown = client.get(f"{_BASE}/policy").json()["data"]
    assert shown["enabled"] is False  # 发布默认关
    assert shown["daily_token_budget"] == 200_000

    resp = client.put(f"{_BASE}/policy", json={"enabled": True, "daily_token_budget": 50_000})
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is True
    assert client.get(f"{_BASE}/policy").json()["data"]["daily_token_budget"] == 50_000


def test_runs_history_paginated(client) -> None:
    row = _create(client).json()["data"]
    # 直接落几条 run 留痕（原生 sqlite3 与应用的 WAL 连接并存安全），验证分页与排序
    # status 列由 SQLModel 映射为 sa.Enum：落库存枚举名（COMPLETED），不是值
    con = sqlite3.connect(_db_path)
    try:
        for i in range(7):
            con.execute(
                "INSERT INTO agent_automation_run"
                " (automation_id, started_at, finished_at, status, summary, tokens_used)"
                " VALUES (?, ?, ?, 'COMPLETED', ?, 0)",
                (row["id"], f"2026-10-30 0{i}:00:00", f"2026-10-30 0{i}:05:00", f"第 {i} 轮"),
            )
        con.commit()
    finally:
        con.close()
    resp = client.get(f"{_BASE}/{row['id']}/runs", params={"limit": 3})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 3
    # 倒序：最新在前
    assert data[0]["summary"] == "第 6 轮"
    page2 = client.get(f"{_BASE}/{row['id']}/runs", params={"offset": 3, "limit": 3}).json()["data"]
    assert page2[0]["summary"] == "第 3 轮"
    assert client.get(f"{_BASE}/999/runs").status_code == 404


# ---------------------------------------------------------------------------
# run-now：与节拍共用闸门
# ---------------------------------------------------------------------------
def test_run_now_respects_kill_switch(client) -> None:
    """全局开关关着：run-now 只落一条 skipped 留痕，不拉起会话（P2 验收）。"""
    row = _create(client).json()["data"]
    resp = client.post(f"{_BASE}/{row['id']}/run-now")
    assert resp.status_code == 200
    run = resp.json()["data"]
    assert run["status"] == "skipped"
    assert run["skip_reason"] == "disabled"
    # 列表里能看到这轮 skipped 摘要
    listed = client.get(_BASE).json()["data"]
    assert listed[0]["last_run"]["skip_reason"] == "disabled"


def test_run_now_without_provider_fails_into_run_row(client) -> None:
    """开关开着但没有模型供应商：本轮落 failed 留痕，失败计数与退避生效（L5）。"""
    row = _create(client).json()["data"]
    client.put(f"{_BASE}/policy", json={"enabled": True})
    resp = client.post(f"{_BASE}/{row['id']}/run-now")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "running"

    # 后台任务会很快以「未配置模型供应商」收场；轮询等待终态（外置卷上
    # 会话文件与 DB 都慢，给足 30s）
    status = "running"
    for _ in range(300):
        runs = client.get(f"{_BASE}/{row['id']}/runs").json()["data"]
        status = runs[0]["status"]
        if status != "running":
            break
        time.sleep(0.1)
    assert status == "failed"

    detail = client.get(_BASE).json()["data"][0]
    assert detail["consecutive_failures"] == 1
    assert detail["last_error"]
    # 退避 1h：下一次到期被推到至少 59 分钟之后（与本轮启动的时间差留裕量）
    from datetime import datetime

    next_run = datetime.fromisoformat(detail["next_run_at"])
    started = datetime.fromisoformat(detail["last_run_at"])
    assert (next_run - started).total_seconds() >= 59 * 60
