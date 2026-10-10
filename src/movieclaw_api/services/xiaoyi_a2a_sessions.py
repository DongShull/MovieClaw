"""小艺云 A2A 的会话映射与运行编排（docs/research/xiaoyi-cloud-a2a.md §11.2）。

职责（对齐 IM 通道的 channel_agent.py，但会话粒度是「平台 sessionId」而非账号）：

1. **会话映射**：平台的 ``params.sessionId`` → 本机 ``agent_session_id``，每个
   sessionId 独立一条 AI 会话（平台明确要求按 sessionId 缓存上下文、严格隔离
   用户对话数据）。映射存在 ``xiaoyi_a2a_session`` 表，丢失/失效时新建会话。
   模式1 的多条并存（同一凭证 ≥5 个 agent-session-id）天然满足：每个平台
   sessionId 一行，互不相干。
2. **同会话串行**：同一 sessionId 的并发 ``message/stream`` 排队（每会话一条
   队列一个 worker），跨会话并发由信号量封顶——语义对齐 ChannelDispatcher
   （movieclaw_channel/dispatcher.py §同会话串行/跨会话并发）。
3. **clearContext**：换一条新 AI 会话（历史留在转录里不删，与 IM /reset 同款）。

安全红线：与 IM 通道同档——只挂 mclaw 产品工具，不开 bash/read/write 工作区
工具，拿不到宿主机 shell。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from movieclaw_agent import AgentRunner, AgentStartParams
from movieclaw_agent.events import AgentEvent
from movieclaw_agent.tools import make_mclaw_tool
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import NotFoundException
from movieclaw_api.services import auth as auth_service
from movieclaw_api.services.agent_session_recorder import AgentSessionRecorder
from movieclaw_api.services.agent_sessions import get_agent_session_store
from movieclaw_api.services.llm_config import acquire_llm_router
from movieclaw_api.services.mclaw_tool import render_service_map
from movieclaw_db.engine import get_database
from movieclaw_db.repositories.agent_session_repo import AgentSessionRepository
from movieclaw_db.repositories.xiaoyi_a2a_session_repo import (
    XiaoyiA2aSessionRepository,
    get_xiaoyi_session_mapping,
)

logger = logging.getLogger("movieclaw_api.xiaoyi_a2a.sessions")

#: 通道侧 AI 助手的步数上限（与 IM 通道同档，IM 对话不该跑出超长循环）
MAX_STEPS = 40
#: 全局并发上限（跨会话，与 ChannelDispatcher 的口径一致）
_MAX_CONCURRENT_RUNS = 2


async def ensure_agent_session(platform_session_id: str) -> str:
    """取该平台 sessionId 映射的本机 AI 会话 id；无映射或已失效则新建并记下。

    与 IM 通道 ensure_agent_session 的差别仅在映射键：那边是「每账号一条」，
    这里是「每平台 sessionId 一条」——多用户各持独立 sessionId，绝不串上下文。
    """
    store = get_agent_session_store()
    existing = await get_xiaoyi_session_mapping(platform_session_id)
    if existing:
        async with get_database().session() as session:
            row = await AgentSessionRepository(session).get(existing)
        if row is not None and store.path(existing).exists():
            return existing
        logger.warning(
            "小艺 A2A 会话映射已失效，将新建 platform_session=%s agent_session=%s",
            platform_session_id,
            existing,
        )
    header = store.create()
    async with get_database().session() as session:
        await AgentSessionRepository(session).create(
            header.session_id, title=f"小艺A2A · {platform_session_id[:16]}"
        )
        await XiaoyiA2aSessionRepository(session).set_mapping(
            platform_session_id, header.session_id
        )
    return header.session_id


async def clear_agent_session(platform_session_id: str) -> str:
    """clearContext：把该平台 sessionId 换到一条全新的空会话，返回新会话 id。

    历史转录不删（与 IM /reset 同款语义：换新会话而非毁尸灭迹），平台侧
    清理上下文后会更新它下发的 sessionId，这里兜的是「同 sessionId 复用」的情况。
    """
    store = get_agent_session_store()
    header = store.create()
    async with get_database().session() as session:
        await AgentSessionRepository(session).create(header.session_id, title=None)
        await XiaoyiA2aSessionRepository(session).set_mapping(
            platform_session_id, header.session_id
        )
    return header.session_id


async def _restricted_tools(session_id: str):
    """受限工具集：只挂 mclaw 产品操作（与 IM 通道同款红线，见模块文档）。"""
    settings = get_settings()
    workdir = Path(settings.agent_workspace_dir).resolve() / "xiaoyi_a2a"
    workdir.mkdir(parents=True, exist_ok=True)
    token = await auth_service.issue_agent_token(session_id)
    cli_env = {
        "MOVIECLAW_SERVER": f"http://127.0.0.1:{settings.port}",
        "MOVIECLAW_TOKEN": token,
    }
    return [make_mclaw_tool(workdir, cli_env, render_service_map())]


@dataclass(slots=True)
class _QueuedRun:
    """一次排队的 message/stream：入参 + 事件回放队列。"""

    task_id: str
    input: str
    session_id: str
    #: 事件队列：runner 产出的事件先进这里，SSE 编码器消费
    events: asyncio.Queue[AgentEvent | None] = field(default_factory=asyncio.Queue)


class XiaoyiA2aRunHub:
    """A2A 侧的运行编排：同会话串行、跨会话并发封顶、按 taskId 取消。

    不复用 AgentRunRegistry：那边的键是 agent_session_id 且公开接口耦合
    /sessions 的 SSE 语义（Last-Event-ID 游标）；A2A 的 taskId 生命周期只在
    一次流式交互内，映射回 session 再绕一圈反而容易踩「同会话并发 400」的
    路由约束。这里的模型更简单：一个请求 = 一个后台任务 + 一个事件队列。
    """

    def __init__(self) -> None:
        self._sema = asyncio.Semaphore(_MAX_CONCURRENT_RUNS)
        self._runs: dict[str, asyncio.Task[None]] = {}
        self._closing = False

    async def close(self) -> None:
        self._closing = True
        for task in self._runs.values():
            task.cancel()
        for task in self._runs.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._runs.clear()

    def is_running(self, task_id: str) -> bool:
        task = self._runs.get(task_id)
        return task is not None and not task.done()

    async def cancel(self, task_id: str) -> bool:
        """按 taskId 幂等取消；返回该任务是否（曾）在运行。"""
        task = self._runs.get(task_id)
        if task is None:
            return False
        if not task.done():
            task.cancel()
        return True

    async def start(self, run: _QueuedRun) -> None:
        """启动一次运行，事件推入 run.events，结束时推 None 作为哨兵。"""
        if self._closing:
            raise RuntimeError("小艺 A2A 运行中枢正在关闭")

        async def execute() -> None:
            try:
                async with self._sema:
                    await self._drive(run)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("小艺 A2A 运行异常 task=%s", run.task_id)
                await run.events.put(
                    AgentEvent(type="agent_error", run_id=run.task_id, error="内部错误")
                )
            finally:
                await run.events.put(None)

        self._runs[run.task_id] = asyncio.create_task(
            execute(), name=f"xiaoyi-a2a-run-{run.task_id}"
        )

    async def _drive(self, run: _QueuedRun) -> None:
        """装配路由/历史/工具并驱动一次 AgentRunner（对齐 channel_agent.run_agent）。"""
        try:
            async with get_database().session() as session:
                llm_router = await acquire_llm_router(session)
        except NotFoundException as exc:
            await run.events.put(
                AgentEvent(type="agent_error", run_id=run.task_id, error=exc.message)
            )
            return

        store = get_agent_session_store()
        history = store.build_history(run.session_id)
        recorder = AgentSessionRecorder(store, run.session_id, entry_count=len(history))
        await recorder.record_user_message(run.input)

        runner = AgentRunner(
            llm_router,
            tools=await _restricted_tools(run.session_id),
            max_steps=MAX_STEPS,
            on_message=recorder.on_message,
            on_compaction=recorder.on_compaction,
        )
        await recorder.begin(run.task_id)
        last_event: AgentEvent | None = None
        try:
            async for ev in runner.start(
                AgentStartParams(input=run.input, history=history),
                run_id=run.task_id,
            ):
                last_event = ev
                await run.events.put(ev)
        finally:
            terminal = (
                last_event
                if last_event is not None
                and last_event.type in ("agent_done", "agent_error", "agent_cancelled")
                else AgentEvent(type="agent_cancelled", run_id=run.task_id)
            )
            await recorder.on_terminal(terminal, reason="service_interrupted")


_hub: XiaoyiA2aRunHub | None = None


def get_xiaoyi_a2a_hub() -> XiaoyiA2aRunHub:
    global _hub
    if _hub is None:
        _hub = XiaoyiA2aRunHub()
    return _hub


async def close_xiaoyi_a2a_hub() -> None:
    global _hub
    if _hub is not None:
        await _hub.close()
        _hub = None


def new_queued_run(task_id: str, input: str, session_id: str) -> _QueuedRun:
    return _QueuedRun(task_id=task_id, input=input, session_id=session_id)


__all__ = [
    "XiaoyiA2aRunHub",
    "clear_agent_session",
    "close_xiaoyi_a2a_hub",
    "ensure_agent_session",
    "get_xiaoyi_a2a_hub",
    "new_queued_run",
]
