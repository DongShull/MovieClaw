"""MovieClaw 对外 API 模型的公共时间契约。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel as PydanticBaseModel
from pydantic import field_serializer


def utc_isoformat(value: datetime) -> str:
    """把时间规范化为带 ``+00:00`` 的 UTC ISO 字符串。

    DAL 按项目约定保存 naive UTC；API 边界在这里补回时区。若上游已经携带
    时区，则先转换到 UTC，保证接口永远使用同一种可移植时间表示。展示语言
    和用户时区属于前端职责，统一由 ``apps/web/lib/time.ts`` 处理。
    """
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat()


def normalize_utc_iso(value: str | None) -> str | None:
    """规范化历史配置中的 ISO 时间；无 offset 的旧值按项目约定视为 UTC。"""
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return utc_isoformat(parsed)


class BaseModel(PydanticBaseModel):
    """所有 API 请求/响应模型的基类。

    通配 field serializer 会覆盖直接字段及容器内的 ``datetime``。未来新增
    响应模型只要继承本类，就不会再把数据库的 naive UTC 输出成浏览器会误认
    作本地时间的裸 ISO 字符串。
    """

    @field_serializer("*", check_fields=False, when_used="json")
    def _serialize_api_value(self, value: Any) -> Any:
        # 标量直接放行：这个钩子挂在每个响应模型的每个字段上，列表接口一次
        # 就是上万次调用，逐个走下面的 isinstance 链是纯开销
        if value.__class__ in _PASSTHROUGH:
            return value
        return _serialize_datetimes(value)


#: 不可能含 datetime 的精确类型（按 ``__class__`` 精确匹配：datetime 是 date
#: 的子类，不会被 date 误放行；StrEnum 等子类照常走下面的判断）
_PASSTHROUGH = frozenset({str, int, float, bool, type(None), date})


def _serialize_datetimes(value: Any) -> Any:
    """递归处理 datetime 容器；嵌套 API 模型会继续使用自己的公共基类。"""
    if value.__class__ in _PASSTHROUGH:
        return value
    if isinstance(value, datetime):
        return utc_isoformat(value)
    if isinstance(value, dict):
        return {key: _serialize_datetimes(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_datetimes(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_serialize_datetimes(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return type(value)(_serialize_datetimes(item) for item in value)
    return value
