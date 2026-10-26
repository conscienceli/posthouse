"""Keep translations complete: every language file must have every key used by the UI."""

import json
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "posthouse" / "static"
I18N = STATIC / "i18n"


def load(code):
    return json.loads((I18N / f"{code}.json").read_text(encoding="utf-8"))


def test_languages_listed_and_present():
    langs = json.loads((I18N / "languages.json").read_text(encoding="utf-8"))
    codes = [x["code"] for x in langs]
    assert codes[0] == "en" and len(codes) == len(set(codes))
    for item in langs:
        assert (I18N / f"{item['code']}.json").is_file(), item
        assert load(item["code"])["lang.name"] == item["name"]


def test_all_languages_have_the_same_keys():
    en = load("en")
    for f in I18N.glob("*.json"):
        if f.name == "languages.json":
            continue
        other = json.loads(f.read_text(encoding="utf-8"))
        assert set(other) == set(en), f"{f.name}: missing {set(en) - set(other)}, extra {set(other) - set(en)}"
        for k, v in en.items():
            assert set(re.findall(r"\{(\w+)\}", v)) == set(re.findall(r"\{(\w+)\}", other[k])), (f.name, k)


def test_ui_keys_exist():
    en = load("en")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    used = {k for k in re.findall(r"\bt\('([a-zA-Z0-9_.]+)'", js) if not k.endswith(".")}
    used |= {f"nav.{k}" for k in ("workspaces", "tokens", "connections", "activity", "trash", "connect")}
    used |= {f"tab.{k}" for k in ("files", "history", "activity", "settings", "connect")}
    used |= {f"role.{k}" for k in ("admin", "agent", "workspace")}
    missing = sorted(k for k in used if k not in en)
    assert not missing, missing
