"""小艺云 A2A 端点：单一 POST 入口的 JSON-RPC 分发 + SSE 编码。

协议规格：docs/research/xiaoyi-cloud-a2a.md（华为小艺开放平台「云A2A协议」）。

    POST /api/v1/a2a/agent/message
      - AK/SK 验签（sign = Base64(HMAC-SHA256(secretKey, ts))，|Δts| < 15min）
      - JSON-RPC 2.0 方法：initialize / notifications/initialized /
        message/stream（SSE）/ tasks/cancel / clearContext
      - authorize / deauthorize / push 二期实现，先回标准 JSON-RPC 错误

公开区路由（与 hooks 同立场）：不进 /api/v1 登录鉴权体系，验签是唯一门票；
未启用或未配置凭据时一律 404，不向探测者暴露端点存在。

事件映射（AgentEvent → A2A，规格 §5.2/§5.3）：
    agent_start           → status-update state=working
    thinking_delta        → artifact-update parts[reasoningText] append=true
    text_delta            → artifact-update parts[text] append=true
    tool_call/tool_result → status-update（过程状态文案）
    agent_done            → 终答 artifact-update lastChunk=true + status
                            state=completed + final=true
    agent_error           → status state=failed + final=true
    agent_cancelled       → status state=canceled + final=true
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from movieclaw_agent.events import AgentEvent
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.xiaoyi_a2a_sessions import (
    clear_agent_session,
    ensure_agent_session,
    get_xiaoyi_a2a_hub,
    new_queued_run,
)
from movieclaw_api.settings import (
    HEADER_STYLES,
    TS_TOLERANCE_SECONDS,
    get_setting_store,
)
from movieclaw_api.settings.xiaoyi_a2a import (
    API_KEY_HEADER_NAMES,
    API_KEY_QUERY_NAMES,
    XiaoyiA2aSetting,
    generate_access_key,
    generate_api_key,
    generate_secret_key,
)

logger = logging.getLogger("movieclaw_api.xiaoyi_a2a")

router = APIRouter(prefix="/a2a", tags=["xiaoyi-a2a"], include_in_schema=False)

#: 管理面（「设置 → 小艺云 A2A」页的后端），挂管理区；GET 回显明文供配置页粘贴
admin_router = APIRouter(prefix="/a2a", tags=["xiaoyi-a2a"])


class XiaoyiA2aConfigView(BaseModel):
    enabled: bool
    auth_mode: str
    access_key: str
    #: secret 明文——管理端「小艺云 A2A」配置页展示用（仅管理员可见），
    #: 方便用户原样粘贴进小艺开放平台；写入侧仍走 SecretBox 加密落库
    secret_key: str = ""
    secret_key_hint: str = ""
    api_key: str = ""
    api_key_hint: str = ""
    api_key_header: str = "Authorization"
    session_mode: str


class XiaoyiA2aConfigPayload(BaseModel):
    enabled: bool = False
    auth_mode: str = Field(default="aksk", pattern="^(aksk|apikey)$")
    access_key: str = ""
    #: 留空 = 沿用已保存的 secret（避免明文回读）
    secret_key: str = ""
    api_key: str = ""
    api_key_header: str = "Authorization"
    session_mode: str = Field(default="assigned", pattern="^(assigned|stateless)$")


class XiaoyiA2aConfigRotatedView(BaseModel):
    """rotate 的返回：新凭据明文，**仅此一次**回显，之后只给打码指纹。"""

    enabled: bool
    auth_mode: str
    session_mode: str
    access_key: str
    secret_key: str
    secret_key_hint: str
    api_key: str
    api_key_hint: str
    api_key_header: str

#: 服务端分配的 agent-session-id 有效期（秒）：规格建议 7 天（§3）
SESSION_ID_TTL_SECONDS = 7 * 24 * 3600

#: JSON-RPC 2.0 标准错误码（规格 §9）
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
METHOD_NOT_FOUND = -32601


def _rpc_response(
    req_id: Any,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result or {}
    return body


def _error_response(req_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(_rpc_response(req_id, error={"code": code, "message": message}))


async def _load_config() -> XiaoyiA2aSetting | None:
    """取配置；未启用或当前鉴权方式的凭据不全时返回 None（端点对外表现为 404）。"""
    from movieclaw_api.settings.xiaoyi_a2a import XiaoyiA2aSetting as _T

    config = await get_setting_store().get(_T)
    if not config.enabled:
        return None
    if config.auth_mode == "apikey":
        if not config.api_key:
            return None
    elif not config.access_key or not config.secret_key:
        return None
    return config  # type: ignore[return-value]


def verify_signature(config: XiaoyiA2aSetting, headers: Any, *, now_ms: int | None = None) -> bool:
    """AK/SK 验签：三个 header + HMAC 比对 + 时间窗防重放（规格 §4.1）。

    header 名不依赖配置去猜——华为文档两处写法不一致（规范篇 accessKey/sign/ts，
    PUSH 篇 X-Access-Key/X-Sign/X-Ts），这里两种命名都认，哪个命中用哪个。
    """
    access_key = sign = ts = None
    for ak_name, sign_name, ts_name in HEADER_STYLES.values():
        access_key = headers.get(ak_name)
        sign = headers.get(sign_name)
        ts = headers.get(ts_name)
        if access_key and sign and ts:
            break
    if not access_key or not sign or not ts:
        return False
    if not hmac.compare_digest(access_key, config.access_key):
        return False
    try:
        ts_value = int(ts)
    except ValueError:
        return False
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if abs(now_ms - ts_value) > TS_TOLERANCE_SECONDS * 1000:
        return False
    expected = base64.b64encode(
        hmac.new(
            config.secret_key.encode("utf-8"), ts.encode("utf-8"), hashlib.sha256
        ).digest()
    ).decode("ascii")
    return hmac.compare_digest(sign, expected)


def verify_api_key(config: XiaoyiA2aSetting, request: Request) -> bool:
    """APIKey 鉴权：令牌放 header 或 Query 都认，常数时间比较。

    华为文档只写「将 APIkey 置于请求 Header 中传输」，没定 header 名，
    且华为平台允许自定义 header 名（默认 Authorization），所以：
    配置的 header 名优先，常见名兜底，Query 传递也兼容；同时兼容
    ``Authorization: Bearer <key>`` 的写法。
    """
    if not config.api_key:
        return False
    expected = config.api_key
    names = [config.api_key_header] if config.api_key_header else []
    for name in API_KEY_HEADER_NAMES:
        if name not in names:
            names.append(name)
    for name in names:
        got = request.headers.get(name)
        if got:
            candidates = {got, got.removeprefix("Bearer "), got.removeprefix("bearer ")}
            if any(c and hmac.compare_digest(c, expected) for c in candidates):
                return True
    for name in API_KEY_QUERY_NAMES:
        got = request.query_params.get(name)
        if got and hmac.compare_digest(got, expected):
            return True
    return False


def _session_key(config: XiaoyiA2aSetting) -> str:
    """给 agent-session-id 签名的密钥：与当前鉴权方式共用同一份凭据。"""
    return config.api_key if config.auth_mode == "apikey" else config.secret_key


def _session_signature(config: XiaoyiA2aSetting, expiry_ms: int) -> str:
    payload = f"a2a-session.{config.access_key}.{expiry_ms}"
    return hmac.new(
        _session_key(config).encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def mint_agent_session_id(config: XiaoyiA2aSetting) -> str:
    """分配一个带签名与有效期的 agent-session-id（规格 §3 模式1）。

    用自签令牌而非落库随机串：无状态、进程重启后下发的 id 仍然可验；同一凭据下
    可无限并行多条会话（规格要求 ≥5）。长度守在 64 字符内，避免被平台字段截断。
    """
    expiry_ms = int((time.time() + SESSION_ID_TTL_SECONDS) * 1000)
    return f"{expiry_ms:x}.{_session_signature(config, expiry_ms)[:32]}"


def verify_agent_session_id(config: XiaoyiA2aSetting, token: str) -> bool:
    """校验客户端带回的 agent-session-id：签名对得上且未过期才放行。"""
    if not token or "." not in token or not _session_key(config):
        return False
    expiry_hex, _, signature = token.partition(".")
    try:
        expiry_ms = int(expiry_hex, 16)
    except ValueError:
        return False
    if expiry_ms < int(time.time() * 1000):
        return False
    return hmac.compare_digest(signature, _session_signature(config, expiry_ms)[:32])


def verify_auth(config: XiaoyiA2aSetting, request: Request) -> bool:
    """两种会话模式的鉴权（规格 §3），先认凭据再认会话：

    - 模式2（服务器间无状态，每次携带认证凭据）：aksk 走 HMAC 验签，apikey 走令牌比对；
    - 模式1（由服务器侧分配 Session）：initialize 分配的 ``agent-session-id`` 由客户端
      在 header 带回，替代之后每次重复签名。
    """
    if config.auth_mode == "apikey":
        if verify_api_key(config, request):
            return True
    elif verify_signature(config, request.headers):
        return True
    return verify_agent_session_id(config, request.headers.get("agent-session-id") or "")


def _extract_user_text(params: dict[str, Any]) -> str | None:
    """从 message.parts 里取用户文本（kind=text 的 parts 拼接；file/data 忽略）。"""
    message = params.get("message") or {}
    parts = message.get("parts") or []
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("kind") == "text"]
    text = "\n".join(t for t in texts if t).strip()
    return text or None


def _sse_frame(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _status_event(
    rpc_id: str, task_id: str, state: str, text: str, *, final: bool = False
) -> dict[str, Any]:
    """A2A status-update 事件（规格 §5.2）。"""
    return _rpc_response(
        rpc_id,
        result={
            "taskId": task_id,
            "kind": "status-update",
            "final": final,
            "status": {
                "message": {
                    "role": "agent",
                    "parts": [{"kind": "text", "text": text}],
                },
                "state": state,
            },
        },
    )


def _artifact_event(
    rpc_id: str,
    task_id: str,
    artifact_id: str,
    part: dict[str, Any],
    *,
    append: bool = True,
    last_chunk: bool = False,
    final: bool = False,
) -> dict[str, Any]:
    """A2A artifact-update 事件（规格 §5.3）。"""
    return _rpc_response(
        rpc_id,
        result={
            "taskId": task_id,
            "kind": "artifact-update",
            "append": append,
            "lastChunk": last_chunk,
            "final": final,
            "artifact": {"artifactId": artifact_id, "parts": [part]},
        },
    )


@router.post("/agent/message")
async def agent_message(request: Request) -> Any:
    """A2A 单一端点：验签/验令牌 → JSON-RPC 分发。"""
    config = cast("XiaoyiA2aSetting | None", await _load_config())
    if config is None:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if not verify_auth(config, request):
        # 排障：把华为实际发来的鉴权相关 header 原样打出来——header 名对不上
        # （X- 前缀 vs 裸名）还是值对不上，一眼就能定位
        auth_headers = {
            k: v for k, v in request.headers.items() if k.lower() in {
                "accesskey", "x-access-key", "sign", "x-sign", "ts", "x-ts",
                "authorization", "x-api-key", "apikey", "api-key",
                "agent-session-id", "mcp-session-id",
            }
        }
        # 顺带记下来源与报文，区分「华为没带鉴权头」还是「反代剥了头」
        auth_query = {
            k: v for k, v in request.query_params.items()
            if "key" in k.lower() or "api" in k.lower()
        }
        logger.warning(
            "小艺 A2A 鉴权失败 path=%s mode=%s ua=%r xff=%r 鉴权头=%s query=%s",
            request.url.path,
            config.auth_mode,
            request.headers.get("user-agent"),
            request.headers.get("x-forwarded-for"),
            auth_headers,
            auth_query,
        )
        # 排障用：全部 header + 报文原样记下——是模式1（只带 agent-session-id）还是
        # 模式2（每次带 AK/SK）、方法名与参数是什么，一看便知（Request.body 会缓存，
        # 不影响后续 request.json()）
        try:
            raw_body = (await request.body()).decode("utf-8", "replace")
        except Exception:
            raw_body = "<读取失败>"
        logger.warning(
            "小艺 A2A 鉴权失败明细 headers=%s body=%s",
            dict(request.headers),
            raw_body[:800],
        )
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return _error_response(None, PARSE_ERROR, "JSON 解析失败")

    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return _error_response(None, INVALID_REQUEST, "非法请求对象")
    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method == "initialize":
        return await _handle_initialize(rpc_id, config)
    if method == "notifications/initialized":
        # 规格：HTTP 200 即可，无响应体
        return JSONResponse(None, status_code=200)
    if method == "message/stream":
        return await _handle_message_stream(rpc_id, params)
    if method == "tasks/cancel":
        return await _handle_cancel(rpc_id, body)
    if method == "clearContext":
        return await _handle_clear_context(rpc_id, body)
    if method in ("authorize", "deauthorize", "push"):
        return _error_response(rpc_id, METHOD_NOT_FOUND, f"{method} 尚未开放（二期）")
    return _error_response(rpc_id, METHOD_NOT_FOUND, f"未知方法：{method}")


async def _handle_initialize(rpc_id: Any, config: XiaoyiA2aSetting) -> JSONResponse:
    """模式1 会话分配（规格 §3）：返回 agent-session-id，TTL 7 天。

    返回的是**自签令牌**（见 ``mint_agent_session_id``）：客户端把它放 header
    ``agent-session-id`` 带回即视为已认证，之后不必每次重复签名。配成模式2
    （无状态、每次带凭据）的平台根本不调 initialize，调了也不影响——对话上下文
    仍按 ``params.sessionId`` 映射（见 sessions 服务），与这个 id 无关。
    """
    return JSONResponse(
        _rpc_response(
            rpc_id,
            result={
                "version": "1.0",
                "agentSessionId": mint_agent_session_id(config),
                "agentSessionTtl": str(SESSION_ID_TTL_SECONDS),
            },
        )
    )


async def _handle_message_stream(rpc_id: Any, params: dict[str, Any]) -> StreamingResponse:
    """主入口：接任务 → 后台驱动 AgentRunner → SSE 流式回事件。"""
    task_id = str(params.get("id") or rpc_id or "")
    platform_session_id = str(params.get("sessionId") or "")
    if not task_id or not platform_session_id:
        return _error_response(rpc_id, INVALID_PARAMS, "缺少 id 或 sessionId")

    user_text = _extract_user_text(params)
    if user_text is None:
        return _error_response(rpc_id, INVALID_PARAMS, "消息里没有可处理的文本")

    session_id = await ensure_agent_session(platform_session_id)
    run = new_queued_run(task_id, user_text, session_id)
    hub = get_xiaoyi_a2a_hub()
    await hub.start(run)

    async def event_source():
        # 开场先推 working（等价 IM 通道的「思考中💭」回执）
        yield _sse_frame(_status_event(str(rpc_id), task_id, "working", "正在处理…"))
        artifact_seq = 0
        sent_final = False
        async for ev in _iter_until_sentinel(run.events):
            artifact_seq += 1
            for frame in _map_event(ev, str(rpc_id), task_id, artifact_seq):
                if frame["result"].get("final"):
                    sent_final = True
                yield _sse_frame(frame)
        # 兜底：事件流意外中断（没有终态事件）时补 final——已经发过 final
        # （done/error/cancelled）就绝不再发：final=true 会断开端云通道
        if not sent_final:
            yield _sse_frame(
                _status_event(str(rpc_id), task_id, "completed", "处理完成", final=True)
            )

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _iter_until_sentinel(queue: Any):
    """消费事件队列直到 None 哨兵（运行结束）。"""
    while True:
        ev = await queue.get()
        if ev is None:
            return
        yield ev


def _map_event(
    ev: AgentEvent, rpc_id: str, task_id: str, artifact_seq: int
) -> list[dict[str, Any]]:
    """AgentEvent → A2A 事件帧（映射表见模块文档；终态帧带 final=true）。"""
    if ev.type == "thinking_delta" and ev.delta:
        return [
            _artifact_event(
                rpc_id,
                task_id,
                f"art-{task_id}-think",
                {"kind": "reasoningText", "reasoningText": ev.delta},
            )
        ]
    if ev.type == "text_delta" and ev.delta:
        return [
            _artifact_event(
                rpc_id,
                task_id,
                f"art-{task_id}-text",
                {"kind": "text", "text": ev.delta},
            )
        ]
    if ev.type == "tool_call" and ev.tool_call:
        return [
            _status_event(rpc_id, task_id, "working", f"调用工具：{ev.tool_call.name}")
        ]
    if ev.type == "tool_result" and ev.tool_result:
        label = ev.tool_result.name
        suffix = "（失败）" if ev.tool_result.is_error else ""
        return [_status_event(rpc_id, task_id, "working", f"工具完成：{label}{suffix}")]
    if ev.type == "agent_done":
        frames: list[dict[str, Any]] = []
        if ev.result and ev.result.text:
            frames.append(
                _artifact_event(
                    rpc_id,
                    task_id,
                    f"art-{task_id}-final-{artifact_seq}",
                    {"kind": "text", "text": ev.result.text},
                    append=False,
                    last_chunk=True,
                )
            )
        frames.append(
            _status_event(rpc_id, task_id, "completed", "处理完成", final=True)
        )
        return frames
    if ev.type == "agent_error":
        return [
            _status_event(rpc_id, task_id, "failed", ev.error or "处理失败", final=True)
        ]
    if ev.type == "agent_cancelled":
        return [
            _status_event(rpc_id, task_id, "canceled", "已取消", final=True)
        ]
    # agent_start / thinking 之外的中间事件（context_compacted 等）：不出帧
    return []


async def _handle_cancel(rpc_id: Any, body: dict[str, Any]) -> JSONResponse:
    """tasks/cancel（规格 §5.4）：按 taskId 取消；文档示例 sessionId 在顶层。"""
    task_id = str(body.get("params", {}).get("id") or body.get("id") or "")
    # 文档两处不一致（顶层 vs params.id）；两个位置都找，取第一个非空
    task_id = str(
        (body.get("params") or {}).get("id")
        or body.get("taskId")
        or body.get("id")
        or ""
    )
    hub = get_xiaoyi_a2a_hub()
    was_running = await hub.cancel(task_id)
    if not was_running:
        return JSONResponse(
            _rpc_response(
                rpc_id,
                error={"code": -32001, "message": "task ID 不存在或已结束"},
            )
        )
    return JSONResponse(
        _rpc_response(
            rpc_id,
            result={
                "id": task_id,
                "status": {"state": "canceled"},
            },
        )
    )


async def _handle_clear_context(rpc_id: Any, body: dict[str, Any]) -> JSONResponse:
    """clearContext（规格 §5.4）：该平台 sessionId 换新会话。"""
    session_id = str(
        (body.get("params") or {}).get("sessionId")
        or body.get("sessionId")
        or ""
    )
    if not session_id:
        return _error_response(rpc_id, INVALID_PARAMS, "缺少 sessionId")
    await clear_agent_session(session_id)
    return JSONResponse(
        _rpc_response(rpc_id, result={"status": {"state": "cleared"}})
    )


# ---------------------------------------------------------------------------
# 管理面（挂管理区，见 router.py _ADMIN_ROUTERS）
# ---------------------------------------------------------------------------


def _mask(secret: str) -> str:
    return f"{secret[:6]}****" if secret else ""


@admin_router.get("/config", operation_id="xiaoyi_a2a.config")
async def get_config() -> ApiResponse[XiaoyiA2aConfigView]:
    cfg = cast(XiaoyiA2aSetting, await get_setting_store().get(XiaoyiA2aSetting))
    return ok(
        XiaoyiA2aConfigView(
            enabled=cfg.enabled,
            auth_mode=cfg.auth_mode,
            access_key=cfg.access_key,
            secret_key=cfg.secret_key,
            secret_key_hint=_mask(cfg.secret_key),
            api_key=cfg.api_key,
            api_key_hint=_mask(cfg.api_key),
            api_key_header=cfg.api_key_header,
            session_mode=cfg.session_mode,
        )
    )


@admin_router.put("/config", operation_id="xiaoyi_a2a.config.save")
async def save_config(payload: XiaoyiA2aConfigPayload) -> ApiResponse[XiaoyiA2aConfigView]:
    store = get_setting_store()
    current = cast(XiaoyiA2aSetting, await store.get(XiaoyiA2aSetting))
    cfg = XiaoyiA2aSetting(
        enabled=payload.enabled,
        auth_mode=payload.auth_mode,
        access_key=payload.access_key,
        secret_key=payload.secret_key or current.secret_key,
        api_key=payload.api_key or current.api_key,
        api_key_header=payload.api_key_header or current.api_key_header,
        session_mode=payload.session_mode,
    )
    await store.set(cfg)
    return ok(
        XiaoyiA2aConfigView(
            enabled=cfg.enabled,
            auth_mode=cfg.auth_mode,
            access_key=cfg.access_key,
            secret_key=cfg.secret_key,
            secret_key_hint=_mask(cfg.secret_key),
            api_key=cfg.api_key,
            api_key_hint=_mask(cfg.api_key),
            api_key_header=cfg.api_key_header,
            session_mode=cfg.session_mode,
        )
    )


@admin_router.post("/config/rotate", operation_id="xiaoyi_a2a.config.rotate")
async def rotate_config() -> ApiResponse[XiaoyiA2aConfigRotatedView]:
    """按当前鉴权方式生成新凭据并落库，返回明文（仅此一次）。

    - aksk 模式：生成一对新的 accessKey / secretKey；
    - apikey 模式：生成一枚新的 api_key。
    旧凭据随即失效，用户需把小艺开放平台里的认证信息同步更新。
    """
    store = get_setting_store()
    current = cast(XiaoyiA2aSetting, await store.get(XiaoyiA2aSetting))
    if current.auth_mode == "apikey":
        access_key = current.access_key
        secret_key = current.secret_key
        api_key = generate_api_key()
    else:
        access_key = generate_access_key()
        secret_key = generate_secret_key()
        api_key = current.api_key
    cfg = XiaoyiA2aSetting(
        enabled=current.enabled,
        auth_mode=current.auth_mode,
        access_key=access_key,
        secret_key=secret_key,
        api_key=api_key,
        api_key_header=current.api_key_header,
        session_mode=current.session_mode,
    )
    await store.set(cfg)
    return ok(
        XiaoyiA2aConfigRotatedView(
            enabled=cfg.enabled,
            auth_mode=cfg.auth_mode,
            session_mode=cfg.session_mode,
            access_key=access_key,
            secret_key=secret_key,
            secret_key_hint=_mask(secret_key),
            api_key=api_key,
            api_key_hint=_mask(api_key),
            api_key_header=cfg.api_key_header,
        )
    )
