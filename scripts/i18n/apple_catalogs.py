"""Shared catalog checks: manual keys, hashes, language aliases and printf parameter maps."""

import hashlib
import json
import re

SPECIFIER = re.compile(r"%%|%(?:(\d+)\$)?(?:[-+ 0#]*\d*(?:\.\d+)?)?(lld|ld|d|llu|lu|u|lf|f|@)")


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


def translation_units(localization):
    if "stringUnit" in localization:
        return {None: localization["stringUnit"]}
    variations = localization["variations"]
    if set(variations) != {"plural"}:
        raise ValueError("Only explicit plural variations are supported")
    plural = variations["plural"]
    if "other" not in plural or set(plural) - {"zero", "one", "two", "few", "many", "other"}:
        raise ValueError("Plural variations need an other branch and valid categories")
    return {category: branch["stringUnit"] for category, branch in plural.items()}


def normalized_translation(localization, record):
    units = translation_units(localization)
    names = record.get("parameters", [])
    if None in units:
        return normalize_printf(units[None]["value"], names)[0]
    control = record["pluralParameter"]
    if control not in names:
        raise ValueError("Plural parameter must be explicitly mapped")
    branches = []
    for category in ("zero", "one", "two", "few", "many", "other"):
        if category in units:
            text, _ = normalize_printf(units[category]["value"], names)
            branches.append(category + " {" + text.replace("{" + control + "}", "#") + "}")
    return "{" + control + ", plural, " + " ".join(branches) + "}"


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
                record = records.get(key, {})
                units = translation_units(translations["en"])
                english = normalized_translation(translations["en"], record)
                if record.get("sourceHash") != hashlib.sha256(source.encode()).hexdigest():
                    errors.append(f"Stale Apple English: {context}")
                names = record.get("parameters", [])
                _, source_params = normalize_printf(source, names)
                if set(source_params) != set(range(len(names))):
                    errors.append(f"Apple printf parameters differ: {context}")
                for unit in units.values():
                    text = unit["value"]
                    if unit["state"] != "translated":
                        errors.append(f"Apple translation needs review: {context}")
                    if re.search(r"[\u3400-\u9fff，。！？：；「」《》、]", text):
                        errors.append(f"Apple English contains Chinese: {context}")
                    allowed_terms = (terms or {}).get(key, (terms or {}).get(source))
                    if allowed_terms is not None and text.lower() not in allowed_terms:
                        errors.append(f"Apple glossary mismatch: {context}")
                    _, english_params = normalize_printf(text, names)
                    if None not in units:
                        index = names.index(record["pluralParameter"])
                        english_params.setdefault(index, source_params[index])
                    if source_params != english_params:
                        errors.append(f"Apple printf parameters differ: {context}")
                normalized_english = english
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
