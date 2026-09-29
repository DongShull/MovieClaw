"""「生成章节」改为默认关闭的迁移回归测试（docs/design/video-chapters.md §4.5）。"""

from __future__ import annotations

import sqlite3

from alembic import command

from movieclaw_api.core.config import get_settings
from movieclaw_db.migrations import _build_config

_BEFORE = "c4d9e2a7b613"  # 本迁移的前一版
_REVISION = "0641cc32b069"


def test_existing_libraries_are_switched_off(tmp_path, monkeypatch) -> None:
    """升级前的库几乎都是被旧默认值（开）打开的，无从区分谁是主动开的：一律置为关，
    想要章节的库在编辑库里重新打开。之后新建的库也不带这个开关。"""
    database = tmp_path / "chapters-default-off.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    get_settings.cache_clear()
    config = _build_config()
    command.upgrade(config, _BEFORE)

    with sqlite3.connect(database) as connection:
        for name, enabled in (("旧默认开", None), ("手动关的", 0)):
            columns = "created_at, updated_at, name, kind, root_paths, is_default"
            values = "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, 'movie', '[]', 0"
            params: tuple = (name,)
            if enabled is not None:
                columns += ", extract_chapter_images"
                values += ", ?"
                params += (enabled,)
            connection.execute(f"INSERT INTO library ({columns}) VALUES ({values})", params)
        connection.commit()
        before = dict(connection.execute("SELECT name, extract_chapter_images FROM library"))
    assert before == {"旧默认开": 1, "手动关的": 0}

    command.upgrade(config, _REVISION)

    with sqlite3.connect(database) as connection:
        after = dict(connection.execute("SELECT name, extract_chapter_images FROM library"))
    assert after == {"旧默认开": 0, "手动关的": 0}
    get_settings.cache_clear()
