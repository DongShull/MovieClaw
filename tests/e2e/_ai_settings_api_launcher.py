"""Keep AI settings services real; route external search HTTP to a loopback provider."""

import os
from urllib.parse import urlsplit

import httpx
from tests.api.test_llm import _FakeProtocol
from tests.e2e._smart_api_launcher import app

from movieclaw_api.services import llm_config, web_search_config
from movieclaw_api.settings import LlmDefaultsSetting, get_setting_store
from movieclaw_llm.protocols import PROTOCOLS

upstream = os.environ["E2E_SEARCH_UPSTREAM"]
assert urlsplit(upstream).hostname == "127.0.0.1"


class SearchTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.transport = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        assert request.url.host in {"api.tavily.com", "api.exa.ai", "api.search.brave.com"}
        redirected = httpx.Request(
            request.method,
            upstream + request.url.raw_path.decode(),
            headers=request.headers,
            content=await request.aread(),
            extensions=request.extensions,
        )
        return await self.transport.handle_async_request(redirected)

    async def aclose(self):
        await self.transport.aclose()


web_search_config.search_transport = lambda **kwargs: SearchTransport()


@app.post("/api/v1/__lab/ai/stale-defaults")
async def stale_defaults():
    await get_setting_store().set(
        LlmDefaultsSetting(
            agent_model="retired-agent-model", subtitle_model="retired-subtitle-model"
        )
    )
    return {"ok": True}


# Dummy ChatGPT grants stay inside this fixture; the protocol is the external-service seam.
PROTOCOLS["openai_chatgpt"] = _FakeProtocol
revocation_confirmed = True


async def revoke_fixture_credentials(credentials):
    return revocation_confirmed


llm_config.revoke_credentials = revoke_fixture_credentials


@app.post("/api/v1/__lab/ai/revocation/{confirmed}")
async def set_revocation(confirmed: bool):
    global revocation_confirmed
    revocation_confirmed = confirmed
    return {"ok": True}
