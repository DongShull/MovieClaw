"use client";

/**
 * 「小艺云 A2A」设置分区（docs/research/xiaoyi-cloud-a2a.md）。
 *
 * 华为小艺开放平台「云A2A智能体」回传任务的接入配置：
 *   - API URL 动态拼接 = 网络页「外部访问」配置的 external_url + 固定路径；
 *   - 鉴权方式可选 AK/SK（accessKey/secretKey 签名）或 APIKey（简单令牌）；
 *   - 凭据明文展示（仅管理员可见），便于原样粘进小艺平台；
 *   - 「生成密钥」按当前鉴权方式后端重新生成对应凭据，覆盖旧值。
 */

import { useCallback, useEffect, useState } from "react";

import { ErrorBanner, Toggle } from "@/components/cloud-push-ui";
import { CopyButton } from "@/components/copy-button";
import { useConfirm, useToast } from "@/components/feedback";
import {
  SETTINGS_BUTTON_CLASS,
  SETTINGS_INPUT_CLASS,
  SettingsList,
  SettingsRow,
  SettingsSection,
} from "@/components/settings-ui";
import { getAppConfig } from "@/lib/api/app";
import {
  type XiaoyiA2aAuthMode,
  type XiaoyiA2aConfig,
  type XiaoyiA2aSessionMode,
  getXiaoyiA2aConfig,
  rotateXiaoyiA2aCredentials,
  saveXiaoyiA2aConfig,
} from "@/lib/api/xiaoyi-a2a";

const A2A_PATH = "/api/v1/a2a/agent/message";

/** 华为小艺开放平台管理中心（创建/配置云A2A智能体、A2A基本配置、认证信息） */
const HAG_URL = "https://developer.huawei.com/consumer/cn/hag/hagindex.html#/";

const AUTH_MODE_LABELS: Record<XiaoyiA2aAuthMode, string> = {
  aksk: "AK/SK（accessKey / sign / ts 签名）",
  apikey: "APIKey（简单令牌）",
};

/** 会话维持方式：与平台「A2A 基本配置」里的两个选项同名（见 docs/research/xiaoyi-cloud-a2a.md §3） */
const SESSION_MODE_LABELS: Record<XiaoyiA2aSessionMode, string> = {
  assigned: "由服务器侧分配 Session",
  stateless: "服务器间采用无状态通信，每次携带认证凭据",
};

