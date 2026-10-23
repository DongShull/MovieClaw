"""Render pure smart decisions at the reader boundary, without mutating the result."""

from movieclaw_api.i18n.stored import render_value
from movieclaw_matcher.smart_memory import Resolved


def smart_presentation(resolved: Resolved) -> dict:
    view = resolved.model_dump()
    for field, message in resolved.messages.items():
        view[field] = render_value(message) if message is not None else None
    if resolved.question is not None:
        view["question"].update(
            {field: render_value(message) for field, message in resolved.question.messages.items()}
        )
    for option, rendered in zip(resolved.scope_options, view["scope_options"], strict=True):
        if option.label_message is not None:
            rendered["label"] = render_value(option.label_message)
    return view
