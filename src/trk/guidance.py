"""Site guidance: a site's own rules for a trk skill, kept outside the release (UP-0016).

One markdown file per skill, `${TRK_GUIDANCE_DIR:-${XDG_CONFIG_HOME:-~/.config}/trk/guidance}/<skill>.md`. trk only
prints it: no decision reads it, and neither install.sh nor a store restore writes it. Each skill tells the agent to
run `trk guidance <name>` once and follow what it prints; site guidance may add and narrow rules, never loosen a rule
of the skill or override a trk refusal."""
import hashlib
import os
from pathlib import Path

from .errors import usage

SKILLS = ("trk", "trk-drive-program", "trk-create-program")
MAX_BYTES = 64 * 1024


def directory():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(os.environ.get("TRK_GUIDANCE_DIR") or Path(base) / "trk" / "guidance")


def path(skill):
    if skill not in SKILLS:
        raise usage(f"{skill!r} is not a trk skill; one of: {', '.join(SKILLS)}", rule="unknown-skill")
    return directory() / f"{skill}.md"


def read(skill):
    """{"skill", "path", "present", "bytes", "sha256", "text"} for `skill`; text None when there is no file. Usage
    error (exit 2) for a file that is unreadable, not UTF-8 or over MAX_BYTES."""
    where = path(skill)
    try:
        raw = where.read_bytes()
    except FileNotFoundError:
        return {"skill": skill, "path": str(where), "present": False, "bytes": 0, "sha256": None, "text": None}
    except OSError as e:
        raise usage(f"{where}: cannot read ({e.strerror})", rule="guidance-unreadable") from None
    if len(raw) > MAX_BYTES:
        raise usage(f"{where} is {len(raw)} bytes; site guidance is at most {MAX_BYTES}", rule="guidance-too-big")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise usage(f"{where} is not UTF-8", rule="guidance-not-utf8") from None
    return {"skill": skill, "path": str(where), "present": True, "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), "text": text}
