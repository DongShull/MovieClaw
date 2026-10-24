"""Drive UI stack with the existing loopback metadata service used by real scans."""

from tests.e2e._ai_settings_api_launcher import app  # noqa: F401

from movieclaw_api.services import media_discover
from movieclaw_api.services.library import scan

scan.get_tmdb_client = media_discover.get_tmdb_client
