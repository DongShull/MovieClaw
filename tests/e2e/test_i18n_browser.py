"""Real Next + API + database language flow. Run with pytest -m integration."""

from __future__ import annotations

import pytest

from . import test_theme_device_class_browser as theme

ADMIN = theme.ADMIN
stack = theme.stack
browser = theme.browser

pytestmark = pytest.mark.integration


def test_language_end_to_end(stack, browser):
    from playwright.sync_api import expect

    artifacts = theme.REPO / ".tmp-uidev/i18n"
    artifacts.mkdir(parents=True, exist_ok=True)
    base = stack
    english = browser.new_context(
        locale="en-US", timezone_id="America/New_York", viewport={"width": 1280, "height": 900}
    )
    page = english.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{base}/setup")
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(page.get_by_role("button", name="Get started", exact=True)).to_be_visible()
    page.get_by_role("button", name="Get started", exact=True).click()
    page.get_by_placeholder("Administrator username").fill(ADMIN["username"])
    page.get_by_placeholder("Password (at least 8 characters)", exact=True).fill(ADMIN["password"])
    page.get_by_placeholder("Confirm password", exact=True).fill("mismatched")
    page.get_by_role("button", name="Create account and continue").click()
    expect(page.get_by_text("Passwords do not match", exact=True)).to_be_visible()
    page.get_by_placeholder("Confirm password", exact=True).fill(ADMIN["password"])
    page.get_by_role("button", name="Create account and continue").click()
    page.wait_for_url(f"{base}/")
    english.request.post(f"{base}/api/v1/auth/logout")

    page.goto(f"{base}/login?next=/settings/appearance")
    expect(page.get_by_placeholder("Username", exact=True)).to_be_visible()
    page.get_by_placeholder("Username", exact=True).fill(ADMIN["username"])
    page.get_by_placeholder("Password", exact=True).fill("wrong-password")
    with page.expect_response(
        lambda r: r.url.endswith("/auth/login") and r.request.method == "POST"
    ) as response:
        page.get_by_role("button", name="Sign in", exact=True).last.click()
    body = response.value.json()
    assert body["message_key"] == "errors.auth.wrongPassword"
    assert body["message"] == "Incorrect username or password"
    assert response.value.request.headers["accept-language"] == "en"
    assert response.value.request.headers["x-movieclaw-timezone"] == "America/New_York"
    expect(page.get_by_text("Incorrect username or password", exact=True)).to_be_visible()
    page.screenshot(path=str(artifacts / "login-en.png"), full_page=True)

    # No document reload: form state and a window marker survive switching.
    page.evaluate("window.__languageMarker = 42")
    page.get_by_role("combobox", name="Language", exact=True).select_option("zh-Hans")
    expect(page.locator("html")).to_have_attribute("lang", "zh-Hans")
    expect(page.get_by_placeholder("用户名", exact=True)).to_have_value(ADMIN["username"])
    assert page.evaluate("window.__languageMarker") == 42
    page.get_by_placeholder("密码", exact=True).fill("wrong-password")
    page.get_by_role("button", name="登录", exact=True).last.click()
    expect(page.get_by_text("用户名或密码错误", exact=True)).to_be_visible()
    page.screenshot(path=str(artifacts / "login-zh.png"), full_page=True)
    page.get_by_placeholder("密码", exact=True).fill(ADMIN["password"])
    page.get_by_role("button", name="登录", exact=True).last.click()
    page.wait_for_url(f"{base}/settings/appearance")
    group = page.get_by_test_id("language-settings")
    expect(group).to_be_visible(timeout=30000)
    page.evaluate("window.__languageMarker = 84")
    group.locator('input[value="en"]').check()
    expect(group.locator('input[value="en"]')).to_be_enabled()
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(page.get_by_role("heading", name="Appearance and language", exact=True)).to_be_visible()
    assert page.evaluate("window.__languageMarker") == 84
    saved = english.request.get(f"{base}/api/v1/ui/preferences").json()["data"]
    assert saved["language"] == "en"
    page.reload()
    expect(group.locator('input[value="en"]')).to_be_checked()
    expect(page.locator("html")).to_have_attribute("lang", "en")
    page.wait_for_function(
        """() => [...document.querySelectorAll('div[aria-hidden="true"][class*="z-[100]"]')]
          .every(element => getComputedStyle(element).opacity === "0")"""
    )
    page.screenshot(path=str(artifacts / "appearance-en.png"), full_page=True)

    # New browser starts in Chinese, but this account's explicit language wins at login.
    chinese = browser.new_context(
        locale="zh-CN", timezone_id="Asia/Shanghai", viewport={"width": 390, "height": 844}
    )
    other = chinese.new_page()
    other.goto(f"{base}/login?next=/settings/appearance")
    expect(other.locator("html")).to_have_attribute("lang", "zh-Hans")
    other.get_by_placeholder("用户名", exact=True).fill(ADMIN["username"])
    other.get_by_placeholder("密码", exact=True).fill(ADMIN["password"])
    other.get_by_role("button", name="登录", exact=True).last.click()
    other.wait_for_url(f"{base}/settings/appearance")
    expect(other.locator("html")).to_have_attribute("lang", "en")
    other.get_by_test_id("language-settings").locator('input[value="auto"]').check()
    expect(other.locator("html")).to_have_attribute("lang", "zh-Hans")
    expect(other.get_by_test_id("language-settings").locator('input[value="auto"]')).to_be_enabled()
    assert not any(cookie["name"] == "mc_locale" for cookie in chinese.cookies())
    other.reload()
    expect(other.locator("html")).to_have_attribute("lang", "zh-Hans")
    assert other.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors
    chinese.close()
    english.close()


def test_development_pseudo_and_localized_manifest(stack, browser):
    from playwright.sync_api import expect

    context = browser.new_context(locale="en-US")
    context.request.post(f"{stack}/api/v1/auth/bootstrap", data=ADMIN)
    context.clear_cookies()
    page = context.new_page()
    page.goto(f"{stack}/login?add=1&locale=pseudo")
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(page.locator("body")).to_contain_text("Šîğñ")
    assert not any(cookie["value"] == "pseudo" for cookie in context.cookies())
    response = context.request.get(f"{stack}/manifest.webmanifest")
    assert {"accept-language", "cookie"} <= {
        value.strip().lower() for value in response.headers["vary"].split(",")
    }
    manifest = response.json()
    assert manifest["lang"] == "en"
    assert manifest["description"] == "Your media tracking workspace"
    page.goto(f"{stack}/login?add=1")
    expect(page.get_by_placeholder("Username", exact=True)).to_be_visible()
    page.get_by_role("combobox", name="Language", exact=True).select_option("zh-Hans")
    expect(page.locator("html")).to_have_attribute("lang", "zh-Hans")
    translated = context.request.get(f"{stack}/manifest.webmanifest").json()
    assert translated["lang"] == "zh-Hans"
    assert translated["description"] == "液态玻璃风格的影视追踪工作台"
    context.close()
