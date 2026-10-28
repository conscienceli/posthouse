"""The ready-to-paste message that connects a new Claude task to a workspace, and the skill it downloads."""

from __future__ import annotations

import io
import json
import re
import shlex
import threading
import zipfile
from pathlib import Path

from .oauth import I18N_DIR, _fill, _load_lang

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "posthouse"
SKILL_PATH = "/skill/posthouse-skill.zip"
GITHUB_SKILL_URL = "https://github.com/conscienceli/posthouse/releases/latest/download/posthouse-skill.zip"
CLI = "python3 ~/.claude/skills/posthouse/scripts/posthouse.py"
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")

_zip_lock = threading.Lock()
_zip_cache: dict = {"key": None, "data": None}


def skill_zip() -> bytes | None:
    """The skill folder as a zip (posthouse/SKILL.md, posthouse/scripts/…), rebuilt when files change."""
    if not (SKILL_DIR / "SKILL.md").is_file():
        return None
    files = sorted(p for p in SKILL_DIR.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    key = tuple((p.as_posix(), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    with _zip_lock:
        if _zip_cache["key"] != key:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for p in files:
                    z.write(p, (Path("posthouse") / p.relative_to(SKILL_DIR)).as_posix())
            _zip_cache.update(key=key, data=buf.getvalue())
        return _zip_cache["data"]


def _strings(lang: str | None) -> dict:
    strings = dict(_load_lang("en"))
    if lang and lang != "en" and LANG_RE.match(lang):
        try:
            known = {x["code"] for x in json.loads((I18N_DIR / "languages.json").read_text(encoding="utf-8"))}
        except (OSError, ValueError, KeyError, TypeError):
            known = set()
        if lang in known:
            strings.update(_load_lang(lang))
    return strings


def build_message(workspace: str | None, base_url: str, token: str, agent: str = "Claude",
                  lang: str | None = None) -> str:
    """workspace=None: the token has agent access and Claude chooses or creates the workspace itself."""
    s = _strings(lang)

    def t(key: str, **kw) -> str:
        return _fill(s.get(key, key), {k: str(v) for k, v in kw.items()})

    q = shlex.quote
    skill_url = base_url + SKILL_PATH if skill_zip() is not None else GITHUB_SKILL_URL
    install = (f"mkdir -p ~/.claude/skills && curl -fsSL {q(skill_url)} -o /tmp/posthouse-skill.zip"
               " && python3 -m zipfile -e /tmp/posthouse-skill.zip ~/.claude/skills/")
    login = f"{CLI} login --url {q(base_url)} --token {q(token)} --agent {q(agent)}"
    if workspace is None:
        return "\n".join([
            t("setup.msgIntroServer"), "",
            f"- {t('setup.msgServer')}: {base_url}",
            f"- {t('setup.msgToken')}: {token} {t('setup.msgTokenNoteAgent')}", "",
            t("setup.msgRun"), "",
            "```bash", install, login, f"{CLI} ls", "```", "",
            t("setup.msgAfterServer", cli=CLI),
        ]) + "\n"
    commands = "\n".join([install, login, f"{CLI} install-hooks --workspace {q(workspace)}",
                          f"{CLI} start {q(workspace)}"])
    return "\n".join([
        t("setup.msgIntro", ws=workspace), "",
        f"- {t('setup.msgServer')}: {base_url}",
        f"- {t('setup.msgWorkspace')}: {workspace}",
        f"- {t('setup.msgToken')}: {token} {t('setup.msgTokenNote')}", "",
        t("setup.msgRun"), "",
        "```bash", commands, "```", "",
        t("setup.msgAfter"),
    ]) + "\n"
