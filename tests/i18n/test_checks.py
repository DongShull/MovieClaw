from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

CHECK = Path(__file__).resolve().parents[2] / "scripts/i18n/check.py"
spec = importlib.util.spec_from_file_location("i18n_check", CHECK)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False))

    for directory in ("apps/web/locales", "src/movieclaw_api/locales"):
        base = tmp_path / directory
        write(base / "zh-Hans/common.json", {"greeting": "你好 {count}"})
        write(base / "en/common.json", {"greeting": "Hello {count}"})
        write(
            base / "_meta/common.json",
            {
                "greeting": {"sourceHash": hashlib.sha256("你好 {count}".encode()).hexdigest()},
            },
        )
    write(tmp_path / "scripts/i18n/migrations.json", {"common.greeting": "你好 {count}"})
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.check_catalogs() == []
    return tmp_path, write


def test_rejects_stale_english_and_changed_chinese(catalog):
    root, write = catalog
    write(root / "apps/web/locales/zh-Hans/common.json", {"greeting": "您好 {count}"})
    errors = check.check_catalogs()
    assert "Stale English: common.greeting" in errors
    assert "Chinese changed during migration: common.greeting" in errors


def test_rejects_missing_keys_parameters_and_chinese_in_english(catalog):
    root, write = catalog
    write(root / "apps/web/locales/en/common.json", {"greeting": "你好，{name}", "extra": "Extra"})
    errors = check.check_catalogs()
    assert any("key sets differ" in error for error in errors)
    assert "Parameter mismatch: common.greeting" in errors
    assert "English contains Chinese text/punctuation: common.greeting" in errors
    assert "Cross-platform English mismatch: common.greeting" in errors


def test_rejects_unknown_server_key(catalog):
    root, _ = catalog
    (root / "src/movieclaw_api/example.py").write_text('t("common.missing")')
    assert any("unknown key common.missing" in error for error in check.check_catalogs())


def test_unused_keys_are_review_hints_and_glossary_is_enforced(catalog):
    root, write = catalog
    assert any("common.greeting" in warning for warning in check.unused_keys())
    (root / "src/movieclaw_api/example.py").write_text('t("common.greeting", count=1)')
    assert not any("src/movieclaw_api" in warning for warning in check.unused_keys())
    write(root / "scripts/i18n/terms.json", {"你好 {count}": ["greetings {count}"]})
    assert "Glossary mismatch: common.greeting" in check.check_catalogs()


def test_ratchet_excludes_logs_and_honors_swift_data_exemptions(catalog, monkeypatch):
    root, _ = catalog
    monkeypatch.setattr(check.subprocess, "check_output", lambda *args, **kwargs: b"{}")
    (root / "src/movieclaw_api/example.py").write_text(
        'logger.info("中文日志")\nraise BadRequestException("需要登录")\n'
    )
    swift = root / "apps/apple/Shared/UI.swift"
    swift.parent.mkdir(parents=True)
    swift.write_text('Text("展示")\n// i18n-ignore: protocol data\nlet data = "中文数据"\n')
    tests = root / "apps/apple/MovieClawTests/Example.swift"
    tests.parent.mkdir(parents=True)
    tests.write_text('let expected = "中文断言"')
    assert check.baseline() == {"apps/apple/Shared/UI.swift": 1, "src/movieclaw_api/example.py": 1}
