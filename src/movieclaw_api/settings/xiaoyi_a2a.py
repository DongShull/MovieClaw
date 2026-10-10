"""小艺云 A2A 配置域（docs/research/xiaoyi-cloud-a2a.md §1 / §4）。

一条配置承载「小艺开放平台 → 本机」入站接入的全部凭据：

- ``enabled``：总开关，关闭时 A2A 端点一律 404（与 MCP 端点同款立场——存量
  部署升级后对外零变化）；
- ``access_key`` / ``secret_key``：小艺开放平台 A2A 基础配置里分配的 AK/SK，
  入站请求按 ``sign = Base64(HMAC-SHA256(secret_key, ts))`` 验签（§4.1）；
- ``session_mode``：会话维持方式，与小艺平台「A2A 基本配置」里的选项一一对应：
  ``assigned`` → 「由服务器侧分配 Session」；``stateless`` →
  「服务器间采用无状态通信，每次携带认证凭据」。端点两种都认（见 routes.xiaoyi_a2a
  的 ``verify_auth``），这个选项用于界面按平台的实际配置给出对应指引。

secret 嵌套在本模型顶层，``register_setting(secret_fields=...)`` 直接可用；
``secret_key`` 明文只在保存时经 SecretBox 加密落库，读取时解密回明文模型。
"""

from __future__ import annotations

import secrets
from typing import Literal

from pydantic import Field

from movieclaw_api.settings.base import SettingSchema, register_setting

A2A_NAMESPACE = "xiaoyi.a2a"

#: 两种 header 命名风格的三组名字（accessKey / sign / ts）
HEADER_STYLES: dict[str, tuple[str, str, str]] = {
    "plain": ("accessKey", "sign", "ts"),
    "x": ("X-Access-Key", "X-Sign", "X-Ts"),
}

#: 防重放窗口（秒）：文档建议校验 |Δts| < 15 分钟（§4.1）
TS_TOLERANCE_SECONDS = 15 * 60

#: 生成凭据的前缀（与 webhook 的 mcwh_ / MCP 的 mcp_ 同立场：肉眼可辨来源）
_ACCESS_KEY_PREFIX = "mcak_"
_SECRET_KEY_PREFIX = "mcsk_"
_API_KEY_PREFIX = "mcapi_"

#: 小艺开放平台凭据字段的长度上限。accessKey 规格写明 String(64)；实测 secretKey
#: 超过 64 也会被平台静默截断——69 字符贴进去只存前 64，截断后的值算签名与后端
#: 永远对不上。所以生成侧就守在 64 以内，别让用户贴一个平台会改写的值。
_CREDENTIAL_MAX_LENGTH = 64

#: APIKey 鉴权模式下，客户端携带 APIKey 的候选 header 名（华为文档只写
#: 「将 APIkey 置于请求 Header 中传输」，没定 header 名——常见名都认一遍）
API_KEY_HEADER_NAMES: tuple[str, ...] = (
    "Authorization",
    "X-API-Key",
    "apiKey",
    "api-key",
    "apikey",
)

#: APIKey 鉴权模式下，Query 传递的候选参数名
API_KEY_QUERY_NAMES: tuple[str, ...] = ("apiKey", "apikey", "api_key")


def _random_credential(prefix: str) -> str:
    """``prefix`` + 随机 hex，长度按平台 64 字符上限反推（贴得进、不会被截断）。"""
    hex_chars = _CREDENTIAL_MAX_LENGTH - len(prefix)
    hex_chars -= hex_chars % 2  # 取偶数，正好整字节
    return prefix + secrets.token_hex(hex_chars // 2)


def generate_access_key() -> str:
    """生成一枚接入码（``mcak_`` + 16 字节随机 hex，落在 64 字符限制内）。"""
    return _ACCESS_KEY_PREFIX + secrets.token_hex(16)


def generate_secret_key() -> str:
    """生成一枚接入密钥（长度守在平台 64 字符上限内，避免被平台截断后验签失败）。"""
    return _random_credential(_SECRET_KEY_PREFIX)


def generate_api_key() -> str:
    """生成一枚 APIKey（长度守在平台 64 字符上限内，避免被平台截断后校验失败）。"""
    return _random_credential(_API_KEY_PREFIX)


@register_setting(
    namespace=A2A_NAMESPACE,
    title="小艺云 A2A",
    secret_fields=["secret_key", "api_key"],
)
class XiaoyiA2aSetting(SettingSchema):
    """小艺云 A2A 入站接入配置。默认关闭、凭据为空——不影响存量部署。"""

    enabled: bool = Field(
        default=False,
        description="总开关。关闭时 A2A 端点一律 404（对外完全隐身）",
    )
    auth_mode: Literal["aksk", "apikey"] = Field(
        default="aksk",
        description="鉴权方式：aksk=accessKey/sign/ts 签名；apikey=简单 APIKey 令牌",
    )
    access_key: str = Field(
        default="",
        description="AK/SK 模式的接入码（accessKey），请求头原样携带",
    )
    secret_key: str = Field(
        default="",
        description="AK/SK 模式的接入密钥（secretKey），用于验算请求签名；加密落库",
    )
    api_key: str = Field(
        default="",
        description="APIKey 模式的 APIKey 令牌，请求头/Query 携带；加密落库",
    )
    api_key_header: str = Field(
        default="Authorization",
        description="APIKey 模式携带令牌的 header 名（华为侧可自定义，默认 Authorization）",
    )
    session_mode: Literal["assigned", "stateless"] = Field(
        default="assigned",
        description=(
            "会话维持方式（与小艺平台「A2A 基本配置」里的选项保持一致）："
            "assigned=由服务器侧分配 Session；"
            "stateless=服务器间采用无状态通信，每次携带认证凭据"
        ),
    )
