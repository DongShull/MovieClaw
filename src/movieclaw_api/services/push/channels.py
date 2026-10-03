"""推送通道：官方中继 + 管理员加的自建中继（docs/design/cloud-push.md §3）。

- **每个通道一个启用开关**。官方通道的地址和凭证来自 MovieClaw Cloud，连接之后默认启用；
  未连接时显示为「未激活」。
- **按设备的 Bundle ID 自动选通道**：可用通道里按「官方在前、自建按列表顺序」取第一个
  ``topics`` 包含它的；整批失败时分发器换下一个。
- **能力快照落库**：每个通道的 ``GET /v1/info`` 定期刷新并存进配置域，重启时不用等它
  就能路由。官方通道没拉到过时按 ``io.movieclaw.app`` 处理。
- **运行期状态只在内存里**：最近一次成功、最近的错误、当日额度、限额解除时间——重启
  清零无妨，下一次推送就有了。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from movieclaw_api.services.push import relay
from movieclaw_api.services.push.relay import RelayError
from movieclaw_api.settings import get_setting_store
from movieclaw_api.settings.cloud import CloudSetting, PushChannelsSetting, PushRelay, RelayInfo
from movieclaw_db.models import utcnow

logger = logging.getLogger("movieclaw_api.push.channels")

OFFICIAL_ID = "official"
OFFICIAL_NAME = "MovieClaw 官方推送"
#: 官方 App 的 Bundle ID：官方通道没拉到过 /v1/info 时按它路由，设备覆盖里据此认出官方版
OFFICIAL_TOPIC = "io.movieclaw.app"
#: 实例能用的中继协议主版本
SUPPORTED_PROTOCOL = 1
#: /v1/info 多久刷新一次
INFO_TTL = timedelta(hours=6)


@dataclass
class ChannelRuntime:
    """一个通道的运行期状态（只在内存里）。"""

    last_success_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    consecutive_failures: int = 0
    quota: dict | None = None
    blocked_until: datetime | None = None
    blocked_message: str | None = None

    def record_success(self, quota: dict | None) -> None:
        self.last_success_at = utcnow()
        self.consecutive_failures = 0
        self.last_error = None
        if quota is not None:
            self.quota = quota

    def record_failure(self, message: str) -> None:
        self.consecutive_failures += 1
        self.last_error = message
        self.last_error_at = utcnow()

    def blocked(self) -> bool:
        return self.blocked_until is not None and self.blocked_until > utcnow()


_runtime: dict[str, ChannelRuntime] = {}


def runtime(channel_id: str) -> ChannelRuntime:
    return _runtime.setdefault(channel_id, ChannelRuntime())


def reset_runtime() -> None:
    """测试用：清空运行期状态。"""
    _runtime.clear()


@dataclass
class Channel:
    """一个推送通道此刻的样子（每次推送前现算，配置随改随生效）。"""

    id: str
    kind: str  # official / custom
    name: str
    enabled: bool
    urls: list[str]
    bearer: str | None
    info: RelayInfo | None
    lan_direct: bool
    #: 能不能发：不能发时 ``problem`` 说明原因（给设置页）
    usable: bool
    problem: str | None = None
    #: 打码后的自建中继令牌
    token_hint: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def topics(self) -> list[str]:
        if self.info and self.info.topics:
            return list(self.info.topics)
        return [OFFICIAL_TOPIC] if self.kind == "official" else []

    def supports(self, topic: str, push_type: str = "alert") -> bool:
        if topic not in self.topics:
            return False
        return not (self.info and self.info.types and push_type not in self.info.types)

    @property
    def max_batch(self) -> int:
        return max(1, min(100, self.info.max_batch if self.info else 100))


def mask_token(token: str) -> str | None:
    """自建中继令牌打码：保留前缀和前 4 位（``mcpush_a1b2…``）。"""
    if not token:
        return None
    head = token.split("_", 2)
    if len(head) == 3 and head[0] == "mcpush":
        return f"mcpush_{head[1][:4]}…"
    return token[:4] + "…"


def _custom_problem(r: PushRelay) -> str | None:
    if r.info is None:
        return "还没连上过这个中继"
    if r.info.protocol != SUPPORTED_PROTOCOL:
        return f"中继的协议版本是 {r.info.protocol}，这个版本只支持 {SUPPORTED_PROTOCOL}"
    if r.info.auth_mode == "issuer":
        return "这个中继要签发方的令牌，这里还不能用"
    if r.info.auth_mode == "static" and not r.token:
        return "缺少中继令牌"
    return None


async def load_channels() -> list[Channel]:
    """官方通道 + 自建中继（按列表顺序）。"""
    from movieclaw_api.services.cloud import get_cloud_service

    store = get_setting_store()
    cloud = await store.get(CloudSetting)
    config = await store.get(PushChannelsSetting)
    service = get_cloud_service()

    bearer = service.official_bearer(cloud)
    endpoints = service.push_endpoints(cloud)
    if not cloud.connected:
        problem = "正在连接 MovieClaw Cloud" if service.pairing else "未连接 MovieClaw Cloud"
    elif cloud.unsupported_message:
        problem = cloud.unsupported_message
    elif "push" not in cloud.scopes:
        problem = "MovieClaw Cloud 没有授予推送权限"
    elif bearer is None:
        problem = "令牌已过期：连不上 MovieClaw Cloud，官方推送暂停"
    elif not endpoints:
        problem = "MovieClaw Cloud 没有给出推送中继地址"
    else:
        problem = None
    official = Channel(
        id=OFFICIAL_ID,
        kind="official",
        name=OFFICIAL_NAME,
        enabled=config.official_enabled,
        urls=endpoints,
        bearer=bearer,
        info=config.official_info,
        lan_direct=False,
        usable=config.official_enabled and problem is None,
        problem=problem,
    )
    channels = [official]
    for r in config.relays:
        problem = _custom_problem(r)
        channels.append(
            Channel(
                id=r.id,
                kind="custom",
                name=r.name or r.url,
                enabled=r.enabled,
                urls=[r.url],
                bearer=r.token or None if (r.info and r.info.auth_mode != "none") else None,
                info=r.info,
                lan_direct=True,
                usable=r.enabled and problem is None,
                problem=problem,
                token_hint=mask_token(r.token),
            )
        )
    return channels


def route(channels: list[Channel], topic: str, push_type: str = "alert") -> list[Channel]:
    """能推这个 Bundle ID 的可用通道，按优先顺序。"""
    return [c for c in channels if c.usable and c.supports(topic, push_type)]


def official_relay_status() -> dict | None:
    """上报用：官方中继能否连通、最近一次成功推送的时间。没用过就不报。"""
    state = _runtime.get(OFFICIAL_ID)
    if state is None or (state.last_success_at is None and state.last_error is None):
        return None
    status: dict = {"reachable": state.consecutive_failures == 0}
    if state.last_success_at is not None:
        status["last_success_at"] = state.last_success_at.replace(microsecond=0).isoformat() + "Z"
    return status


# ----------------------------------------------------------------------
# /v1/info 快照
# ----------------------------------------------------------------------


async def _fetch_first(urls: list[str], *, lan_direct: bool) -> RelayInfo:
    error: RelayError | None = None
    for url in urls:
        try:
            return await relay.fetch_info(url, lan_direct=lan_direct)
        except RelayError as exc:
            error = exc
    raise error or RelayError("没有可用的中继地址", retryable=False)


async def refresh_official_info() -> RelayInfo | None:
    """拉官方中继的 /v1/info 存进快照；失败保留旧的。"""
    from movieclaw_api.services.cloud import get_cloud_service

    store = get_setting_store()
    cloud = await store.get(CloudSetting)
    urls = get_cloud_service().push_endpoints(cloud)
    if not cloud.connected or not urls:
        return None
    try:
        info = await _fetch_first(urls, lan_direct=False)
    except RelayError as exc:
        logger.warning("读取官方推送中继的能力失败：%s", exc.message)
        return None
    config = (await store.get(PushChannelsSetting)).model_copy(deep=True)
    config.official_info = info
    await store.set(config)
    return info


async def refresh_relay_info(relay_id: str) -> RelayInfo:
    """重新拉自建中继的 /v1/info；失败抛 RelayError（设置页显示原因）。"""
    store = get_setting_store()
    config = (await store.get(PushChannelsSetting)).model_copy(deep=True)
    target = next((r for r in config.relays if r.id == relay_id), None)
    if target is None:
        raise KeyError(relay_id)
    try:
        info = await relay.fetch_info(target.url)
    except RelayError as exc:
        runtime(relay_id).record_failure(exc.message)
        raise
    target.info = info
    await store.set(config)
    return info


async def refresh_stale_infos() -> None:
    """把超过 6 小时（或从没拉到过）的快照刷新一遍。出错只记日志。"""
    store = get_setting_store()
    config = await store.get(PushChannelsSetting)
    cloud = await store.get(CloudSetting)
    now = utcnow()

    def stale(info: RelayInfo | None) -> bool:
        return info is None or info.fetched_at is None or now - info.fetched_at > INFO_TTL

    if cloud.connected and config.official_enabled and stale(config.official_info):
        await refresh_official_info()
    for r in list(config.relays):
        if r.enabled and stale(r.info):
            with contextlib.suppress(RelayError, KeyError):
                await refresh_relay_info(r.id)


def new_relay_id() -> str:
    return "r_" + secrets.token_hex(6)


_refresh_task: asyncio.Task[None] | None = None


async def _refresh_loop() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await refresh_stale_infos()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- 刷新失败下一轮再来
            logger.exception("刷新推送中继能力时出错")
        await asyncio.sleep(1800)


def start_refresh_loop() -> None:
    """应用启动后起一个后台循环：每半小时检查一次快照是否过期。"""
    global _refresh_task
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = asyncio.get_running_loop().create_task(_refresh_loop())


async def stop_refresh_loop() -> None:
    global _refresh_task
    task, _refresh_task = _refresh_task, None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
