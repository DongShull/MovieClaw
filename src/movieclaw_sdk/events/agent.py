"""AI 助手域事件（docs/design/domain-events.md）。"""

from __future__ import annotations

from typing import Literal

from movieclaw_sdk.events._base import Frozen, define


class RunFinished(Frozen):
    """AI 会话的一轮跑完了：正常回答或出错（用户主动停止、服务停机打断的不算）。"""

    member_id: int
    """会话的主人；会话目前只有超管有，恒为 0。"""
    session_id: str
    title: str | None
    """会话标题（多半取自第一句提问）。"""
    title_ref: dict | None = None
    status: Literal["done", "error"]
    answer: str
    """回答开头的纯文本（去掉 Markdown 记号、压成一行）；出错时为空。"""
    error: str | None = None
    error_ref: dict | None = None
    elapsed_ms: int | None = None
    source: Literal["app", "plugin", "channel"]
    """从哪发起：``app`` = App / 网页 / 命令行，``plugin`` = 插件代发，
    ``channel`` = 微信等 IM 通道。"""


AGENT_RUN_FINISHED = define(
    "agent.run.finished",
    RunFinished,
    title="AI 回复完成",
    group="AI 助手",
    doc="AI 会话的一轮跑完（回答完或出错）",
    sensitive=True,
    version="1.2",
)


class AutomationDelivered(Frozen):
    """值守自动化的一轮跑完且**有值得报告的结论**（NO-OP 不发，不吵人）。

    收件人按推送规则解析（v1 恒为管理员）；点开落到该自动化的稳定会话。
    """

    automation_id: int
    name: str
    """自动化名称（推送标题用）。"""
    run_id: int
    """本轮运行编号（幂等键：同一轮只投递一次）。"""
    session_id: str
    """该自动化长期复用的稳定会话；点开推送落到这里。"""
    summary: str
    """结论正文（一行纯文本预览）。"""


AGENT_AUTOMATION_DELIVERED = define(
    "agent.automation.delivered",
    AutomationDelivered,
    title="自动化有结论",
    group="AI 助手",
    doc="值守自动化跑完一轮且产生值得报告的结论（NO-OP 不发）",
    sensitive=True,
    version="1.0",
)
