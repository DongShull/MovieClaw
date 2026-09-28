"""播放体验打点测试（docs/design/playback-qoe.md）。

守护的是「一次播放一条记录」的几条硬规则：
- 开始与收尾按播放编号合并，谁先到都行，服务端自己的块不被 App 的上报冲掉；
- 北极星、跳转分位、非自愿中断、可避免的规格损失只在服务端判定一处，口径与文档一致；
- 取流统计只在内存累计、收尾时一次写入；
- 没收尾的记录会被标出来、过期记录按时间清理；
- 统计样本不足时不编分位数，实验室的播放默认不算。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.playback import qoe
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import PlaybackMetric
from movieclaw_db.models.base import utcnow


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'q.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    qoe._serve.clear()
    yield get_database()
    qoe._serve.clear()
    await dispose_db()
    get_settings.cache_clear()


def report(attempt_id: str = "a1", **kwargs) -> qoe.FinishReport:
    base = dict(
        attempt_id=attempt_id,
        outcome="watched",
        tier=0,
        client="ios",
        route="loopback",
        first_frame_ms=800,
        playing_ms=900,
        watched_ms=600_000,
    )
    base.update(kwargs)
    return qoe.FinishReport(**base)


async def finish(db, rep: qoe.FinishReport) -> PlaybackMetric:
    async with db.session() as session:
        return await qoe.finish_attempt(session, member_id=0, report=rep)


async def begin(db, attempt_id: str = "a1", *, tier: int = 0, server: dict | None = None) -> None:
    async with db.session() as session:
        await qoe.begin_attempt(
            session,
            attempt_id=attempt_id,
            member_id=0,
            file=None,
            tier=tier,
            client="ios",
            server=server or {"outcome": "plan", "tier": tier},
        )


# —— 开始与收尾 ——


async def test_begin_then_finish_merges_into_one_row(db):
    await begin(db, server={"outcome": "plan", "tier": 0, "timings_ms": {"decide": 12}})
    row = await finish(db, report())
    async with db.session() as session:
        rows = (await session.execute(PlaybackMetric.__table__.select())).all()
    assert len(rows) == 1
    assert row.status == qoe.STATUS_FINISHED
    assert row.metric_version == qoe.METRIC_VERSION
    # 服务端在会话接口记的块不被 App 的上报冲掉
    assert row.detail["server"]["sessions"][0]["timings_ms"] == {"decide": 12}


async def test_reconnect_sessions_append_to_the_same_attempt(db):
    """断线重连、换画质会再开会话：同一个编号只追加会话摘要、更新档位。"""
    await begin(db, tier=0)
    await begin(db, tier=3, server={"outcome": "plan", "tier": 3})
    async with db.session() as session:
        row = await qoe.get_attempt(session, "a1")
    assert row.tier == 3
    assert [s["tier"] for s in row.detail["server"]["sessions"]] == [0, 3]


async def test_finish_without_begin_still_records(db):
    """协商阶段就失败、根本没开成会话的播放也要有一行——最糟的那批不能漏。"""
    row = await finish(db, report(outcome="failed", tier=-1, first_frame_ms=None, playing_ms=None,
                                  error_kind="sourceOpenFailed", error_category="network",
                                  error_stage="negotiation"))
    assert row.status == qoe.STATUS_FINISHED
    assert row.tier == -1
    assert row.undisturbed is False
    assert row.detail["judgement"]["reasons"] == ["failed"]


async def test_abnormal_exit_is_marked(db):
    row = await finish(db, report(outcome="abnormal_exit"))
    assert row.status == qoe.STATUS_ABNORMAL
    assert "abnormal_exit" in row.detail["judgement"]["reasons"]


async def test_unknown_outcome_and_oversized_lists_are_trimmed_not_rejected(db):
    seeks = [{"ms": 100, "buffered": True, "outcome": "landed"}] * 80
    row = await finish(db, report(outcome="weird", detail={"seeks": seeks}))
    assert row.outcome == "exited"
    assert len(row.detail["seeks"]) == 50
    assert row.seek_in_count == 50


# —— 判定 ——


async def test_undisturbed_play(db):
    row = await finish(db, report(detail={
        "seeks": [
            {"ms": 250, "buffered": True, "outcome": "landed"},
            {"ms": 900, "buffered": False, "outcome": "landed"},
            {"ms": 200, "buffered": False, "outcome": "superseded"},
        ],
        "interruptions": [{"kind": "rebuffer", "ms": 300}, {"kind": "reconnect", "ms": 4000}],
    }))
    assert row.undisturbed is True
    assert (row.seek_in_count, row.seek_in_max_ms) == (1, 250)
    # 很快被取代的跳转（拖动中）只计数，不算进分位
    assert (row.seek_out_count, row.seek_out_max_ms) == (1, 900)
    # 0.3 秒的卡顿、撑住了的重连都不是用户看得见的中断
    assert row.interrupt_count == 0
    assert (row.reconnect_count, row.reconnect_ms) == (1, 4000)


@pytest.mark.parametrize(
    ("fields", "detail", "reason"),
    [
        ({"first_frame_ms": 2100}, {}, "startup_slow"),
        ({}, {"seeks": [{"ms": 1600, "buffered": False, "outcome": "landed"}]}, "seek_slow"),
        ({}, {"interruptions": [{"kind": "rebuffer", "ms": 600}]}, "interrupted"),
        ({}, {"interruptions": [{"kind": "freeze", "ms": 1200}]}, "interrupted"),
        ({}, {"interruptions": [{"kind": "error"}]}, "interrupted"),
        ({}, {"delivery": {"route": "server_transcode"}}, "avoidable_loss"),
        ({}, {"behaviors": [{"kind": "subtitle_change", "misguess": True}]}, "misguess"),
        ({"outcome": "exit_before_start", "first_frame_ms": None}, {}, "no_picture"),
    ],
)
async def test_each_disturbance_breaks_the_north_star(db, fields, detail, reason):
    row = await finish(db, report(**fields, detail=detail))
    assert row.undisturbed is False
    assert reason in row.detail["judgement"]["reasons"]


async def test_a_long_wait_counts_even_if_the_seek_never_landed(db):
    """等了 10 秒还没出画、用户放弃又跳了一次：只看落地的跳转会漏掉最糟的这一次。"""
    row = await finish(db, report(detail={"seeks": [
        {"ms": 10_554, "buffered": False, "outcome": "superseded"},
        {"ms": 300, "buffered": False, "outcome": "landed"},
    ]}))
    assert row.seek_out_max_ms == 300
    assert "seek_slow" in row.detail["judgement"]["reasons"]
    # 拖进度条时一连串很快被取代的跳转不算
    quick = await finish(db, report("a2", detail={"seeks": [
        {"ms": 120, "buffered": True, "outcome": "superseded"},
        {"ms": 200, "buffered": True, "outcome": "landed"},
    ]}))
    assert quick.undisturbed is True


def test_loss_rules_measure_against_what_the_device_can_present():
    """可避免的规格损失：以当前设备 + 当前音频输出能呈现的最好效果为准。"""
    lossless_to_lossy = {"audio": {"source_lossless": True, "output_codec": "eac3"}}
    # 蓝牙本身是有损传输，无损转有损听不出来，不算
    assert qoe.avoidable_losses({**lossless_to_lossy, "output": {"audio_route": "bluetooth"}}) == []
    assert qoe.avoidable_losses({**lossless_to_lossy, "output": {"audio_route": "hdmi"}}) == [
        "lossless_lost"
    ]
    # TrueHD 全景声在 iOS 上没有透传路径，算设备上限；E-AC-3 全景声原样送得出去
    truehd_atmos = {"audio": {"source_atmos": True, "atmos_kept": False, "source_codec": "truehd"}}
    assert qoe.avoidable_losses(truehd_atmos) == []
    eac3_atmos = {"audio": {"source_atmos": True, "atmos_kept": False, "source_codec": "eac3"}}
    assert qoe.avoidable_losses(eac3_atmos) == ["atmos_lost"]
    # 用户主动限画质不算损失
    assert qoe.avoidable_losses({"route": "server_transcode", "user_capped": True}) == []
    # 屏幕支持 HDR 时丢了 HDR / 杜比视界才算
    hdr_lost = {"video": {"source_format": "hdr10", "output_format": "sdr"}}
    assert qoe.avoidable_losses({**hdr_lost, "output": {"display_hdr": False}}) == []
    assert qoe.avoidable_losses({**hdr_lost, "output": {"display_hdr": True}}) == ["hdr_lost"]
    assert qoe.avoidable_losses(None) is None


def test_source_class_buckets():
    from movieclaw_db.models import LibraryFile

    def cls(**kw):
        return qoe.source_class(LibraryFile(library_id=1, file_path="/x", **kw))

    assert cls(container="bluray", resolution="2160p") == "uhd_bluray"
    assert cls(container="iso", resolution="576p") == "dvd"
    assert cls(container="dvd") == "dvd"
    assert cls(container="avi", resolution="720p") == "legacy"
    assert cls(container="mkv", resolution="2160p", hdr="Dolby Vision") == "dolby_vision"
    assert cls(container="mp4", resolution="2160p") == "4k"
    assert cls(container="mp4", resolution="1080p") == "1080p"
    assert qoe.source_class(None) == "unknown"


# —— 取流统计 ——


async def test_serve_tally_is_written_once_at_finish(db):
    probe = qoe.serve_probe("a1")
    probe.first_chunk()
    probe.finish(8 * 1024 * 1024, disconnected=False)
    slow = qoe.serve_probe("a1")
    slow._started -= 2.0  # 模拟首字节等了 2 秒
    slow.first_chunk()
    slow.finish(1024, disconnected=True)
    assert qoe.serve_probe(None) is None

    row = await finish(db, report())
    serve = row.detail["server"]["serve"]
    assert serve["requests"] == 2
    assert serve["bytes"] == 8 * 1024 * 1024 + 1024
    assert serve["disconnects"] == 1
    assert serve["first_byte_max_ms"] >= 2000
    assert len(serve["slow"]) == 1
    # 取走后内存里不留
    assert qoe.take_serve_stats("a1") is None


def test_transcode_summary_joins_the_attempt():
    qoe._serve.clear()
    qoe.note_transcode_session("a2", {"session_id": "s1", "timeouts": 1})
    qoe.note_transcode_session(None, {"session_id": "s2"})
    stats = qoe.take_serve_stats("a2")
    assert stats["transcode"] == [{"session_id": "s1", "timeouts": 1}]


def test_idle_serve_tallies_are_dropped():
    qoe._serve.clear()
    qoe.serve_probe("old").finish(1, disconnected=False)
    assert qoe.sweep_serve_stats(now=qoe._serve["old"].last_seen + 10) == 0
    assert qoe.sweep_serve_stats(now=qoe._serve["old"].last_seen + 7200) == 1
    assert "old" not in qoe._serve


# —— 清扫与清理 ——


async def test_stale_started_rows_become_unreported_and_can_still_finish(db):
    await begin(db, "old")
    await begin(db, "fresh")
    async with db.session() as session:
        row = await qoe.get_attempt(session, "old")
        row.created_at = utcnow() - timedelta(hours=7)
        await session.commit()
        assert await qoe.sweep_unreported(session) == 1
        assert (await qoe.get_attempt(session, "fresh")).status == qoe.STATUS_STARTED
    # App 下次启动补报，照样合并回来
    row = await finish(db, report("old"))
    assert row.status == qoe.STATUS_FINISHED


async def test_purge_is_by_age(db):
    await finish(db, report("keep"))
    await finish(db, report("drop"))
    async with db.session() as session:
        row = await qoe.get_attempt(session, "drop")
        row.created_at = utcnow() - timedelta(days=100)
        await session.commit()
        assert await qoe.purge_expired(session, days=90) == 1
        assert await qoe.get_attempt(session, "keep") is not None


# —— 统计 ——


async def test_small_samples_list_rows_instead_of_percentiles(db):
    await finish(db, report("a"))
    await finish(db, report("b", first_frame_ms=3000))
    async with db.session() as session:
        stats = await qoe.qoe_stats(session, days=7, group_by=None, include_lab=False)
    overall = stats["overall"]
    assert overall["small_sample"] is True
    assert overall["first_frame_ms"] is None
    assert overall["undisturbed_rate"] is None
    assert {s["attempt_id"] for s in overall["samples"]} == {"a", "b"}
    assert [w["attempt_id"] for w in stats["worst"]] == ["b"]
    assert stats["reasons"] == [{"reason": "startup_slow", "label": "起播慢", "count": 1}]


async def test_stats_percentiles_groups_and_lab_exclusion(db):
    for index in range(40):
        await finish(db, report(
            f"x{index}",
            first_frame_ms=500 + index * 10,
            network_class="home" if index % 2 else "away",
            detail={"seeks": [{"ms": 100 + index, "buffered": True, "outcome": "landed"}]},
        ))
    await finish(db, report("lab", lab_scenario="devlab", first_frame_ms=9000))
    async with db.session() as session:
        stats = await qoe.qoe_stats(session, days=7, group_by="network_class", include_lab=False)
        with_lab = await qoe.qoe_stats(session, days=7, group_by=None, include_lab=True)
    overall = stats["overall"]
    assert overall["attempts"] == 40
    assert overall["undisturbed_rate"] == 1.0
    assert overall["first_frame_ms"]["p50"] == 700
    assert overall["first_frame_ms"]["p90"] == 850
    assert overall["seek_in_buffer_ms"]["count"] == 40
    assert {g["key"] for g in stats["groups"]} == {"home", "away"}
    assert {g["label"] for g in stats["groups"]} == {"在家", "在外"}
    assert with_lab["overall"]["attempts"] == 41
