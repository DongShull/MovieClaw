"""Shared catalog checks: manual keys, hashes, language aliases and printf parameter maps."""

import hashlib
import json
import re

SPECIFIER = re.compile(r"%%|%(?:(\d+)\$)?(?:[-+ 0#]*\d*(?:\.\d+)?)?(lld|ld|d|llu|lu|u|f|@)")


def normalize_printf(text, names):
    implicit = 0
    parameters = {}

    def replace(match):
        nonlocal implicit
        if match[0] == "%%":
            return "%"
        if match[1]:
            index = int(match[1]) - 1
        else:
            index = implicit
            implicit += 1
        parameters[index] = match[2]
        if index >= len(names):
            raise ValueError("Missing logical parameter name")
        return "{" + names[index] + "}"

    return SPECIFIER.sub(replace, text), parameters


def check_apple(root, web_english, web_parameters, terms=None):
    base = root / "apps/apple"
    meta_path = base / "Localization.meta.json"
    if not meta_path.exists():
        return []
    meta = json.loads(meta_path.read_text())
    paths = {str(p.relative_to(base)) for p in base.glob("*/*.xcstrings")}
    errors = []
    if paths != meta.keys():
        errors.append("Apple catalog metadata file set differs")
    for path, records in meta.items():
        catalog = json.loads((base / path).read_text())
        if catalog["sourceLanguage"] != "zh-Hans":
            errors.append(f"Apple source language changed: {path}")
        if catalog["strings"].keys() != records.keys():
            errors.append(f"Apple catalog metadata key set differs: {path}")
        for key, entry in catalog["strings"].items():
            context = f"{path}:{key}"
            if path.endswith("Localizable.xcstrings") and not re.fullmatch(
                r"[a-z][a-zA-Z0-9]*(?:\.[a-z][a-zA-Z0-9]*){1,3}", key
            ):
                errors.append(f"Invalid Apple semantic key: {context}")
            translations = entry.get("localizations", {})
            if translations.keys() != {"zh-Hans", "zh-Hant", "en"}:
                errors.append(f"Apple missing localization: {context}")
                continue
            if translations["zh-Hant"] != translations["zh-Hans"]:
                errors.append(f"Apple simplified alias differs: {context}")
            if entry.get("extractionState") != "manual":
                errors.append(f"Apple key is not manual: {context}")
            try:
                source = translations["zh-Hans"]["stringUnit"]["value"]
                english = translations["en"]["stringUnit"]["value"]
                record = records.get(key, {})
                if record.get("sourceHash") != hashlib.sha256(source.encode()).hexdigest():
                    errors.append(f"Stale Apple English: {context}")
                if translations["en"]["stringUnit"]["state"] != "translated":
                    errors.append(f"Apple translation needs review: {context}")
                if re.search(r"[\u3400-\u9fff，。！？：；「」《》、]", english):
                    errors.append(f"Apple English contains Chinese: {context}")
                if source in (terms or {}) and english.lower() not in terms[source]:
                    errors.append(f"Apple glossary mismatch: {context}")
                names = record.get("parameters", [])
                _, source_params = normalize_printf(source, names)
                normalized_english, english_params = normalize_printf(english, names)
                if source_params != english_params or set(source_params) != set(range(len(names))):
                    errors.append(f"Apple printf parameters differ: {context}")
                if key in web_english:
                    if normalized_english != web_english[key]:
                        errors.append(f"Apple/Web English differs: {context}")
                    if set(names) != web_parameters.get(key, set()):
                        errors.append(f"Apple/Web logical parameters differ: {context}")
            except (KeyError, ValueError) as exc:
                errors.append(
                    f"Apple catalog needs an explicit parameter/variation mapping: {context}: {exc}"
                )
    return errors
