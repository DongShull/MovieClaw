"""Validate declared translation provenance against application-owned metadata sources."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import tomllib
from pathlib import Path

HAN = re.compile(r"[\u3400-\u9fff，。！？：；「」《》、]")


def check_manifest_sources(root: Path) -> list[str]:
    owners = {}
    for directory in (root / "plugins", root / "src/movieclaw_plugins"):
        for path in sorted(directory.glob("*/movieclaw-plugin.toml")):
            document = tomllib.loads(path.read_text())
            # Source packages take precedence over their generated bundled copies.
            owners.setdefault(document["plugin"]["id"], (path, document))
    if not owners:
        return []
    ledger_path = root / "scripts/i18n/plugin-metadata.json"
    if not ledger_path.exists():
        return [f"Missing plugin metadata provenance: {ledger_path}"]
    ledger = json.loads(ledger_path.read_text())
    errors = []
    for path, document in owners.values():
        source_file = str(path.relative_to(root))
        for namespace, fields in (
            ("plugin", ("title", "description")),
            ("theme", ("label", "description")),
        ):
            metadata = document.get(namespace, {})
            for field in fields:
                source = metadata.get(field, "")
                translated = metadata.get(field + "_localized", {}).get("en")
                if isinstance(translated, dict):
                    translated = translated.get("value")
                name = f"{source_file}:{namespace}.{field}"
                if translated is None:
                    if HAN.search(source):
                        errors.append(f"Missing English plugin metadata: {name}")
                    continue
                if (
                    not isinstance(translated, str)
                    or not translated.strip()
                    or HAN.search(translated)
                ):
                    errors.append(f"Invalid English plugin metadata: {name}")
                expected = (
                    ledger.get(source_file, {}).get(f"{namespace}.{field}", {}).get("sourceHash")
                )
                if expected != hashlib.sha256(source.encode()).hexdigest():
                    errors.append(f"Stale English plugin metadata: {name}")
    return errors


def check_builtin_sources(root: Path, catalog: dict[str, str]) -> list[str]:
    manifest_path = root / "src/movieclaw_api/plugins/manifest.py"
    if not manifest_path.exists():
        return []
    ledger_path = root / "scripts/i18n/builtin-sources.json"
    if not ledger_path.exists():
        return [f"Missing built-in metadata provenance: {ledger_path}"]
    records = json.loads(ledger_path.read_text())
    tree = ast.parse(manifest_path.read_text())
    groups = ast.literal_eval(
        next(
            node.value
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "BUILTIN_GROUPS"
        )
    )
    presentation = ast.parse((root / "src/movieclaw_api/plugins/presentation.py").read_text())
    group_keys = ast.literal_eval(
        next(
            node.value
            for node in presentation.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "GROUP_MESSAGE_KEYS"
                for target in node.targets
            )
        )
    )
    errors = []
    if len(groups) != len(group_keys):
        errors.append("Built-in group metadata does not match the manifest")
    for (label, _), key in zip(groups, group_keys, strict=False):
        if catalog.get(key) != label:
            errors.append(f"Built-in group metadata source changed: {key}")
    declaration = next(
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "BUILTIN_MANIFEST"
    )
    generator = next(
        node for node in ast.walk(declaration.value) if isinstance(node, ast.GeneratorExp)
    )
    declared = {ast.unparse(node) for node in generator.generators[0].iter.elts}
    if declared != {row["manifest_ref"] for row in records.values() if "manifest_ref" in row}:
        errors.append("Built-in metadata declarations do not match the manifest")
    modules = {}
    for entry_id, record in records.items():
        path = root / record["path"]
        if path not in modules:
            modules[path] = ast.parse(path.read_text())
        function = next(
            node
            for node in modules[path].body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == record["function"]
        )
        decorator = next(
            node
            for node in function.decorator_list
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "plugin"
        )
        if ast.literal_eval(decorator.args[0]) != entry_id:
            errors.append(f"Built-in metadata source ID changed: {entry_id}")
        title = (
            next(
                (ast.literal_eval(kw.value) for kw in decorator.keywords if kw.arg == "title"),
                entry_id,
            )
            or entry_id
        )
        doc = ast.get_docstring(function) or ""
        description = " ".join(
            line.strip() for line in doc.split("\n\n", 1)[0].splitlines()
        ).strip()
        for field, source in (("title", title), ("description", description)):
            if catalog.get(record[field]) != source:
                errors.append(f"Built-in metadata source changed: {entry_id}.{field}")
    for path, module in modules.items():
        covered = {
            row["function"] for row in records.values() if root / row["path"] == path
        }
        for node in ast.walk(module):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ctx"
                and node.func.attr == "plugin"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id not in covered
            ):
                errors.append(f"Missing built-in child metadata: {node.args[0].id}")
    return errors
