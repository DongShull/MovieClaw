"""Agent 自动化：定时到点自己跑一轮值守会话（docs/design/agent-automations.md）。

六个循环（Loop），一次自动化运行 = L1→L5 同步链，L6 是异步旁路：

    L1 Beat    scheduler 每 60s tick，挑 ``next_run_at <= now`` 且启用的行
    L2 Gate    kill switch → 静默时段 → 日预算 → 单飞（顺序不可换）
    L3 Run     以 origin=automation 跑一轮 Agent（仅 mclaw 工具 + token/墙钟上限）
    L4 Deliver 结论写回稳定会话 + 按渠道推送；NO-OP 不打扰；run_id 幂等
    L5 Observe run 落库；失败计数/退避/熔断（连续失败 ≥3 自动停用 + 待处理事项）
    L6 Maintain run 历史 90 天清理（异步旁路的定时任务）

全部状态都在 SQLite（automation / run 两表），tick 幂等：重启后错过的到期
合并为一次补跑，不追历史。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_agent import AgentRunner, AgentStartParams
from movieclaw_agent.events import AgentEvent
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.i18n.stored import source_text
from movieclaw_api.services.agent_session_recorder import AgentSessionRecorder
from movieclaw_api.services.agent_sessions import get_agent_session_store
from movieclaw_api.services.agent_toolkit import agent_system_prompt, profile_tools
from movieclaw_api.services.llm_config import acquire_llm_router
from movieclaw_api.services.push.notify import AlertContent, notify
from movieclaw_api.services.system_notice import upsert_notice
from movieclaw_api.settings import AgentAutomationsSetting, get_setting_store
from movieclaw_db.engine import get_database
from movieclaw_db.models import NoticeSeverity
from movieclaw_db.models.agent_automation import (
    AgentAutomation,
    AgentAutomationRun,
    AutomationRunStatus,
    AutomationTriggerType,
)
from movieclaw_db.models.base import utcnow
from movieclaw_db.repositories.agent_automation_repo import AgentAutomationRepository
from movieclaw_db.repositories.agent_session_repo import AgentSessionRepository, is_running
from movieclaw_kernel import Origin, current_origin
from movieclaw_llm import ChatMessage, ChatResponse, LlmError
from movieclaw_scheduler import TriggerType, register_task
from movieclaw_scheduler.config import get_scheduler_config

logger = logging.getLogger("movieclaw_api.agent_automations")

#: L1 节拍间隔（秒）
TICK_SECONDS = 60
#: 会话 owner 前缀：网页据此挂 automation 徽标（/sessions 列表）
AUTOMATION_OWNER_PREFIX = "automation:"
#: L2 单飞在途表：automation_id → 本轮任务（进程内；DB 侧以会话心跳兜底）
_active_runs: dict[int, asyncio.Task[None]] = {}
#: L3 熔断：单轮墙钟上限（秒）。token 上限在自动化行上（max_tokens_per_run）。
RUN_WALL_CLOCK_SECONDS = 10 * 60
#: L5 失败退避：连续失败第 1/2/≥3 次后的顺延（表驱动，验收 §L5-2）
FAILURE_BACKOFF = (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))
#: L5 熔断：连续失败达到该次数自动停用（验收 §L5-1）
MAX_CONSECUTIVE_FAILURES = 3
#: L6 run 历史保留天数
RUN_HISTORY_RETENTION_DAYS = 90
#: L4 推送正文的结论预览长度
DELIVERY_PREVIEW_CHARS = 200
#: 日预算类 skip 的顺延目标：调度器时区的下一个自然日开始
_SKIP_UNTIL_NEXT_DAY: str = "next_day"

# i18n-ignore: model-facing system prompt (design §8.4)
_AUTOMATION_SPEC = """\
你是被定时调起的值守会话（automation），没有人在屏幕前等你回答：
- 只处理本轮事实：以本条指令为任务书，用 mclaw 工具查证现状、执行动作；不要延续上一轮未说完的话。
- 自包含执行：不要反问、不要等待确认；拿不准的事项不要擅自动手，把它连同原因写进结论。
- 没有任何值得报告的事项时，正文只输出「NO-OP」四个字符，不要输出别的。
- 有事项时输出简明结论：做了什么、发现了什么、建议用户做什么。"""

_NO_OP_TOKENS = {"no-op", "noop", "no op", "无事项", "无变化"}

_QUIET_HOURS_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")

#: 合法投递渠道（v1：App 推送 + IM 账号）
DELIVERY_CHANNELS = ("push", "im")


# ---------------------------------------------------------------------------
# 到期与静默时段（L1/L2 的纯函数，表驱动单测的直接对象）
# ---------------------------------------------------------------------------
def compute_next_run(
    trigger_type: AutomationTriggerType | str,
    trigger_expr: str,
    *,
    now: datetime,
    last_run_at: datetime | None = None,
) -> datetime:
    """到期计算（L1）：interval 以最近一次运行为节拍锚点；cron 取下一个触发点。

    错过的多次到期**合并为一次**：停机/超长轮次后一律从「现在」重新起拍
    （``now + interval``），不追跑历史（验收 §L1-2）。cron 天然如此——
    ``get_next_fire_time`` 只向后看。
    """
    tz = ZoneInfo(get_scheduler_config().timezone)
    if str(trigger_type) == AutomationTriggerType.INTERVAL:
        interval = timedelta(seconds=max(1, int(trigger_expr)))
        if last_run_at is not None and now - last_run_at < interval:
            # 正常节拍：按上一轮的实际时刻顺延（run-now 提前后不重置节奏）
            return last_run_at + interval
        return now + interval
    fire = CronTrigger.from_crontab(trigger_expr, timezone=tz).get_next_fire_time(
        None, now.replace(tzinfo=UTC)
    )
    if fire is None:
        return now + timedelta(days=1)
    return fire.replace(tzinfo=None)


def validate_trigger(trigger_type: str, trigger_expr: str) -> None:
    """触发配置校验（创建/更新路径共用）：interval ≥ 60s；cron 必须可解析。"""
    if trigger_type == AutomationTriggerType.INTERVAL:
        try:
            seconds = int(trigger_expr)
        except ValueError:
            raise BadRequestException(
                message_key="errors.agent.automationTriggerInvalid",
                params={"detail": "interval 需要秒数"},
            ) from None
        if seconds < TICK_SECONDS:
            raise BadRequestException(
                message_key="errors.agent.automationTriggerInvalid",
                params={"detail": f"interval 至少 {TICK_SECONDS} 秒"},
            )
        return
    try:
        CronTrigger.from_crontab(
            trigger_expr, timezone=ZoneInfo(get_scheduler_config().timezone)
        )
    except ValueError as exc:
        raise BadRequestException(
            message_key="errors.agent.automationTriggerInvalid", params={"detail": str(exc)}
        ) from None


def validate_quiet_hours(expr: str | None) -> None:
    """静默时段校验：``HH:MM-HH:MM``，起止相同无意义。"""
    if not expr:
        return
    match = _QUIET_HOURS_RE.match(expr.strip())
    if match is None:
        raise BadRequestException(message_key="errors.agent.automationQuietHoursInvalid")
    start_h, start_m, end_h, end_m = (int(g) for g in match.groups())
    if start_h > 23 or end_h > 23 or start_m > 59 or end_m > 59 or (start_h, start_m) == (
        end_h,
        end_m,
    ):
        raise BadRequestException(message_key="errors.agent.automationQuietHoursInvalid")


def validate_delivery_channels(channels: str) -> None:
    """投递渠道校验：``push`` / ``im`` 的逗号组合，至少一个。"""
    parts = [c.strip() for c in channels.split(",") if c.strip()]
    if not parts or any(c not in DELIVERY_CHANNELS for c in parts):
        raise BadRequestException(message_key="errors.agent.automationChannelsInvalid")


def quiet_hours_end(expr: str | None, now: datetime) -> datetime | None:
    """现在是否处于静默时段：命中返回时段结束时刻（顺延目标），否则 None。

    跨午夜（``23:00-07:00``）按「start > end」识别；时区取调度器时区。
    """
    if not expr:
        return None
    match = _QUIET_HOURS_RE.match(expr.strip())
    if match is None:
        return None
    tz = ZoneInfo(get_scheduler_config().timezone)
    local = now.replace(tzinfo=UTC).astimezone(tz)
    minutes_now = local.hour * 60 + local.minute
    start_h, start_m, end_h, end_m = (int(g) for g in match.groups())
    start, end = start_h * 60 + start_m, end_h * 60 + end_m
    in_quiet = start <= minutes_now < end if start <= end else (
        minutes_now >= start or minutes_now < end
    )
    if not in_quiet:
        return None
    end_today = local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if end <= minutes_now:  # 跨午夜且已过午夜：结束在明天
        end_today += timedelta(days=1)
    return end_today.astimezone(UTC).replace(tzinfo=None)


def _day_start(now: datetime) -> datetime:
    """调度器时区的「今天」在 UTC naive 下的起点（日预算口径）。"""
    tz = ZoneInfo(get_scheduler_config().timezone)
    local = now.replace(tzinfo=UTC).astimezone(tz)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start.astimezone(UTC).replace(tzinfo=None)


def _next_day_start(now: datetime) -> datetime:
    return _day_start(now) + timedelta(days=1)


def is_no_op(text: str | None) -> bool:
    """空结论判定（L4：没事别说话）：NO-OP 字样或空正文。"""
    if not text or not text.strip():
        return True
    normalized = text.strip().strip("*_`#- \n").casefold()
    return normalized in _NO_OP_TOKENS


# ---------------------------------------------------------------------------
# 提示词规约（设计 §6 的会话侧补充：automation 建自动化的行为契约在 mclaw_tool）
# ---------------------------------------------------------------------------
def automation_instructions() -> str:
    """值守规约：拼在系统提示词后（每次运行现取，无状态）。"""
    return _AUTOMATION_SPEC


# ---------------------------------------------------------------------------
# L1 · Beat：60s 节拍任务
# ---------------------------------------------------------------------------
@register_task(
    "run_agent_automations",
    title="Agent 自动化节拍",
    title_key="settings.scheduledTasks.catalog.runAgentAutomationsTitle",
    trigger_type=TriggerType.INTERVAL,
    interval_seconds=TICK_SECONDS,
    description="挑出到期的 Agent 自动化，过闸门后以 automation 身份跑一轮值守会话。",
    description_key="settings.scheduledTasks.catalog.runAgentAutomationsDescription",
)
async def run_agent_automations() -> None:
    """L1 节拍：全局开关关着时撤下在途轮；开着时逐条拉起到期的自动化。"""
    policy = await get_setting_store().get(AgentAutomationsSetting)
    if not policy.enabled:
        # kill switch：≤1 tick 内不再有新 run 启动，在途轮次收到取消（验收 §L2-1）
        await cancel_active_runs("自动化已停用")
        return
    now = utcnow()
    async with get_database().session() as session:
        repo = AgentAutomationRepository(session)
        due = await repo.due(now)
        for automation in due:
            # 静默时段：顺延到时段结束即返回，不留痕、不排队（验收 §L2-2）
            quiet_end = quiet_hours_end(automation.quiet_hours, now)
            if quiet_end is not None:
                automation.next_run_at = quiet_end
                await repo.save(automation)
                continue
            await launch(session, automation, now=now)


async def cancel_active_runs(reason: str) -> None:
    """取消全部在途轮（kill switch / 插件卸载 / 停机时调用）。"""
    tasks = [task for task in list(_active_runs.values()) if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("已取消 %d 个在途自动化运行：%s", len(tasks), reason)


# ---------------------------------------------------------------------------
# L2 · Gate + 拉起
# ---------------------------------------------------------------------------
async def launch(
    session: AsyncSession, automation: AgentAutomation, *, now: datetime
) -> AgentAutomationRun:
    """过 L2 闸门后拉起一轮运行；被挡下时返回 skipped 的 run 行。

    节拍（tick）与 run-now 共用这一条路——手动立即跑同样过闸门（设计 §5）。
    """
    repo = AgentAutomationRepository(session)
    in_flight = _active_runs.get(automation.id)
    if in_flight is not None and not in_flight.done():
        # 单飞：不排队，记 skip(busy)（验收 §L2-3）
        return await _record_skip(repo, automation, "busy", now)
    policy = await get_setting_store().get(AgentAutomationsSetting)
    if not policy.enabled:
        return await _record_skip(repo, automation, "disabled", now)
    day_start = _day_start(now)
    if await repo.count_runs_since(automation.id, day_start) >= automation.max_runs_per_day:
        return await _record_skip(
            repo, automation, "daily_run_limit", now, next_run=_next_day_start(now)
        )
    if await repo.tokens_used_since(day_start) >= policy.daily_token_budget:
        return await _record_skip(
            repo, automation, "daily_token_budget", now, next_run=_next_day_start(now)
        )

    run = AgentAutomationRun(
        automation_id=automation.id or 0,
        started_at=now,
        status=AutomationRunStatus.RUNNING,
        session_id=automation.session_id,
    )
    session.add(run)
    automation.last_run_at = now
    # 到期推进以本轮启动为锚点：长轮次不会让下一次挤在一起（失败退避在 L5 覆盖）
    automation.next_run_at = compute_next_run(
        automation.trigger_type,
        automation.trigger_expr,
        now=now,
        last_run_at=now,
    )
    await session.commit()
    await session.refresh(run)

    task = asyncio.create_task(
        _execute_run(automation.id or 0, run.id or 0),
        name=f"agent-automation-{automation.id}-run-{run.id}",
    )
    _active_runs[automation.id or 0] = task
    task.add_done_callback(lambda _t: _active_runs.pop(automation.id or 0, None))
    return run


async def _record_skip(
    repo: AgentAutomationRepository,
    automation: AgentAutomation,
    reason: str,
    now: datetime,
    *,
    next_run: datetime | None = None,
) -> AgentAutomationRun:
    """记一条 skipped 留痕；给了 ``next_run`` 就顺延（预算类挡到明天）。"""
    run = AgentAutomationRun(
        automation_id=automation.id or 0,
        started_at=now,
        finished_at=now,
        status=AutomationRunStatus.SKIPPED,
        session_id=automation.session_id,
        skip_reason=reason,
    )
    await repo.save_run(run)
    if next_run is not None:
        automation.next_run_at = next_run
        await repo.save(automation)
    logger.info("自动化 %s 本轮跳过（%s）", automation.id, reason)
    return run


# ---------------------------------------------------------------------------
# L3 · Run：以 automation 身份跑一轮会话
# ---------------------------------------------------------------------------
class _UsageMeter:
    """挂在 on_message 前面的用量累计器：超限判断的数据源。

    runner 只在 agent_done 里给全量 usage；中间步的用量随定稿消息的
    ChatResponse 进来，逐条累计即可在超限的第一时间停下。
    """

    def __init__(self) -> None:
        self.tokens = 0

    def add(self, response: ChatResponse | None) -> None:
        if response is None or response.usage is None:
            return
        usage = response.usage
        self.tokens += usage.total_tokens or (usage.prompt_tokens + usage.completion_tokens)


async def _execute_run(automation_id: int, run_id: int) -> None:
    """一轮完整执行（L3 会话 → L4 投递 → L5 观测）。自身不抛异常：结果都落 run 行。"""
    started = time.monotonic()
    status = AutomationRunStatus.FAILED
    summary: str | None = None
    tokens = 0
    error_text: str | None = None
    count_failure = True
    recorder: AgentSessionRecorder | None = None
    run_tag = uuid.uuid4().hex[:12]
    terminal_recorded = False
    token_cap = 0

    try:
        # automation 身份：domain-events 因果链记 automation，会话内发起的
        # 下游事件都能归因回这条值守任务
        current_origin.set(Origin(kind="automation", id=str(automation_id)))
        async with get_database().session() as session:
            automation = await AgentAutomationRepository(session).get(automation_id)
            if automation is None:
                count_failure = False
                error_text = "自动化已删除，本轮终止"
                return
            token_cap = automation.max_tokens_per_run
            prompt = automation.prompt
            owner = automation_owner(automation_id)

        store = get_agent_session_store()
        session_id = automation.session_id
        if not session_id:
            # 稳定会话映射：建一次，长期复用（网页 /sessions/{id} 能看到全部历史轮次）
            header = store.create(owner=owner)
            session_id = header.session_id
            async with get_database().session() as db:
                await AgentSessionRepository(db).create(session_id, title=None, owner=owner)
                await AgentAutomationRepository(db).bind_session(automation_id, session_id)
        async with get_database().session() as db:
            session_row = await AgentSessionRepository(db).get(session_id)
            if session_row is not None and is_running(session_row):
                # 上次进程崩溃残留的运行态尚未被心跳超时自愈：本轮先让路
                status = AutomationRunStatus.SKIPPED
                error_text = "busy"
                count_failure = False
                return

        # 未配置模型供应商在这里抛出（404 语义），落成 failed run 而非无人知晓
        async with get_database().session() as session:
            llm_router = await acquire_llm_router(session)

        # 会话链尾兜底（与网页接受路径同款）：喂给供应商的历史永远配对完整
        store.seal_pending_tool_calls(session_id, reason="service_interrupted")
        history = store.build_history(session_id)
        _, entries = store.read(session_id)
        recorder = AgentSessionRecorder(store, session_id, entry_count=len(entries))

        # 工具面：mclaw 档（无 bash/read/write/edit）；令牌带 automation 标记，
        # automations 管理端点据此拒绝 automation 身份（防自我增殖）
        tools = await profile_tools(
            "mclaw", session_id, owner, automation_id=automation_id
        )
        system_prompt = await agent_system_prompt(tools) + "\n\n" + _AUTOMATION_SPEC

        usage = _UsageMeter()

        async def on_message(message: ChatMessage, response: ChatResponse | None) -> None:
            usage.add(response)
            await recorder.on_message(message, response)  # type: ignore[union-attr]

        runner = AgentRunner(
            llm_router,
            tools=tools,
            on_message=on_message,
            on_compaction=recorder.on_compaction,  # type: ignore[union-attr]
        )
        await recorder.begin(run_tag)
        await recorder.record_user_message(prompt)
        params = AgentStartParams(input=prompt, history=history, system_prompt=system_prompt)

        wall_exceeded = False
        token_exceeded = False
        terminal: AgentEvent | None = None
        try:
            async with asyncio.timeout(RUN_WALL_CLOCK_SECONDS):
                async with contextlib.aclosing(runner.start(params, run_id=run_tag)) as stream:
                    async for event in stream:
                        if event.type in ("agent_done", "agent_error"):
                            terminal = event
                            break
                        if usage.tokens >= token_cap:
                            token_exceeded = True
                            break
        except TimeoutError:
            wall_exceeded = True
        if terminal is not None:
            terminal_recorded = True
            await recorder.on_terminal(terminal, reason="service_interrupted")
            done_usage = terminal.result.usage.total_tokens if terminal.result else 0
            tokens = max(usage.tokens, done_usage)
            if terminal.type == "agent_done":
                text = (terminal.result.text or "").strip() if terminal.result else ""
                summary = text or None
                if token_exceeded or wall_exceeded or tokens > token_cap:
                    status = AutomationRunStatus.BUDGET_EXCEEDED
                elif is_no_op(text):
                    status = AutomationRunStatus.NO_OP
                    summary = None
                else:
                    status = AutomationRunStatus.COMPLETED
            else:
                status = AutomationRunStatus.FAILED
                error_text = terminal.error or source_text("errors.agent.run.unknown")
        else:
            # 超限优雅终止：runner 收到 aclose 会把半截输出按 aborted 定稿落
            # 盘，内容照常保留（验收 §L3-4）；本轮按预算超限收场
            terminal_recorded = True
            await recorder.on_terminal(
                AgentEvent(type="agent_cancelled", run_id=run_tag), reason="user_cancelled"
            )
            status = AutomationRunStatus.BUDGET_EXCEEDED
            tokens = usage.tokens
    except TimeoutError:  # pragma: no cover - asyncio.timeout 已在内部消化
        status = AutomationRunStatus.BUDGET_EXCEEDED
        error_text = "单轮墙钟超限"
    except asyncio.CancelledError:
        # kill switch / 停机取消在途轮：runner 已把半截内容落盘，这里补收尾。
        # 不计入失败（不是自动化的错），也不做退避——停用是用户 explicit 的决定。
        status = AutomationRunStatus.FAILED
        error_text = "运行被停止（自动化已停用或应用关闭）"
        count_failure = False
    except LlmError as exc:
        status = AutomationRunStatus.FAILED
        error_text = str(exc) or "模型调用失败"
    except Exception as exc:  # noqa: BLE001 - 一切意外都落成 failed run，绝不静默
        logger.exception("自动化 %s 第 %d 轮执行异常", automation_id, run_id)
        status = AutomationRunStatus.FAILED
        error_text = str(exc) or exc.__class__.__name__
    finally:
        if recorder is not None and not terminal_recorded:
            with contextlib.suppress(Exception):
                await recorder.on_terminal(
                    AgentEvent(type="agent_cancelled", run_id=run_tag),
                    reason="service_interrupted",
                )
        await _finalize_run(
            automation_id=automation_id,
            run_id=run_id,
            status=status,
            summary=summary,
            tokens=tokens,
            error_text=error_text,
            count_failure=count_failure,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def automation_owner(automation_id: int) -> str:
    """自动化会话的 owner 标识（网页会话列表据此挂 automation 徽标）。"""
    return AUTOMATION_OWNER_PREFIX + str(automation_id)


# ---------------------------------------------------------------------------
# L4 + L5 · 收尾：run 行 + 台账 + 投递 + 熔断
# ---------------------------------------------------------------------------
async def _finalize_run(
    *,
    automation_id: int,
    run_id: int,
    status: AutomationRunStatus,
    summary: str | None,
    tokens: int,
    error_text: str | None,
    count_failure: bool,
    duration_ms: int,
) -> None:
    """统一收尾：run 行定稿 → 自动化台账（失败计数/退避）→ 投递 → 熔断。"""
    now = utcnow()
    async with get_database().session() as session:
        repo = AgentAutomationRepository(session)
        run = await repo.get_run(run_id)
        automation = await repo.get(automation_id)
        if run is not None:
            run.finished_at = now
            run.status = status
            run.summary = summary
            run.tokens_used = tokens
            await repo.save_run(run)
        if automation is None:
            return
        automation.last_run_at = now
        failed = status in (AutomationRunStatus.FAILED, AutomationRunStatus.BUDGET_EXCEEDED)
        if failed and count_failure:
            failures = automation.consecutive_failures + 1
            automation.consecutive_failures = failures
            automation.last_error = error_text
            backoff = FAILURE_BACKOFF[min(failures, len(FAILURE_BACKOFF)) - 1]
            automation.next_run_at = now + backoff
            logger.warning(
                "自动化 %s 连续失败第 %d 次，退避 %s：%s",
                automation_id,
                failures,
                backoff,
                error_text,
            )
            if failures >= MAX_CONSECUTIVE_FAILURES and automation.enabled:
                automation.enabled = False
                await _raise_failure_notice(session, automation, failures, error_text)
        elif status in (AutomationRunStatus.COMPLETED, AutomationRunStatus.NO_OP):
            # 成功清零失败计数；next_run_at 已在 launch 时推进，不再动
            automation.consecutive_failures = 0
            automation.last_error = None
        await repo.save(automation)
        # L4 投递：只有 completed 且未投递过的轮次才推（NO-OP / 失败 / 超限不推；
        # delivered_at 是幂等标记——同一 run 重试投递用户只收到一次）
        if (
            status == AutomationRunStatus.COMPLETED
            and run is not None
            and run.delivered_at is None
            and summary
        ):
            run.delivered_at = now
            await repo.save_run(run)
            _deliver(automation=automation, session_id=run.session_id, summary=summary)
    logger.info(
        "自动化 %s 第 %d 轮完成 status=%s tokens=%d duration_ms=%d",
        automation_id,
        run_id,
        status.value if hasattr(status, "value") else status,
        tokens,
        duration_ms,
    )


def _deliver(*, automation: AgentAutomation, session_id: str | None, summary: str) -> None:
    """按配置渠道推送结论（fire-and-forget；任何失败只记日志）。"""
    channels = {c.strip() for c in automation.delivery_channels.split(",") if c.strip()}
    if "push" not in channels:
        return
    body = summary if len(summary) <= DELIVERY_PREVIEW_CHARS else summary[: DELIVERY_PREVIEW_CHARS - 1] + "…"
    notify(
        event="agent.automation.delivered",
        recipients={0},  # v1 仅超管
        build=lambda _session, _member_id: _content(automation.name, session_id, body),
        to_im="im" in channels,
    )


async def _content(name: str, session_id: str | None, body: str) -> AlertContent:
    from movieclaw_api.i18n import t

    return AlertContent(
        title=t("notify.automation.deliveredTitle", name=name),
        body=body,
        open=f"/sessions/{session_id}" if session_id else "/sessions",
        thread=f"automation:{name}",
        source="server",
    )


async def _raise_failure_notice(
    session: AsyncSession, automation: AgentAutomation, failures: int, error_text: str | None
) -> None:
    """连续失败熔断：自动停用 + 管理员待处理事项（推送由告警链路带出）。"""
    logger.warning("自动化 %s 连续失败 %d 次，已自动停用", automation.id, failures)
    await upsert_notice(
        session,
        dedupe_key=f"agent.automation:{automation.id}",
        severity=NoticeSeverity.ERROR,
        source="agent",
        title_key="agent.automations.noticeTitle",
        message_key="agent.automations.noticeMessage",
        message_params={
            "name": automation.name,
            "count": failures,
            "error": error_text or "",
        },
        payload={"automation_id": automation.id},
    )


async def mark_reenabled(session: AsyncSession, automation_id: int) -> None:
    """人工重新启用：熔断计数清零，停用告警随之熄灯。"""
    from movieclaw_api.services.system_notice import resolve_notices

    await resolve_notices(session, dedupe_key=f"agent.automation:{automation_id}")


# ---------------------------------------------------------------------------
# L6 · Maintain：run 历史 90 天清理
# ---------------------------------------------------------------------------
@register_task(
    "cleanup_agent_automation_runs",
    title="清理 Agent 自动化运行历史",
    title_key="settings.scheduledTasks.catalog.cleanupAgentAutomationRunsTitle",
    trigger_type=TriggerType.CRON,
    cron="50 4 * * *",  # 每天 04:50，排在 cleanup_task_runs（04:30）之后
    description="删除超过保留期的 agent_automation_run 历史记录，防止表无限增长。",
    description_key="settings.scheduledTasks.catalog.cleanupAgentAutomationRunsDescription",
)
async def cleanup_agent_automation_runs() -> None:
    cutoff = utcnow() - timedelta(days=RUN_HISTORY_RETENTION_DAYS)
    async with get_database().session() as session:
        deleted = await AgentAutomationRepository(session).purge_runs_older_than(cutoff)
    if deleted:
        logger.info("已清理 %d 条过期的自动化运行历史", deleted)
