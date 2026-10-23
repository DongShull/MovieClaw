"""Render stored message references without rewriting legacy Chinese history."""

from functools import lru_cache

from fastapi.encoders import jsonable_encoder

from movieclaw_api.i18n import messages, parse_message, t, use_locale
from movieclaw_api.i18n.metadata import localized_text


@lru_cache(maxsize=1)
def legacy_keys() -> dict[str, str]:
    candidates: dict[str, list[str]] = {}
    for key, source in messages("zh-Hans").items():
        if all(isinstance(node, str) for node in parse_message(source)):
            candidates.setdefault("".join(parse_message(source)), []).append(key)
    # Equal Chinese can mean different things; only infer an unambiguous key.
    return {source: keys[0] for source, keys in candidates.items() if len(keys) == 1}


def stored_message(text: str, key: str | None = None, params: dict | None = None) -> str:
    key = key or legacy_keys().get(text)
    if not key or key not in messages("zh-Hans"):
        return text

    return t(key, **{name: render_value(value) for name, value in (params or {}).items()})


def reference(key: str, **params) -> dict:
    """A JSON-safe reference for a nested reason or label in stored parameters."""
    return {"message_key": key, "params": jsonable_encoder(params, custom_encoder={Exception: str})}


def render_value(value):
    if isinstance(value, dict) and "message_text" in value:
        return localized_text(value["message_text"], value.get("message_localized"))
    if isinstance(value, dict) and "message_key" in value:
        return stored_message(
            value.get("message", ""),
            value["message_key"],
            value.get("params", value.get("message_params")),
        )
    if isinstance(value, dict) and "message_list" in value:
        return t(value["separator_key"]).join(render_value(item) for item in value["message_list"])
    return value


def stored_fields(key: str, **params) -> dict:
    """Keep a Chinese rollback value beside the reference in a JSON history field."""
    params = jsonable_encoder(params, custom_encoder={Exception: str})
    with use_locale("zh-Hans"):
        source = stored_message("", key, params)
    return {"message": source, "message_key": key, "message_params": params}


def localize_fields(value):
    """Copy marked JSON history; never change a cached or ORM-owned dictionary."""
    if isinstance(value, list):
        return [localize_fields(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {name: localize_fields(child) for name, child in value.items()}
    for field in ("message", "label", "title"):
        text = value.get(field)
        key = value.get(f"{field}_key")
        if isinstance(text, str) and (field == "message" or key):
            result[field] = stored_message(
                text, key, value.get("message_params", value.get("params"))
            )
    return result
