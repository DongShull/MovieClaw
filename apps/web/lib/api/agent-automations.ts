import { request } from "@/lib/http";

/** Agent 自动化的全局闸门（agent.automations 设置域投影）。 */
export interface AutomationPolicy {
  enabled: boolean;
  daily_token_budget: number;
}

/** 读取 Agent 自动化全局闸门（设置页「AI 设定」展示用）。 */
export async function fetchAutomationPolicy(): Promise<AutomationPolicy> {
  const response = await request<{ data: AutomationPolicy }>(
    "/agent/automations/policy",
  );
  return response.data;
}

/** 增量保存：没给的项保持原值，不会覆盖别处刚改的设置。 */
export async function saveAutomationPolicy(
  changes: Partial<AutomationPolicy>,
): Promise<AutomationPolicy> {
  const response = await request<{ data: AutomationPolicy }>(
    "/agent/automations/policy",
    { method: "PUT", body: JSON.stringify(changes) },
  );
  return response.data;
}
