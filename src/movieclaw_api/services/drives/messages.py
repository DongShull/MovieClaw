"""MovieClaw 自己抛出的网盘错误：带消息键（接口按读者语言渲染），``message`` 是中文原文。

插件报来的错误只有原话，原样透传。
"""

from __future__ import annotations

from typing import Any

from movieclaw_api.i18n.stored import reference, stored_fields


def keyed(cls: type[Exception], key: str, /, *args: Any, **params: Any) -> Exception:
    """构造带消息键的错误：``exception_fields`` 据 ``message_key`` 渲染给读者。"""
    fields = stored_fields(key, **params)
    exc = cls(fields["message"], *args)
    exc.message_key = key  # type: ignore[attr-defined]
    exc.message_params = fields["message_params"]  # type: ignore[attr-defined]
    return exc


def error_reference(exc: BaseException) -> dict | str:
    """错误的文案引用：自家错误按键，插件或系统的原话原样。"""
    key = getattr(exc, "message_key", None)
    if key:
        params = getattr(exc, "message_params", None) or getattr(exc, "params", None) or {}
        return reference(key, **params)
    return getattr(exc, "message", None) or str(exc)
