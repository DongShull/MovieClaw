"""播放进度上报（/Sessions/Playing*）的回归测试：多版本片长、坏位置值、库可见性。"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_jellyfin.ids import episode_guid, item_guid, media_source_guid

from .helpers import jf_login

TICKS_PER_MINUTE = 60 * 1000 * 10_000


def _db() -> sqlite3.Connection:
    prefix = "sqlite+aiosqlite:///"
    database_url = get_settings().database_url
    assert database_url.startswith(prefix)
    return sqlite3.connect(database_url.removeprefix(prefix))


def _state(media_item_id: int, season: int = 0, episode: int = 0) -> tuple[int, int] | None:
    """(position_ms, played)；没有行返回 None。"""
    with _db() as conn:
        return conn.execute(
            "SELECT position_ms, played FROM playback_state "
            "WHERE media_item_id = ? AND season_number = ? AND episode_number = ?",
            (media_item_id, season, episode),
        ).fetchone()


def _long_version(movie_id: int) -> int:
    """把电影的第二个版本（strm）改成 200 分钟的加长版，返回它的文件 id。
    第一个版本（本地 2160p）是 148 分钟。"""
    with _db() as conn:
        file_id = conn.execute(
            "SELECT id FROM library_file WHERE media_item_id = ? ORDER BY id DESC LIMIT 1",
            (movie_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE library_file SET duration_seconds = ? WHERE id = ?", (200 * 60, file_id)
        )
        conn.commit()
    return file_id


def test_progress_runtime_follows_played_version(client: TestClient, seeded: dict) -> None:
    """加长版（200 分钟）看到 150 分钟是 75%，要留续播点；拿 148 分钟的
    影院版当分母会算成 >90%，标已看并清掉进度。不带轨序号也得按
    MediaSourceId 认出正在放的版本。"""
    auth = {"ApiKey": jf_login(client)}
    movie = seeded["movie"]
    long_id = _long_version(movie)
    body = {
        "ItemId": item_guid(movie),
        "MediaSourceId": media_source_guid(long_id),
        "PositionTicks": 150 * TICKS_PER_MINUTE,
    }
    assert client.post("/Sessions/Playing/Progress", params=auth, json=body).status_code == 204
    assert _state(movie) == (150 * 60_000, 0)

    assert client.post("/Sessions/Playing/Stopped", params=auth, json=body).status_code == 204
    assert _state(movie) == (150 * 60_000, 0)


def test_stopped_unparsable_position_keeps_resume_point(
    client: TestClient, seeded: dict
) -> None:
    """Stopped 的 PositionTicks 解析不了时不能当「没报位置 = 播到结尾」标已看。"""
    auth = {"ApiKey": jf_login(client)}
    show = seeded["show"]
    ep = episode_guid(show, 1, 1)
    client.post(
        "/Sessions/Playing/Progress",
        params=auth,
        json={"ItemId": ep, "PositionTicks": 20 * TICKS_PER_MINUTE},
    )
    resp = client.post(
        "/Sessions/Playing/Stopped",
        params=auth,
        json={"ItemId": ep, "PositionTicks": "not-a-number"},
    )
    assert resp.status_code == 204
    assert _state(show, 1, 1) == (20 * 60_000, 0)

    # 浮点写法的字符串能解析：照常落库
    client.post(
        "/Sessions/Playing/Stopped",
        params=auth,
        json={"ItemId": ep, "PositionTicks": f"{25 * TICKS_PER_MINUTE:.1f}"},
    )
    assert _state(show, 1, 1) == (25 * 60_000, 0)


def test_reports_on_invisible_library_are_dropped(client: TestClient, seeded: dict) -> None:
    """白名单外库里的条目：GUID 可枚举，上报不得写出观看状态。"""
    auth = {"ApiKey": jf_login(client)}
    movie = seeded["movie"]
    with _db() as conn:
        conn.execute(
            "UPDATE library SET access_mode = 'selected', admin_visible = 0 WHERE id = ?",
            (seeded["movie_lib"],),
        )
        conn.commit()
    guid = item_guid(movie)
    client.post("/Sessions/Playing", params=auth, json={"ItemId": guid})
    client.post(
        "/Sessions/Playing/Progress",
        params=auth,
        json={"ItemId": guid, "PositionTicks": 60 * TICKS_PER_MINUTE},
    )
    client.post("/Sessions/Playing/Stopped", params=auth, json={"ItemId": guid})
    client.post(f"/PlayingItems/{guid}", params=auth)
    client.delete(f"/PlayingItems/{guid}", params=auth)
    assert _state(movie) is None
