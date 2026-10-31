"""值守自动化的结论投递（docs/design/agent-automations.md §L4）。

事件出口守护（tests/api/test_event_entry_guard.py）把 notify 圈在 push 包内：
业务侧（services/agent_automations）只调这里的入口，按自动化自己的
``delivery_channels`` 决定要不要进 IM；账号 → 通知里的
``agent.automation.delivered`` 开关照常生效（规则表已登记）。

幂等在调用方：run 行的 ``delivered_at`` 是唯一投递标记，这里不做判断。
"""

from __future__ import annotations

from movieclaw_api.i18n import t
from movieclaw_api.services.push.notify import AlertContent, notify
from movieclaw_db.models.agent_automation import AgentAutomation

#: 推送正文的结论预览长度
DELIVERY_PREVIEW_CHARS = 200


def deliver_conclusion(
    automation: AgentAutomation,
    *,
    session_id: str | None,
    summary: str,
) -> None:
    """按自动化配置的渠道推送结论（fire-and-forget；任何失败只记日志）。

    ``push`` 渠道没选就不发（v1 渠道集合只有 push/im 两个，配置里勾了 im
    才会在推送之外进 IM 账号）；NO-OP / 失败 / 超限的轮次在调用方就不会
    走到这里。
    """
    channels = {c.strip() for c in automation.delivery_channels.split(",") if c.strip()}
    if "push" not in channels or not summary:
        return
    body = (
        summary
        if len(summary) <= DELIVERY_PREVIEW_CHARS
        else summary[: DELIVERY_PREVIEW_CHARS - 1] + "…"
    )
    notify(
        event="agent.automation.delivered",
        recipients={0},  # v1 仅超管
        build=lambda _session, _member_id: _content(automation.name, session_id, body),
        to_im="im" in channels,
    )


async def _content(name: str, session_id: str | None, body: str) -> AlertContent:
    return AlertContent(
        title=t("notify.automation.deliveredTitle", name=name),
        body=body,
        open=f"/sessions/{session_id}" if session_id else "/sessions",
        thread=f"automation:{name}",
        source="server",
    )
