from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts/i18n"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


apple = load("apple_checks", "apple_catalogs.py")
merger = load("catalog_merger", "xcstrings-merge.py")


@pytest.fixture
def catalog(tmp_path):
    source = "%lld 小时 %lld 分钟"

    def translation(value):
        return {"stringUnit": {"state": "translated", "value": value}}

    entry = {
        "extractionState": "manual",
        "localizations": {
            "zh-Hans": translation(source),
            "zh-Hant": translation(source),
            "en": translation("%1$lld hr %2$lld min"),
        },
    }
    base = tmp_path / "apps/apple"
    (base / "Shared").mkdir(parents=True)
    metadata = {
        "Shared/Localizable.xcstrings": {
            "units.duration.hoursMinutes": {
                "sourceHash": hashlib.sha256(source.encode()).hexdigest(),
                "parameters": ["hours", "minutes"],
            }
        }
    }
    (base / "Localization.meta.json").write_text(json.dumps(metadata))
    path = base / "Shared/Localizable.xcstrings"
    value = {"sourceLanguage": "zh-Hans", "strings": {"units.duration.hoursMinutes": entry}}
    path.write_text(json.dumps(value))
    return tmp_path, path, value, entry


def validate(root):
    return apple.check_apple(
        root,
        {"units.duration.hoursMinutes": "{hours} hr {minutes} min"},
        {"units.duration.hoursMinutes": {"hours", "minutes"}},
    )


def test_catalog_maps_positional_printf_to_web_parameters(catalog):
    root, _, _, _ = catalog
    assert validate(root) == []


@pytest.mark.parametrize(
    "change,expected",
    [
        ("source", "Stale Apple English"),
        ("alias", "simplified alias differs"),
        ("parameters", "printf parameters differ"),
        ("english", "Apple/Web English differs"),
    ],
)
def test_catalog_rejects_stale_alias_parameters_and_cross_platform_drift(catalog, change, expected):
    root, path, value, entry = catalog
    languages = entry["localizations"]
    if change == "source":
        languages["zh-Hans"]["stringUnit"]["value"] = "变化"
    elif change == "alias":
        languages["zh-Hant"]["stringUnit"]["value"] = "不同"
    elif change == "parameters":
        languages["en"]["stringUnit"]["value"] = "%lld hr"
    else:
        languages["en"]["stringUnit"]["value"] = "%lld hours %lld minutes"
    path.write_text(json.dumps(value))
    assert any(expected in error for error in validate(root))


def test_disjoint_catalog_merge_and_conflict_detection():
    left = {"sourceLanguage": "zh-Hans", "strings": {"common.close": {"value": "Close"}}}
    right = {"sourceLanguage": "zh-Hans", "strings": {"common.open": {"value": "Open"}}}
    assert merger.merge(left, right)["strings"].keys() == {"common.close", "common.open"}
    right["strings"]["common.close"] = {"value": "Dismiss"}
    with pytest.raises(ValueError, match="Conflicting catalog key: common.close"):
        merger.merge(left, right)
    assert left["strings"] == {"common.close": {"value": "Close"}}


def test_merge_cli_keeps_existing_output_on_conflict(tmp_path):
    left, right, output = (tmp_path / name for name in ("left.json", "right.json", "output.json"))
    for path, value in ((left, "Close"), (right, "Dismiss")):
        path.write_text(
            json.dumps({"sourceLanguage": "zh-Hans", "strings": {"common.close": value}})
        )
    output.write_text("previous catalog")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "xcstrings-merge.py"),
            str(left),
            str(right),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Conflicting catalog key: common.close" in result.stderr
    assert output.read_text() == "previous catalog"
