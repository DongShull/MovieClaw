"""登录设备迁移的回归测试（alembic 5b8e2c4f9a17）。

已配对的命令行与转码器原来存在配置域 ``auth.api_tokens`` 的 JSON 里；迁移把它们
原样搬进 ``login_device`` 表——令牌哈希不变，升级后**不用重新配对**。这是用户
升级时唯一能感知到的风险点，值得为它锁一次行为。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from alembic import command

from movieclaw_api.core.config import get_settings
from movieclaw_db.migrations import _build_config

_BEFORE = "2973f590e15e"


def test_legacy_tokens_move_into_login_device(tmp_path, monkeypatch) -> None:
    database = tmp_path / "login-device.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    get_settings.cache_clear()
    config = _build_config()
    command.upgrade(config, _BEFORE)

    worker_hash = hashlib.sha256(b"mclaw_worker-token").hexdigest()
    cli_hash = hashlib.sha256(b"mclaw_cli-token").hexdigest()
    legacy = {
        "tokens": [
            {
                "id": "a1",
                "name": "Yi的Mac-mini",
                "token_hash": worker_hash,
                "created_at": "2026-09-20T08:00:00+00:00",
                "client_type": "worker",
                "owner_kind": "admin",
                "last_used_at": "2026-09-26T21:30:00+00:00",
            },
            {
                "id": "b2",
                "name": "mclaw@macbook",
                "token_hash": cli_hash,
                "created_at": "2026-09-21T09:00:00+08:00",
                "client_type": "cli",
            },
            # 同一枚令牌出现两次（手工改坏的配置）：只搬一行，撞不上唯一索引
            {"id": "c3", "name": "dup", "token_hash": cli_hash, "created_at": "bad"},
            # 缺哈希的残缺记录：跳过
            {"id": "d4", "name": "broken"},
        ]
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO app_setting (namespace, value_json, created_at, updated_at) "
            "VALUES ('auth.api_tokens', ?, '2026-09-20 08:00:00', '2026-09-20 08:00:00')",
            (json.dumps(legacy),),
        )

    command.upgrade(config, "head")

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT member_id, kind, name, token_hash, scope, created_at, last_seen_at, "
            "expires_at FROM login_device ORDER BY id"
        ).fetchall()
        leftover = connection.execute(
            "SELECT COUNT(*) FROM app_setting WHERE namespace = 'auth.api_tokens'"
        ).fetchone()[0]

    assert [(r[0], r[1], r[2], r[3], r[4]) for r in rows] == [
        (0, "worker", "Yi的Mac-mini", worker_hash, "transcode"),
        (0, "cli", "mclaw@macbook", cli_hash, "full"),
    ]
    # 时间换成数据库约定的朴素 UTC；长期有效（不设过期）
    assert rows[0][5].startswith("2026-09-20 08:00:00")
    assert rows[0][6].startswith("2026-09-26 21:30:00")
    assert rows[1][5].startswith("2026-09-21 01:00:00")
    assert rows[0][7] is None and rows[1][7] is None
    # 旧配置域搬完即删，不留一份再也没人维护的凭证清单
    assert leftover == 0
    get_settings.cache_clear()


def test_migration_without_legacy_tokens_is_a_no_op(tmp_path, monkeypatch) -> None:
    database = tmp_path / "fresh.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    get_settings.cache_clear()
    command.upgrade(_build_config(), "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM login_device").fetchone()[0] == 0
    get_settings.cache_clear()
