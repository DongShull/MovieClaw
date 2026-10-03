"""下载器用途配置域（「设置 → 下载器」里按用途指定）。

下载器列表（``downloader_client`` 表）只回答「怎么连上」，这里回答「哪个用途
用哪台」。订阅投递与刷流取种本是两条独立链路，此前共用 ``is_default`` 那台：
一台 Transmission 刷流、一台 qBittorrent 拉正片是常见部署，共用默认就必然
有一边被挤到不合用的客户端上，也互相挤占队列。

两个字段都留空 = 两条链路都跟随默认下载器，与未引入本配置域时的行为完全一致。

为什么不是新表：见 ``movieclaw_db.models.app_setting.AppSetting`` 的取舍说明 ——
集成配置走「按域存一条 JSON」，新增一个域 = 一个 Pydantic 模型 + 注册，
零数据库迁移。
"""

from __future__ import annotations

from pydantic import Field

from movieclaw_api.settings.base import SettingSchema, register_setting

DOWNLOADER_USAGE_NAMESPACE = "downloader.usage"


@register_setting(namespace=DOWNLOADER_USAGE_NAMESPACE, title="下载器用途")
class DownloaderUsageSetting(SettingSchema):
    """两条链路各自指定的下载器 id；None = 跟随默认下载器。"""

    subscription_downloader_id: int | None = Field(
        default=None,
        description="订阅投递使用的下载器 id；空 = 跟随默认下载器",
    )
    boost_downloader_id: int | None = Field(
        default=None,
        description="刷流取种使用的下载器 id；空 = 跟随默认下载器",
    )
