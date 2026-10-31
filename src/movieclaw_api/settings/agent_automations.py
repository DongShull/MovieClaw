"""Agent 自动化的全局闸门（docs/design/agent-automations.md §L2）。

发布时默认关闭：无人值守的 LLM 会话是真金白银，开关必须在设置页显式打开。
日 token 预算是三层预算的最外层（单轮/每自动化每日/全局每日）。
"""

from __future__ import annotations

from pydantic import Field

from movieclaw_api.settings.base import SettingSchema, register_setting

AGENT_AUTOMATIONS_NAMESPACE = "agent.automations"


@register_setting(namespace=AGENT_AUTOMATIONS_NAMESPACE, title="Agent 自动化")
class AgentAutomationsSetting(SettingSchema):
    """Agent 自动化的全局开关与预算。

    - ``enabled``：kill switch。关掉后节拍不再拉起新的一轮，在途轮次在
      下一拍被取消；自动化行自己的 ``enabled`` 不受影响（重新打开即恢复）。
    - ``daily_token_budget``：全部自动化每天（按调度器时区的自然日）消耗的
      token 总预算；达到后当天的到期一律记 skip(daily_token_budget)。
    """

    enabled: bool = Field(default=False, description="Agent 自动化总开关（默认关）")
    daily_token_budget: int = Field(
        default=200_000, ge=1000, description="全部自动化每天消耗的 token 预算"
    )
