"""Run the built MovieClawEnglish scheme against an isolated real API and SQLite.

Build with xcodebuild build-for-testing first; set APPLE_I18N_XCTESTRUN to its
MovieClawEnglish_*.xctestrun and APPLE_I18N_SIMULATOR to an iPhone simulator UUID.
"""

from __future__ import annotations

import os
import plistlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from . import test_theme_device_class_browser as theme

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("platform", ["ios", "tvos"])
def test_apple_language_flow(tmp_path, platform):
    prefix = "APPLE_I18N_TV" if platform == "tvos" else "APPLE_I18N"
    original = os.environ.get(f"{prefix}_XCTESTRUN")
    simulator = os.environ.get(f"{prefix}_SIMULATOR")
    if not original or not simulator:
        pytest.skip(f"Requires a built English scheme and an {platform} simulator")
    source = Path(original).resolve()
    config = plistlib.loads(source.read_bytes())
    port = theme._free_port()
    base = f"http://127.0.0.1:{port}"
    data = tmp_path / "data"
    data.mkdir()
    database = data / "app.db"
    env = {
        **os.environ,
        "APP_ENV": "local",
        "APP_RELOAD": "false",
        "APP_PORT": str(port),
        "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        "METADATA_DIR": str(data / "metadata"),
        "LOG_DIR": str(data / "logs"),
        "SECRET_KEY_FILE": str(data / ".secret_key"),
        "E2E_TMDB_LOG": str(tmp_path / "tmdb.log"),
    }
    run = source.with_name("i18n-fixture.xctestrun")
    artifacts = theme.REPO / ".tmp-uidev/i18n/apple"
    if platform == "tvos":
        artifacts = artifacts / "tvos"
    artifacts.mkdir(parents=True, exist_ok=True)
    with (artifacts / "api.log").open("w") as api_log:
        api = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("_api_launcher.py"))],
            env=env,
            cwd=tmp_path,
            stdout=api_log,
            stderr=subprocess.STDOUT,
        )
        try:
            theme._wait_http(f"{base}/api/v1/auth/bootstrap", 90)
            assert httpx.post(f"{base}/api/v1/auth/bootstrap", json=theme.ADMIN).is_success
            for name in (
                ("MovieClawTVUITests",)
                if platform == "tvos"
                else ("MovieClawTests", "MovieClawUITests")
            ):
                config[name]["EnvironmentVariables"].update(
                    {
                        "MC_TEST_I18N": "1",
                        "MC_TEST_SERVER": base,
                        "MC_TEST_PASSWORD": theme.ADMIN["password"],
                    }
                )
            run.write_bytes(plistlib.dumps(config))
            with (artifacts / "xcodebuild.log").open("w") as log:
                result = subprocess.run(
                    [
                        "xcodebuild",
                        "test-without-building",
                        "-xctestrun",
                        str(run),
                        "-destination",
                        f"platform={'tvOS' if platform == 'tvos' else 'iOS'} Simulator,"
                        f"id={simulator}",
                        "-parallel-testing-enabled",
                        "NO",
                        "-collect-test-diagnostics",
                        "never",
                    ],
                    cwd=theme.REPO,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=600,
                )
            output = (artifacts / "xcodebuild.log").read_text()
            assert result.returncode == 0, output[-12000:]
            expected = (
                [
                    "testEnglishAccountAndAboutUseRealServer",
                    "testTraditionalChineseUsesSimplifiedAlias",
                ]
                if platform == "tvos"
                else [
                    "testFreshSignInUsesEnglishAndChineseMessages",
                    "testEnglishNetworkAndValidationErrors",
                    "testEnglishPluralVariations",
                ]
            )
            for test_name in expected:
                assert f"{test_name}]' passed" in output, output[-12000:]
            with sqlite3.connect(database) as connection:
                devices = connection.execute(
                    "SELECT locale,timezone FROM login_device WHERE kind=?", (platform,)
                ).fetchall()
            assert devices
            assert all(locale in {"en", "zh-Hans"} and timezone for locale, timezone in devices)
        finally:
            run.unlink(missing_ok=True)
            theme._stop(api)
            try:
                api.wait(timeout=15)
            except subprocess.TimeoutExpired:
                api.kill()
