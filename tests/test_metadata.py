import ast
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _plugin_versions():
    tree = ast.parse((ROOT / "netbox_export" / "__init__.py").read_text(encoding="utf-8"))
    values = {
        target.id: node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    return values["min_version"], values["max_version"]


def test_readme_netbox_range_matches_plugin_config():
    min_version, max_version = _plugin_versions()
    major, minor, patch = max_version.split(".")
    upper = f"{major}.{minor}.x" if patch == "99" else max_version  # 4.7.99 is documented as 4.7.x
    expected = f"`{min_version}` – `{upper}`"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert f"| **NetBox** | {expected} |" in readme
    assert f"- NetBox {expected}" in readme


def test_german_catalog_is_complete():
    po = ROOT / "netbox_export" / "locale" / "de" / "LC_MESSAGES" / "django.po"
    content = po.read_text(encoding="utf-8")
    entries = re.findall(r'^msgid ((?:".*"\n)+)msgstr ((?:".*"\n?)+)', content, re.MULTILINE)

    assert "#, fuzzy" not in content
    assert [msgid for msgid, msgstr in entries[1:] if msgstr.strip() == '""'] == []
    assert po.with_suffix(".mo").is_file()
