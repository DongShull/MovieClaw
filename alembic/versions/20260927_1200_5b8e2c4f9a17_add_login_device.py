"""add login_device：统一的「人 × 设备」凭证表

docs/design/login-devices.md。网页登录会话、原生 App、命令行、转码器、网页
手工创建的令牌从此落在同一张表：一行 = 一个人授权给一台客户端的一枚长期凭证，
只存令牌哈希，权限在验签时按主人当前身份装配。

存量迁移：命令行 / 转码器 / 手工令牌原来存在配置域 ``auth.api_tokens`` 的一段
JSON 里（只有超管能批准，所以一律记到超管名下，member_id=0）。这里原样搬进新表——
令牌哈希不变，已配对的命令行和转码器**不用重新配对**；搬完删掉旧配置域，免得
留一份再也没人维护、却还能被误读的凭证清单。

网页会话不需要迁移：升级前签发的签名 Cookie 仍被新代码接受直至自然过期
（最长 30 天），新登录一律发表内令牌。

回退：跨过本迁移的回退会由回退选择器自动恢复升级前的数据库备份
（docs/design/in-app-update.md「多版本回退与数据兼容」），旧配置域随备份回来，
所以这里可以放心删除旧数据。无运行时依赖变更，不 bump runtime-version。

Revision ID: 5b8e2c4f9a17
Revises: 2973f590e15e
Create Date: 2026-09-27 12:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "5b8e2c4f9a17"
down_revision: str | None = "2973f590e15e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 旧配置域的命名空间（迁移冻结在写下时的事实，不 import 应用代码）
_LEGACY_NAMESPACE = "auth.api_tokens"
#: 旧 client_type → 新 kind。worker 的凭证只能转码（scope=transcode）。
_KINDS = {"worker": "worker", "cli": "cli", "manual": "manual"}


def _naive_utc(value: object) -> datetime | None:
    """ISO8601 字符串 → 数据库约定的朴素 UTC 时间；解析不了就当没有。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def upgrade() -> None:
    table = op.create_table(
        "login_device",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False, server_default="full"),
        sa.Column("installation_id", sa.String(), nullable=True),
        sa.Column("client_version", sa.String(), nullable=True),
        sa.Column("platform", sa.String(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_ip", sa.String(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("login_device", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_login_device_member_id"), ["member_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_login_device_kind"), ["kind"], unique=False)
        batch_op.create_index(batch_op.f("ix_login_device_token_hash"), ["token_hash"], unique=True)
        batch_op.create_index(
            batch_op.f("ix_login_device_installation_id"), ["installation_id"], unique=False
        )

    # 存量令牌：配置域里的 JSON 清单 → 表行
    connection = op.get_bind()
    row = connection.execute(
        sa.text("SELECT value_json FROM app_setting WHERE namespace = :ns"),
        {"ns": _LEGACY_NAMESPACE},
    ).fetchone()
    if row is None:
        return
    try:
        tokens = json.loads(row[0]).get("tokens") or []
    except (ValueError, AttributeError):
        tokens = []
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = []
    seen_hashes: set[str] = set()
    for token in tokens:
        if not isinstance(token, dict):
            continue
        token_hash = token.get("token_hash")
        if not isinstance(token_hash, str) or not token_hash or token_hash in seen_hashes:
            continue
        seen_hashes.add(token_hash)
        kind = _KINDS.get(str(token.get("client_type") or "manual"), "manual")
        created = _naive_utc(token.get("created_at")) or now
        rows.append(
            {
                "created_at": created,
                "updated_at": now,
                "member_id": 0,
                "kind": kind,
                "name": str(token.get("name") or "未命名设备")[:64],
                "token_hash": token_hash,
                "scope": "transcode" if kind == "worker" else "full",
                "last_seen_at": _naive_utc(token.get("last_used_at")),
            }
        )
    if rows:
        op.bulk_insert(table, rows)
    connection.execute(
        sa.text("DELETE FROM app_setting WHERE namespace = :ns"), {"ns": _LEGACY_NAMESPACE}
    )


def downgrade() -> None:
    with op.batch_alter_table("login_device", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_login_device_installation_id"))
        batch_op.drop_index(batch_op.f("ix_login_device_token_hash"))
        batch_op.drop_index(batch_op.f("ix_login_device_kind"))
        batch_op.drop_index(batch_op.f("ix_login_device_member_id"))
    op.drop_table("login_device")
