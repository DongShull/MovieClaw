"""Agent 自动化的循环单测（docs/design/agent-automations.md 六循环验收）。

- L1 到期计算与补跑合并（compute_next_run 表驱动用例）；
- L2 闸门顺序：kill switch / 静默时段 / 日预算 / 单飞；
- L5 观测：失败退避 1h→6h→24h、连续失败 ≥3 熔断 + 待处理事项、成功清零；
- L4 投递：NO-OP 不投、delivered_at 幂等；
- L3 令牌：automation 标记的签发与校验、mclaw 档位工具面。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

import movieclaw_api.services.agent_automations as auto
from movieclaw_agent import AgentStartParams
from movieclaw_agent.events import AgentEvent
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.services.agent_automations import (
    MAX_CONSECUTIVE_FAILURES,
    compute_next_run,
    is_no_op,
    launch,
    quiet_hours_end,
    run_agent_automations,
    validate_quiet_hours,
    validate_trigger,
)
from movieclaw_api.settings import (
    AgentAutomationsSetting,
    get_setting_store,
    init_setting_store,
    reset_setting_store,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.models import SystemNotice
from movieclaw_db.models.agent_automation import (
    AgentAutomation,
    AgentAutomationRun,
    AutomationRunStatus,
    AutomationTriggerType,
)
from movieclaw_db.models.base import utcnow
from movieclaw_db.repositories.agent_automation_repo import AgentAutomationRepository
from movieclaw_llm import ChatMessage, TokenUsage
from movieclaw_scheduler.config import SchedulerConfig, set_scheduler_config


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'loop.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("AGENT_SESSIONS_DIR", str(tmp_path / "agent-sessions"))
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(tmp_path / "agent-workspace"))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    from movieclaw_api.core.config import get_settings
    from movieclaw_api.services.agent_sessions import reset_agent_session_store
    from movieclaw_db.migrations import run_migrations

    get_settings.cache_clear()
    set_scheduler_config(SchedulerConfig(timezone="Asia/Shanghai"))
    init_db(get_settings().database_url, echo=False)
    reset_setting_store()
    reset_agent_session_store()
    auto._active_runs.clear()
    auto._active_registry_runs.clear()
    auto._token_reservations.clear()
    auto._launch_locks.clear()
    # 令牌签发/解密要会话密钥：应用 lifespan 在服务级单测里不跑，这里手动初始化
    from movieclaw_db.crypto import init_secret_box, reset_secret_box

    reset_secret_box()
    init_secret_box("test-master-key", tmp_path / ".secret_key")
    await run_migrations()
    init_setting_store()
    # 执行体经 AgentRunRegistry 跑（跟随/停止/取消统一走它）；服务级单测手动初始化
    from movieclaw_api.services.agent_runs import (
        close_agent_run_registry,
        init_agent_run_registry,
    )

    init_agent_run_registry()
    yield get_database()
    await close_agent_run_registry()
    await dispose_db()
    reset_secret_box()
    reset_setting_store()
    get_settings.cache_clear()
    reset_agent_session_store()
    auto._active_runs.clear()
    auto._active_registry_runs.clear()
    auto._token_reservations.clear()
    auto._launch_locks.clear()


async def _seed_automation(**overrides) -> AgentAutomation:
    row = AgentAutomation(
        name="每分钟点个名",
        prompt="检查待识别队列，无事项输出 NO-OP。",
        trigger_type=AutomationTriggerType.INTERVAL,
        trigger_expr="3600",
        next_run_at=utcnow() - timedelta(seconds=1),
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    async with get_database().session() as session:
        created = await AgentAutomationRepository(session).create(row)
        session.expunge(created)
        return created


async def _reload(automation_id: int) -> AgentAutomation | None:
    async with get_database().session() as session:
        row = await AgentAutomationRepository(session).get(automation_id)
        if row is not None:
            session.expunge(row)
        return row


def _noop_run(automation_id: int, run_id: int) -> None:  # pragma: no cover - 占位执行体
    return None


# ---------------------------------------------------------------------------
# L1 · 到期计算（表驱动）
# ---------------------------------------------------------------------------
def test_interval_next_run_anchors_on_last_run() -> None:
    """正常节拍：以最近一轮为锚点顺延（run-now 提前后不重置节奏）。"""
    now = datetime(2026, 10, 30, 12, 0, 0)
    last = now - timedelta(minutes=10)
    nxt = compute_next_run("interval", "3600", now=now, last_run_at=last)
    assert nxt == last + timedelta(hours=1)


def test_interval_catchup_merges_missed_expiries() -> None:
    """停机 5 小时只补跑一次：从「现在」重新起拍，不追历史（L1 验收②）。"""
    now = datetime(2026, 10, 30, 12, 0, 0)
    last = now - timedelta(hours=5)
    nxt = compute_next_run("interval", "3600", now=now, last_run_at=last)
    assert nxt == now + timedelta(hours=1)


def test_cron_next_run_in_scheduler_timezone() -> None:
    """cron「0 8 * * *」取下一个上海 08:00，落库仍是 UTC naive。"""
    now = datetime(2026, 10, 30, 23, 30, 0)  # = 上海 10-31 07:30
    nxt = compute_next_run("cron", "0 8 * * *", now=now)
    assert abs((nxt - datetime(2026, 10, 31, 0, 0, 0)).total_seconds()) < 1


def test_validate_trigger_rejects_bad_cron_and_short_interval() -> None:
    with pytest.raises(BadRequestException):
        validate_trigger("cron", "99 99 * * *")
    with pytest.raises(BadRequestException):
        validate_trigger("interval", "5")  # 低于 60s 节拍下限
    with pytest.raises(BadRequestException):
        validate_trigger("interval", "abc")
    validate_trigger("cron", "*/5 * * * *")  # 合法不抛


# ---------------------------------------------------------------------------
# L2 · 静默时段（表驱动）
# ---------------------------------------------------------------------------
def test_quiet_hours_outside_window_returns_none() -> None:
    now = datetime(2026, 10, 30, 23, 30, 0)  # = 上海 07:30，已出 23:00-07:00
    assert quiet_hours_end("23:00-07:00", now) is None


def test_quiet_hours_evening_until_next_morning() -> None:
    later = datetime(2026, 10, 30, 15, 30, 0)  # = 上海 23:30，静默中
    end = quiet_hours_end("23:00-07:00", later)
    # 结束于上海 10-31 07:00 = UTC 10-30 23:00
    assert end == datetime(2026, 10, 30, 23, 0, 0)


def test_quiet_hours_after_midnight_ends_same_morning() -> None:
    utc_2am = datetime(2026, 10, 30, 18, 0, 0)  # = 上海 10-31 02:00，跨午夜静默中
    end = quiet_hours_end("23:00-07:00", utc_2am)
    # 结束于上海 10-31 07:00 = UTC 10-30 23:00（不再加一天）
    assert end == datetime(2026, 10, 30, 23, 0, 0)


def test_quiet_hours_none_and_invalid() -> None:
    some_utc = datetime(2026, 10, 30, 12, 0, 0)
    assert quiet_hours_end(None, some_utc) is None
    assert quiet_hours_end("not-a-range", some_utc) is None
    with pytest.raises(BadRequestException):
        validate_quiet_hours("08:00-08:00")  # 起止相同无意义
    validate_quiet_hours("07:00-23:00")  # 合法不抛


# ---------------------------------------------------------------------------
# L4 · NO-OP 判定
# ---------------------------------------------------------------------------
def test_is_no_op_variants() -> None:
    assert is_no_op(None)
    assert is_no_op("")
    assert is_no_op("   ")
    assert is_no_op("NO-OP")
    assert is_no_op("**no-op**")
    assert is_no_op("无事项")
    assert not is_no_op("今天入库了 3 部影片")
    assert not is_no_op("no-op，但是有一件事")  # 附加内容不算 NO-OP


# ---------------------------------------------------------------------------
# L2 · 闸门（launch 路径）
# ---------------------------------------------------------------------------
async def test_launch_skips_when_kill_switch_off(db, monkeypatch) -> None:
    monkeypatch.setattr(auto, "_execute_run", _noop_run)
    row = await _seed_automation()
    async with get_database().session() as session:
        fresh = await AgentAutomationRepository(session).get(row.id)
        run = await launch(session, fresh, now=utcnow())
    assert run.status == AutomationRunStatus.SKIPPED
    assert run.skip_reason == "disabled"


async def test_launch_single_flight_records_busy(db, monkeypatch) -> None:
    monkeypatch.setattr(auto, "_execute_run", _noop_run)
    row = await _seed_automation()
    await get_setting_store().set(AgentAutomationsSetting(enabled=True))

    # 在途登记是 run 行 id（不是 task）：执行体的收尾按「仍属于自己」清理
    auto._active_runs[row.id] = 999
    async with get_database().session() as session:
        fresh = await AgentAutomationRepository(session).get(row.id)
        run = await launch(session, fresh, now=utcnow())
    assert run.status == AutomationRunStatus.SKIPPED
    assert run.skip_reason == "busy"
    # 在途登记没有被这一轮 skip 覆盖
    assert auto._active_runs[row.id] == 999


async def test_concurrent_launch_single_flight(db, monkeypatch) -> None:
    """两个并发 launch（run-now 撞节拍）：只有一轮真正起来（审查意见 2）。"""
    release = asyncio.Event()

    async def _hanging_run(automation_id: int, run_id: int) -> None:
        await release.wait()

    monkeypatch.setattr(auto, "_execute_run", _hanging_run)
    row = await _seed_automation()
    await get_setting_store().set(AgentAutomationsSetting(enabled=True))

    async with get_database().session() as session:
        fresh = await AgentAutomationRepository(session).get(row.id)
        results = await asyncio.gather(
            launch(session, fresh, now=utcnow()),
            launch(session, fresh, now=utcnow()),
        )
    statuses = sorted(r.status.value for r in results)
    assert statuses == ["running", "skipped"]
    busy = next(r for r in results if r.status == AutomationRunStatus.SKIPPED)
    assert busy.skip_reason == "busy"
    release.set()


async def test_launch_daily_run_limit_defers_to_next_day(db, monkeypatch) -> None:
    monkeypatch.setattr(auto, "_execute_run", _noop_run)
    row = await _seed_automation(max_runs_per_day=1)
    await get_setting_store().set(AgentAutomationsSetting(enabled=True))
    # 今天已经跑过一轮（skipped 不占日预算，这里落一条 completed）
    async with get_database().session() as session:
        await AgentAutomationRepository(session).save_run(
            AgentAutomationRun(
                automation_id=row.id,
                status=AutomationRunStatus.COMPLETED,
                summary="早上那轮",
            )
        )
    now = utcnow()
    async with get_database().session() as session:
        fresh = await AgentAutomationRepository(session).get(row.id)
        run = await launch(session, fresh, now=now)
    assert run.status == AutomationRunStatus.SKIPPED
    assert run.skip_reason == "daily_run_limit"
    # 顺延到调度器时区的下一个自然日（精确断言：本地次日开始，换算回 UTC naive）
    reloaded = await _reload(row.id)
    assert reloaded.next_run_at == auto._next_day_start(now)


async def test_launch_global_token_budget_skips(db, monkeypatch) -> None:
    monkeypatch.setattr(auto, "_execute_run", _noop_run)
    row = await _seed_automation()
    await get_setting_store().set(
        AgentAutomationsSetting(enabled=True, daily_token_budget=1000)
    )
    async with get_database().session() as session:
        await AgentAutomationRepository(session).save_run(
            AgentAutomationRun(
                automation_id=row.id,
                status=AutomationRunStatus.COMPLETED,
                tokens_used=1500,
            )
        )
    async with get_database().session() as session:
        fresh = await AgentAutomationRepository(session).get(row.id)
        run = await launch(session, fresh, now=utcnow())
    assert run.status == AutomationRunStatus.SKIPPED
    assert run.skip_reason == "daily_token_budget"


async def test_launch_happy_path_spawns_run(db, monkeypatch) -> None:
    started = asyncio.Event()

    async def _fake_run(automation_id: int, run_id: int) -> None:
        started.set()

    monkeypatch.setattr(auto, "_execute_run", _fake_run)
    row = await _seed_automation()
    await get_setting_store().set(AgentAutomationsSetting(enabled=True))
    now = utcnow()
    async with get_database().session() as session:
        fresh = await AgentAutomationRepository(session).get(row.id)
        run = await launch(session, fresh, now=now)
    assert run.status == AutomationRunStatus.RUNNING
    assert run.session_id is None  # 稳定会话在执行体里首次创建
    reloaded = await _reload(row.id)
    assert reloaded.last_run_at is not None
    assert reloaded.next_run_at > now
    await asyncio.wait_for(started.wait(), timeout=2)


# ---------------------------------------------------------------------------
# L1 · 节拍任务本体
# ---------------------------------------------------------------------------
async def test_tick_cancels_in_flight_when_disabled(db) -> None:
    """kill switch 关闭后 ≤1 tick：在途轮经运行注册表收到取消（L2 验收①）。"""
    from movieclaw_api.services.agent_runs import get_agent_run_registry

    class _HangingRunner:
        async def start(self, params, *, run_id=None):
            yield AgentEvent(type="agent_start", run_id=run_id or "t")
            await asyncio.sleep(30)

    registry = get_agent_run_registry()
    rid = registry.start(
        _HangingRunner(), AgentStartParams(input="x"), session_id="s-kill", on_terminal=None
    )
    auto._active_runs[42] = 1
    auto._active_registry_runs[1] = rid

    await run_agent_automations()  # 默认 policy.enabled=False → cancel_active_runs

    for _ in range(50):
        if not registry.is_active(rid):
            break
        await asyncio.sleep(0.05)
    assert not registry.is_active(rid)
    events, _ = await registry.get_events(rid, 0, timeout_seconds=0)
    assert any(e.event.type == "agent_cancelled" for e in events)


async def test_tick_launches_due_automations(db, monkeypatch) -> None:
    """节拍任务本体：启用的到期自动化被拉起（L1 验收①的任务侧）。"""
    release = asyncio.Event()

    async def _hanging_run(automation_id: int, run_id: int) -> None:
        await release.wait()

    monkeypatch.setattr(auto, "_execute_run", _hanging_run)
    row = await _seed_automation()  # next_run_at 已在过去
    await get_setting_store().set(AgentAutomationsSetting(enabled=True))

    await run_agent_automations()

    async with get_database().session() as session:
        repo = AgentAutomationRepository(session)
        runs, total = await repo.list_runs(row.id)
    assert total == 1
    assert runs[0].status == AutomationRunStatus.RUNNING
    release.set()


# ---------------------------------------------------------------------------
# L5 · 失败退避与熔断（表驱动）
# ---------------------------------------------------------------------------
async def _finalize(row_id: int, run_id: int, **kwargs) -> None:
    await auto._finalize_run(
        automation_id=row_id,
        run_id=run_id,
        status=kwargs.pop("status", AutomationRunStatus.FAILED),
        summary=kwargs.pop("summary", None),
        tokens=kwargs.pop("tokens", 0),
        error_text=kwargs.pop("error_text", "boom"),
        count_failure=kwargs.pop("count_failure", True),
        duration_ms=kwargs.pop("duration_ms", 10),
    )


async def test_backoff_table_one_hour_six_hours_day(db) -> None:
    """连续失败退避 1h → 6h → 24h（L5 验收②，表驱动），第 3 次熔断停用。"""
    row = await _seed_automation()
    async with get_database().session() as session:
        run = AgentAutomationRun(automation_id=row.id, status=AutomationRunStatus.RUNNING)
        await AgentAutomationRepository(session).save_run(run)
        run_id = run.id

    for i, hours in enumerate((1, 6, 24), start=1):
        before = utcnow()
        await _finalize(row.id, run_id)
        reloaded = await _reload(row.id)
        gap = (reloaded.next_run_at - before).total_seconds()
        assert hours * 3600 - 5 <= gap <= hours * 3600 + 5, f"第 {i} 次失败退避应为 {hours}h"

    # 第 3 次失败：自动停用 + 待处理事项（L5 验收①）
    reloaded = await _reload(row.id)
    assert reloaded.enabled is False
    assert reloaded.consecutive_failures == MAX_CONSECUTIVE_FAILURES
    async with get_database().session() as session:
        notice = (
            await session.execute(
                select(SystemNotice).where(
                    SystemNotice.dedupe_key == f"agent.automation:{row.id}"
                )
            )
        ).scalar_one_or_none()
    assert notice is not None
    assert notice.severity == "error"
    assert "点个名" in notice.title  # 文案带上自动化名，人不用猜是哪条


async def test_success_resets_failure_count(db, monkeypatch) -> None:
    async def _fake_run(automation_id: int, run_id: int) -> None:
        pass

    monkeypatch.setattr(auto, "_execute_run", _fake_run)
    row = await _seed_automation()
    await get_setting_store().set(AgentAutomationsSetting(enabled=True))
    async with get_database().session() as session:
        fresh = await AgentAutomationRepository(session).get(row.id)
        run = await launch(session, fresh, now=utcnow())
    await _finalize(row.id, run.id)
    reloaded = await _reload(row.id)
    assert reloaded.consecutive_failures == 1

    # 再来一轮成功的：失败计数清零、last_error 清空
    async with get_database().session() as session:
        fresh = await AgentAutomationRepository(session).get(row.id)
        fresh.next_run_at = utcnow() - timedelta(seconds=1)
        await AgentAutomationRepository(session).save(fresh)
        run2 = await launch(session, fresh, now=utcnow())
    await _finalize(
        row.id,
        run2.id,
        status=AutomationRunStatus.COMPLETED,
        summary="一切正常",
        tokens=1200,
    )
    reloaded = await _reload(row.id)
    assert reloaded.consecutive_failures == 0
    assert reloaded.last_error is None


# ---------------------------------------------------------------------------
# L4 · 投递幂等与 NO-OP 抑制
# ---------------------------------------------------------------------------
async def test_delivery_idempotent_and_noop_suppressed(db) -> None:
    row = await _seed_automation()
    async with get_database().session() as session:
        repo = AgentAutomationRepository(session)
        run = AgentAutomationRun(automation_id=row.id, status=AutomationRunStatus.RUNNING)
        await repo.save_run(run)
        run_id = run.id

    await _finalize(row.id, run_id, status=AutomationRunStatus.COMPLETED, summary="有结论")
    async with get_database().session() as session:
        delivered = await AgentAutomationRepository(session).get_run(run_id)
    assert delivered.delivered_at is not None

    # 同一 run 再走一遍收尾（重试路径）：已投递标记挡住，不重复投
    await _finalize(row.id, run_id, status=AutomationRunStatus.COMPLETED, summary="有结论")

    # NO-OP 轮：不投递
    async with get_database().session() as session:
        run2 = AgentAutomationRun(automation_id=row.id, status=AutomationRunStatus.RUNNING)
        await AgentAutomationRepository(session).save_run(run2)
        run2_id = run2.id
    await _finalize(row.id, run2_id, status=AutomationRunStatus.NO_OP)
    async with get_database().session() as session:
        noop_run = await AgentAutomationRepository(session).get_run(run2_id)
    assert noop_run.delivered_at is None
    assert noop_run.summary is None


# ---------------------------------------------------------------------------
# L3 · 令牌与工具面
# ---------------------------------------------------------------------------
async def test_agent_token_carries_automation_marker(db) -> None:
    from movieclaw_api.services.auth import issue_agent_token, verify_bearer_token

    token = await issue_agent_token("sess1", automation_id=9)
    principal = await verify_bearer_token(token)
    assert principal.kind == "agent"
    assert principal.automation_id == 9

    plain = await issue_agent_token("sess2")
    assert (await verify_bearer_token(plain)).automation_id is None


async def test_mclaw_profile_excludes_workspace_tools(db) -> None:
    """mclaw 档位工具面只有 mclaw（+搜索）：无 bash/read/write/edit（L3 验收②）。"""
    from movieclaw_api.services.agent_toolkit import profile_tools

    tools = await profile_tools("mclaw", "sess-x", "automation:1", automation_id=1)
    names = {t.name for t in tools}
    assert "mclaw" in names
    assert names.isdisjoint({"bash", "read", "write", "edit"})


# ---------------------------------------------------------------------------
# L3 · 熔断：墙钟超限优雅终止，内容照常落库（验收 §L3-4）
# ---------------------------------------------------------------------------
async def test_wall_clock_aborts_gracefully_and_keeps_content(db, monkeypatch) -> None:
    """墙钟到点：经注册表优雅取消，半截正文按 aborted 定稿保留（L3 验收④）。"""
    from movieclaw_agent.events import AgentDone
    from movieclaw_api.services.agent_sessions import get_agent_session_store

    class _HangingRunner:
        """挂住的 runner：只发 agent_start，然后永远挂着（直到被超时取消）。"""

        def __init__(self, router, tools=None, on_message=None, on_compaction=None, **_kw):
            self.on_message = on_message

        async def start(self, params, *, run_id=None):
            yield AgentEvent(type="agent_start", run_id=run_id or "t")
            # 半截正文先落盘（经 on_message 定稿回调），再挂住——验证超限后
            # 「已产生的内容照常保留」
            from movieclaw_llm import ChatResponse

            await self.on_message(
                ChatMessage(role="assistant", content="查到一半的结论"),
                ChatResponse(
                    content="查到一半的结论",
                    model="qwen3.7-max",
                    provider="test",
                    usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                ),
            )
            await asyncio.sleep(30)
            yield AgentEvent(
                type="agent_done", run_id=run_id or "t", result=AgentDone(text="never")
            )

    async def _fake_router(_session):
        return object()  # runner 是假的，路由器不会被真正调用

    monkeypatch.setattr(auto, "AgentRunner", _HangingRunner)
    monkeypatch.setattr(auto, "acquire_llm_router", _fake_router)
    monkeypatch.setattr(auto, "RUN_WALL_CLOCK_SECONDS", 0.2)

    row = await _seed_automation(prompt="查一查待识别队列")
    async with get_database().session() as session:
        repo = AgentAutomationRepository(session)
        run = AgentAutomationRun(automation_id=row.id, status=AutomationRunStatus.RUNNING)
        await repo.save_run(run)
        run_id = run.id

    await auto._execute_run(row.id, run_id)

    reloaded = await _reload(row.id)
    assert reloaded.session_id  # 稳定会话已建立
    async with get_database().session() as session:
        finished = await AgentAutomationRepository(session).get_run(run_id)
    assert finished.status == AutomationRunStatus.BUDGET_EXCEEDED
    assert finished.session_id == reloaded.session_id
    # 内容照常落库：转录里用户指令与半截 assistant 正文都在
    _, entries = get_agent_session_store().read(reloaded.session_id)
    roles = [e.message.role for e in entries]
    assert "user" in roles and "assistant" in roles
    texts = [e.message.text() for e in entries if e.message.role == "assistant"]
    assert any("查到一半" in t for t in texts)
    # budget_exceeded 计入失败：退避生效
    assert reloaded.consecutive_failures == 1


async def test_launch_quiet_hours_defers(db, monkeypatch) -> None:
    """静默时段在 launch 的共享闸门里：run-now 也顺延（审查意见 5）。"""
    monkey_now = utcnow()

    async def _noop_run(automation_id: int, run_id: int) -> None:  # pragma: no cover
        return None

    monkeypatch.setattr(auto, "_execute_run", _noop_run)
    await get_setting_store().set(AgentAutomationsSetting(enabled=True))  # 先过 kill switch
    # 以「现在」为中心造一个必然命中的静默窗（调度器时区），不依赖测试跑在几点
    from datetime import timedelta as _td
    from zoneinfo import ZoneInfo as _ZI

    local = monkey_now.replace(tzinfo=UTC).astimezone(_ZI("Asia/Shanghai"))
    start = (local - _td(minutes=1)).strftime("%H:%M")
    end = (local + _td(minutes=1)).strftime("%H:%M")
    row = await _seed_automation(quiet_hours=f"{start}-{end}")
    async with get_database().session() as session:
        fresh = await AgentAutomationRepository(session).get(row.id)
        run = await launch(session, fresh, now=monkey_now)
    assert run.status == AutomationRunStatus.SKIPPED
    assert run.skip_reason == "quiet"
    reloaded = await _reload(row.id)
    assert reloaded.next_run_at > monkey_now  # 顺延到时段结束，不是原地重试


async def test_global_token_budget_reserves_in_flight(db, monkeypatch) -> None:
    """日 token 预算给在途轮预留额度（审查意见 3）：预算 1000、单轮上限 30000
    时，第二轮不能因为「还没落账」就放行。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _hanging_run(automation_id: int, run_id: int) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(auto, "_execute_run", _hanging_run)
    row = await _seed_automation(max_tokens_per_run=30000)
    await get_setting_store().set(AgentAutomationsSetting(enabled=True, daily_token_budget=1000))

    async with get_database().session() as session:
        fresh = await AgentAutomationRepository(session).get(row.id)
        first = await launch(session, fresh, now=utcnow())
    assert first.status == AutomationRunStatus.RUNNING
    assert auto._token_reservations[first.id] == 30000  # 在途预留已登记
    await asyncio.wait_for(started.wait(), timeout=2)

    async with get_database().session() as session:
        fresh = await AgentAutomationRepository(session).get(row.id)
        second = await launch(session, fresh, now=utcnow())
    assert second.status == AutomationRunStatus.SKIPPED
    assert second.skip_reason == "daily_token_budget"
    release.set()


# ---------------------------------------------------------------------------
# L6 · 历史清理
# ---------------------------------------------------------------------------
async def test_cleanup_purges_old_runs(db) -> None:
    row = await _seed_automation()
    async with get_database().session() as session:
        repo = AgentAutomationRepository(session)
        await repo.save_run(
            AgentAutomationRun(
                automation_id=row.id,
                status=AutomationRunStatus.COMPLETED,
                started_at=utcnow() - timedelta(days=91),
            )
        )
        await repo.save_run(
            AgentAutomationRun(automation_id=row.id, status=AutomationRunStatus.COMPLETED)
        )
        deleted = await repo.purge_runs_older_than(utcnow() - timedelta(days=90))
    assert deleted == 1
