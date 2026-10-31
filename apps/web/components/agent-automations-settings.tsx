"use client";

import { useTranslations } from "next-intl";

import { useCallback, useEffect, useState } from "react";

import { Toggle } from "@/components/cloud-push-ui";
import { SETTINGS_INPUT_CLASS, SettingsRow, SettingsSection } from "@/components/settings-ui";
import { fetchAutomationPolicy, saveAutomationPolicy } from "@/lib/api/agent-automations";

/**
 * 「AI 设定」分区里的 Agent 自动化总闸（docs/design/agent-automations.md §L2）。
 *
 * 无人值守的 LLM 会话是真金白银，发布默认关；打开后节拍才会拉起到期的自动化。
 * 与日 token 预算同框：开关改成即存（失败回滚），预算数字失焦或回车提交。
 */
export function AgentAutomationsSettings() {
  const t = useTranslations();
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [budget, setBudget] = useState<number | null>(null);
  const [budgetDraft, setBudgetDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchAutomationPolicy()
      .then((policy) => {
        if (!alive) return;
        setEnabled(policy.enabled);
        setBudget(policy.daily_token_budget);
        setBudgetDraft(String(policy.daily_token_budget));
      })
      .catch((e: Error) => {
        if (alive) setError(e.message);
      });
    return () => {
      alive = false;
    };
  }, []);

  const toggle = useCallback(
    async (next: boolean) => {
      const previous = enabled;
      setEnabled(next); // 乐观更新：失败回滚，成功时省一次等待
      setBusy(true);
      setError(null);
      try {
        const policy = await saveAutomationPolicy({ enabled: next });
        setEnabled(policy.enabled);
      } catch (e) {
        setEnabled(previous);
        setError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [enabled],
  );

  const commitBudget = useCallback(async () => {
    if (budgetDraft === "" || budget == null) return;
    const parsed = Number(budgetDraft);
    if (!Number.isInteger(parsed) || parsed < 1000) {
      setBudgetDraft(String(budget));
      return;
    }
    const previous = budget;
    setBusy(true);
    setError(null);
    try {
      const policy = await saveAutomationPolicy({ daily_token_budget: parsed });
      setBudget(policy.daily_token_budget);
      setBudgetDraft(String(policy.daily_token_budget));
    } catch (e) {
      setBudget(previous);
      setBudgetDraft(String(previous));
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [budget, budgetDraft]);

  return (
    <SettingsSection title={t("settings.ai.automations.title")}>
      <SettingsRow
        label={t("settings.ai.automations.toggleLabel")}
        description={
          enabled
            ? t("settings.ai.automations.onHint")
            : t("settings.ai.automations.offHint")
        }
        error={error}
      >
        {enabled !== null && (
          <Toggle
            checked={enabled}
            label={t("settings.ai.automations.toggleLabel")}
            disabled={busy}
            onChange={(next) => void toggle(next)}
          />
        )}
      </SettingsRow>
      {budget !== null && (
        <SettingsRow
          label={t("settings.ai.automations.budgetLabel")}
          description={t("settings.ai.automations.budgetHint")}
        >
          <input
            type="number"
            min={1000}
            step={1000}
            aria-label={t("settings.ai.automations.budgetLabel")}
            value={budgetDraft}
            disabled={busy}
            onChange={(e) => setBudgetDraft(e.target.value)}
            onBlur={() => void commitBudget()}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.currentTarget.blur();
              }
            }}
            className={`${SETTINGS_INPUT_CLASS} w-40 max-sm:w-full`}
          />
        </SettingsRow>
      )}
    </SettingsSection>
  );
}
