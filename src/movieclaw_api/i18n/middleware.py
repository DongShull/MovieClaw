from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from movieclaw_api.i18n import current_locale, negotiate, normalize_locale


class LocaleMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        locale = normalize_locale(request.cookies.get("mc_locale", "")) or negotiate(
            request.headers.get("accept-language")
        )
        token = current_locale.set(locale)

        async def localized_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Language"] = locale
                headers.add_vary_header("Accept-Language, Cookie")
            await send(message)

        try:
            await self.app(scope, receive, localized_send)
        finally:
            current_locale.reset(token)