export function XiaoyiA2aSection() {
  const confirm = useConfirm();
  const toast = useToast();
  const [config, setConfig] = useState<XiaoyiA2aConfig | null>(null);
  const [externalUrl, setExternalUrl] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [hideSecret, setHideSecret] = useState(false);
  const [hideAccessKey, setHideAccessKey] = useState(false);
  const [hideApiKey, setHideApiKey] = useState(false);

  const load = useCallback(async () => {
    try {
      setConfig(await getXiaoyiA2aConfig());
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
    // 外部访问地址来自「设置 → 网络 → 外部访问」，拉不到也不阻塞本页
    getAppConfig()
      .then((v) => setExternalUrl(v.external_url ?? ""))
      .catch(() => {});
  }, [load]);

  const apiUrl = externalUrl ? externalUrl.replace(/\/+$/, "") + A2A_PATH : "";

  /** 只改 enabled / auth_mode / session_mode 时用：凭据字段留空 = 沿用已存值。 */
  function basePayload(patch: Partial<XiaoyiA2aConfig>) {
    return {
      enabled: config!.enabled,
      auth_mode: config!.auth_mode,
      access_key: config!.access_key,
      secret_key: "",
      api_key: "",
      api_key_header: config!.api_key_header,
      session_mode: config!.session_mode,
      ...patch,
    };
  }

  async function handleToggle(enabled: boolean) {
    if (config == null) return;
    const previous = config;
    setConfig({ ...config, enabled });
    setBusy(true);
    setError(null);
    try {
      setConfig(await saveXiaoyiA2aConfig(basePayload({ enabled })));
      toast.success(enabled ? "已启用小艺云 A2A" : "已关闭小艺云 A2A");
    } catch (e) {
      setError((e as Error).message);
      setConfig(previous);
    } finally {
      setBusy(false);
    }
  }

  async function handleModeChange(mode: XiaoyiA2aAuthMode) {
    if (config == null) return;
    const previous = config;
    setConfig({ ...config, auth_mode: mode });
    setBusy(true);
    setError(null);
    try {
      setConfig(await saveXiaoyiA2aConfig(basePayload({ auth_mode: mode })));
      toast.success("认证方式已保存");
    } catch (e) {
      setError((e as Error).message);
      setConfig(previous);
    } finally {
      setBusy(false);
    }
  }

  async function handleSessionModeChange(mode: XiaoyiA2aSessionMode) {
    if (config == null) return;
    const previous = config;
    setConfig({ ...config, session_mode: mode });
    setBusy(true);
    setError(null);
    try {
      setConfig(await saveXiaoyiA2aConfig(basePayload({ session_mode: mode })));
      toast.success("会话维持方式已保存");
    } catch (e) {
      setError((e as Error).message);
      setConfig(previous);
    } finally {
      setBusy(false);
    }
  }

  async function handleApiKeyHeaderSave(value: string) {
    if (config == null) return;
    const header = value.trim() || "Authorization";
    if (header === config.api_key_header) return;
    const previous = config;
    setConfig({ ...config, api_key_header: header });
    setBusy(true);
    setError(null);
    try {
      setConfig(await saveXiaoyiA2aConfig(basePayload({ api_key_header: header })));
      toast.success("Header 名已保存");
    } catch (e) {
      setError((e as Error).message);
      setConfig(previous);
    } finally {
      setBusy(false);
    }
  }

  async function handleRotate() {
    const isApiKey = config?.auth_mode === "apikey";
    if (
      !(await confirm({
        title: isApiKey ? "生成新的 APIKey？" : "生成新的接入密钥？",
        description: "旧凭据立刻作废，你需要把小艺开放平台里的认证信息同步更新。",
        confirmLabel: "生成",
        tone: "danger",
      }))
    )
      return;
    setBusy(true);
    setError(null);
    try {
      await rotateXiaoyiA2aCredentials();
      setHideSecret(false);
      setHideAccessKey(false);
      setHideApiKey(false);
      await load();
      toast.success(
        isApiKey
          ? "已生成新 APIKey，请填进小艺开放平台"
          : "已生成新密钥，请把下面的 accessKey / secretKey 填进小艺开放平台",
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (config == null) {
    return (
      <div className="space-y-2.5">
        <div className="h-[72px] animate-pulse rounded-xl bg-white/[0.04]" />
        <div className="h-[72px] animate-pulse rounded-xl bg-white/[0.04]" />
        {error && <ErrorBanner>{error}</ErrorBanner>}
      </div>
    );
  }

  const isApiKey = config.auth_mode === "apikey";

  return (
    <div className="space-y-10">
      {/* API URL：动态拼接（外部访问地址 + 固定路径），填进小艺平台的「API URL」 */}
      <SettingsSection title="API URL">
        <SettingsList>
          {apiUrl ? (
            <SettingsRow label="端点地址" description="填进小艺开放平台「A2A 基本配置 → API URL」">
              <div className="flex w-full items-center gap-2">
                <code className="min-w-0 flex-1 break-all rounded-lg bg-black/30 px-3 py-2 font-mono text-caption text-[var(--text)]">
                  {apiUrl}
                </code>
                <CopyButton text={apiUrl} label="复制" className={SETTINGS_BUTTON_CLASS} />
              </div>
            </SettingsRow>
          ) : (
            <SettingsRow
              label="端点地址"
              description="先到「网络 → 外部访问」配置外部访问地址，这里会自动拼出端点地址"
            >
              <span className="text-sub text-[var(--text-faint)]">尚未配置外部访问地址</span>
            </SettingsRow>
          )}
        </SettingsList>
      </SettingsSection>

      {error && <ErrorBanner>{error}</ErrorBanner>}

      <SettingsSection
        title="认证信息"
        action={
          <button
            type="button"
            disabled={busy}
            onClick={() => void handleRotate()}
            className={SETTINGS_BUTTON_CLASS}
          >
            生成密钥
          </button>
        }
      >
        <SettingsList>
          <SettingsRow label="启用小艺云 A2A" description="关闭后端点一律 404，对外完全隐身">
            <div className="flex items-center gap-3">
              <a
                href={HAG_URL}
                target="_blank"
                rel="noopener noreferrer"
                className={`${SETTINGS_BUTTON_CLASS} inline-flex items-center`}
              >
                打开小艺开放平台
              </a>
              <Toggle
                checked={config.enabled}
                label="启用小艺云 A2A"
                onChange={(v) => void handleToggle(v)}
              />
            </div>
          </SettingsRow>

          <SettingsRow label="认证方式" description="与小艺平台「认证信息」里选的方式保持一致">
            <select
              aria-label="认证方式"
              value={config.auth_mode}
              onChange={(e) => void handleModeChange(e.target.value as XiaoyiA2aAuthMode)}
              disabled={busy}
              className={`${SETTINGS_INPUT_CLASS} w-72 max-sm:w-44`}
            >
              {(Object.keys(AUTH_MODE_LABELS) as XiaoyiA2aAuthMode[]).map((mode) => (
                <option key={mode} value={mode}>
                  {AUTH_MODE_LABELS[mode]}
                </option>
              ))}
            </select>
          </SettingsRow>

          <SettingsRow
            label="会话维持方式"
            description="与小艺平台「A2A 基本配置」里选的方式保持一致；AK/SK 与 APIKey 两种认证都适用"
          >
            <select
              aria-label="会话维持方式"
              value={config.session_mode}
              onChange={(e) => void handleSessionModeChange(e.target.value as XiaoyiA2aSessionMode)}
              disabled={busy}
              className={`${SETTINGS_INPUT_CLASS} w-96 max-sm:w-52`}
            >
              {(Object.keys(SESSION_MODE_LABELS) as XiaoyiA2aSessionMode[]).map((mode) => (
                <option key={mode} value={mode}>
                  {SESSION_MODE_LABELS[mode]}
                </option>
              ))}
            </select>
          </SettingsRow>

          {isApiKey ? (
            <>
              <SettingsRow
                label="Header 名"
                description="华为「Header域传参」里填的 header 名（默认 Authorization），两边保持一致"
              >
                <input
                  type="text"
                  key={config.api_key_header}
                  defaultValue={config.api_key_header}
                  onBlur={(e) => void handleApiKeyHeaderSave(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                  aria-label="Header 名"
                  className={`${SETTINGS_INPUT_CLASS} w-48 max-sm:w-36 font-mono`}
                />
              </SettingsRow>
              <SettingsRow
                label="API key"
                description="令牌，原样填进小艺平台「认证信息 → 值」（随上面的 Header 名携带）"
              >
                <div className="flex items-center gap-2">
                  <input
                    type={hideApiKey ? "password" : "text"}
                    readOnly
                    value={config.api_key}
                    placeholder="（未生成）"
                    onFocus={(e) => e.currentTarget.select()}
                    aria-label="API key"
                    className={`${SETTINGS_INPUT_CLASS} w-64 max-sm:w-40 font-mono`}
                  />
                  {config.api_key && (
                    <div className="flex flex-col gap-1">
                      <button
                        type="button"
                        onClick={() => setHideApiKey((v) => !v)}
                        className={SETTINGS_BUTTON_CLASS}
                      >
                        {hideApiKey ? "显示" : "隐藏"}
                      </button>
                      <CopyButton
                        text={config.api_key}
                        label="复制"
                        className={SETTINGS_BUTTON_CLASS}
                      />
                    </div>
                  )}
                </div>
              </SettingsRow>
            </>
          ) : (
            <>
              <SettingsRow label="Access key" description="接入码，原样填进小艺平台「认证信息 → Access key」">
                <div className="flex items-center gap-2">
                  <input
                    type={hideAccessKey ? "password" : "text"}
                    readOnly
                    value={config.access_key}
                    placeholder="（未生成）"
                    onFocus={(e) => e.currentTarget.select()}
                    aria-label="Access key"
                    className={`${SETTINGS_INPUT_CLASS} w-64 max-sm:w-40 font-mono`}
                  />
                  {config.access_key && (
                    <div className="flex flex-col gap-1">
                      <button
                        type="button"
                        onClick={() => setHideAccessKey((v) => !v)}
                        className={SETTINGS_BUTTON_CLASS}
                      >
                        {hideAccessKey ? "显示" : "隐藏"}
                      </button>
                      <CopyButton
                        text={config.access_key}
                        label="复制"
                        className={SETTINGS_BUTTON_CLASS}
                      />
                    </div>
                  )}
                </div>
              </SettingsRow>

              <SettingsRow label="Secret key" description="接入密钥，原样填进小艺平台「认证信息 → Secret key」">
                <div className="flex items-center gap-2">
                  <input
                    type={hideSecret ? "password" : "text"}
                    readOnly
                    value={config.secret_key}
                    placeholder="（未生成）"
                    onFocus={(e) => e.currentTarget.select()}
                    aria-label="Secret key"
                    className={`${SETTINGS_INPUT_CLASS} w-64 max-sm:w-40 font-mono`}
                  />
                  {config.secret_key && (
                    <div className="flex flex-col gap-1">
                      <button
                        type="button"
                        onClick={() => setHideSecret((v) => !v)}
                        className={SETTINGS_BUTTON_CLASS}
                      >
                        {hideSecret ? "显示" : "隐藏"}
                      </button>
                      <CopyButton
                        text={config.secret_key}
                        label="复制"
                        className={SETTINGS_BUTTON_CLASS}
                      />
                    </div>
                  )}
                </div>
              </SettingsRow>

            </>
          )}
        </SettingsList>
      </SettingsSection>
    </div>
  );
}
