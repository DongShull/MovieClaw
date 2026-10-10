import { request } from "@/lib/http";

/** 后端统一响应信封（见 movieclaw_api.schemas.response.ApiResponse） */
interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

async function unwrap<T>(promise: Promise<ApiEnvelope<T>>): Promise<T> {
  return (await promise).data;
}

export type XiaoyiA2aSessionMode = "assigned" | "stateless";

export type XiaoyiA2aAuthMode = "aksk" | "apikey";

/** 配置视图：GET 回显明文 AK/SK/APIKey（管理端配置页展示用，仅管理员可见）。 */
export interface XiaoyiA2aConfig {
  enabled: boolean;
  auth_mode: XiaoyiA2aAuthMode;
  access_key: string;
  secret_key: string;
  secret_key_hint: string;
  api_key: string;
  api_key_hint: string;
  api_key_header: string;
  session_mode: XiaoyiA2aSessionMode;
}

/** rotate 的响应：唯一一次带新凭据明文（见 routes.xiaoyi_a2a）。 */
export interface XiaoyiA2aRotated {
  enabled: boolean;
  auth_mode: XiaoyiA2aAuthMode;
  session_mode: XiaoyiA2aSessionMode;
  access_key: string;
  secret_key: string;
  secret_key_hint: string;
  api_key: string;
  api_key_hint: string;
  api_key_header: string;
}

export interface XiaoyiA2aConfigPayload {
  enabled: boolean;
  auth_mode: XiaoyiA2aAuthMode;
  access_key: string;
  /** 留空 = 沿用已保存的 secret（明文不回传） */
  secret_key: string;
  api_key: string;
  api_key_header: string;
  session_mode: XiaoyiA2aSessionMode;
}

export function getXiaoyiA2aConfig(): Promise<XiaoyiA2aConfig> {
  return unwrap(request<ApiEnvelope<XiaoyiA2aConfig>>("/a2a/config"));
}

export function saveXiaoyiA2aConfig(payload: XiaoyiA2aConfigPayload): Promise<XiaoyiA2aConfig> {
  return unwrap(
    request<ApiEnvelope<XiaoyiA2aConfig>>("/a2a/config", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

/** 后端生成一对新 AK/SK 并落库，返回的 secret 明文仅此一次。 */
export function rotateXiaoyiA2aCredentials(): Promise<XiaoyiA2aRotated> {
  return unwrap(
    request<ApiEnvelope<XiaoyiA2aRotated>>("/a2a/config/rotate", { method: "POST" }),
  );
}
