"""`trk pages timer install|remove|status` (Page publisher spec: Timer and the pull): a systemd user
unit pair `trk-pages.{service,timer}` that runs `trk pages tick` about every minute, the backstop
for a flusher that died or exited with failed pages left.

Loaded only by `pages timer`. The units go to `$XDG_CONFIG_HOME/systemd/user` (default
`~/.config/systemd/user`) and are driven with `systemctl --user`. Under `$TRK_TEST=1` the default
unit directory is refused, as the default store is, so no test can touch this machine's systemd.

On macOS, where there is no systemctl, the timer is a launchd agent instead: `com.trk.pages.plist`
in `~/Library/LaunchAgents` (or `$TRK_LAUNCH_AGENTS`), run every 60 seconds and driven with
`launchctl`. Under `$TRK_TEST=1` the default folder is refused there too.
"""

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import store
from .errors import refused, usage

SERVICE = "trk-pages.service"
TIMER = "trk-pages.timer"
PERIOD = "60s"
LABEL = "com.trk.pages"
PLIST = LABEL + ".plist"
SECONDS = 60


def launchd():
    """True on macOS without systemctl: the timer is a launchd agent."""
    return sys.platform == "darwin" and shutil.which("systemctl") is None


def name():
    """What the timer is called where it lives."""
    return PLIST if launchd() else TIMER


def unit_dir():
    if launchd():
        return agents_dir()
    default = Path.home() / ".config" / "systemd" / "user"
    config = os.environ.get("XDG_CONFIG_HOME")
    path = Path(config).expanduser() / "systemd" / "user" if config else default
    if os.environ.get("TRK_TEST") == "1" and path.resolve() == default.resolve():
        raise usage(f"$TRK_TEST=1 refuses the default systemd user directory {default}; set $XDG_CONFIG_HOME",
                    rule="test-units")
    return path


def agents_dir():
    default = Path.home() / "Library" / "LaunchAgents"
    override = os.environ.get("TRK_LAUNCH_AGENTS")
    path = Path(override).expanduser() if override else default
    if os.environ.get("TRK_TEST") == "1" and path.resolve() == default.resolve():
        raise usage(f"$TRK_TEST=1 refuses the default launchd folder {default}; set $TRK_LAUNCH_AGENTS",
                    rule="test-units")
    return path


def trk_path():
    """The trk the units run: this process's own script when it is `trk`, else the `trk` on PATH."""
    running = Path(sys.argv[0])
    if running.name == "trk" and running.exists():
        return str(running.resolve())
    found = shutil.which("trk")
    if found is None:
        raise refused("no-trk", "cannot find the trk executable to run from the timer; put trk on PATH")
    return str(Path(found).resolve())


def _quote(word, *, exec_line):
    """A word as systemd reads it, double-quoted when it needs it. `%` is a specifier everywhere;
    `$` expands only on an Exec line, so only there is it doubled."""
    special = '"\'\\;%' + ("$" if exec_line else "")
    if word and not any(ch.isspace() or ch in special for ch in word):
        return word
    text = word.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + (text.replace("$", "$$") if exec_line else text) + '"'


def exec_start(trk):
    return f"{_quote(trk, exec_line=True)} pages tick"


def units(trk, home):
    """{file name: content} for the pair. `home` is $TRK_HOME at install time, or None."""
    environment = f"Environment={_quote('TRK_HOME=' + home, exec_line=False)}\n" if home else ""
    service = ("[Unit]\n"
               "Description=trk page publisher: pull comments, then publish due pages\n\n"
               "[Service]\n"
               "Type=oneshot\n"
               f"{environment}"
               f"ExecStart={exec_start(trk)}\n")
    timer = ("[Unit]\n"
             "Description=Run trk pages tick about every minute\n\n"
             "[Timer]\n"
             f"OnActiveSec={PERIOD}\n"  # the first run; OnUnitActiveSec alone never fires before the service has run
             f"OnUnitActiveSec={PERIOD}\n\n"
             "[Install]\n"
             "WantedBy=timers.target\n")
    return {SERVICE: service, TIMER: timer}


def plist(trk, home):
    """The launchd agent: `trk pages tick` every SECONDS, with a PATH that finds trk and its adapter."""
    path = os.pathsep.join(dict.fromkeys([str(Path(trk).parent), str(Path.home() / ".local/bin"),
                                          "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]))
    log = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "trk" / "pages-timer.log"
    env = {"PATH": path, **({"TRK_HOME": home} if home else {})}
    return plistlib.dumps({"Label": LABEL, "ProgramArguments": [trk, "pages", "tick"],
                           "StartInterval": SECONDS, "EnvironmentVariables": env,
                           "StandardOutPath": str(log), "StandardErrorPath": str(log)}), log


def launchctl(*argv, check=True):
    """Run `launchctl …`; a failure with `check` is refused under rule `launchctl`."""
    try:
        done = subprocess.run(["launchctl", *argv], capture_output=True, text=True, check=False)
    except OSError as exc:
        raise refused("launchctl", f"cannot run launchctl: {exc.strerror or exc}") from None
    if check and done.returncode != 0:
        detail = (done.stderr or done.stdout).strip()
        raise refused("launchctl", f"`launchctl {' '.join(argv)}` failed ({done.returncode})"
                                   + (f": {detail}" if detail else ""))
    return done


def _domain():
    return f"gui/{os.getuid()}"


def systemctl(*argv, check=True):
    """Run `systemctl --user …`; a failure with `check` is refused under rule `systemctl`."""
    try:
        done = subprocess.run(["systemctl", "--user", *argv], capture_output=True, text=True, check=False)
    except OSError as exc:
        raise refused("systemctl", f"cannot run systemctl: {exc.strerror or exc}") from None
    if check and done.returncode != 0:
        detail = (done.stderr or done.stdout).strip()
        raise refused("systemctl", f"`systemctl --user {' '.join(argv)}` failed ({done.returncode})"
                                   + (f": {detail}" if detail else ""))
    return done


def install():
    """Write both units, reload, and enable and start the timer. Rewriting is how a moved trk or a
    changed $TRK_HOME is picked up."""
    directory = unit_dir()
    home = os.environ.get("TRK_HOME")
    home = os.path.abspath(os.path.expanduser(home)) if home else None
    if home is None:  # the timer would run against the live store: only the installed trk may do that
        store.guard_default(store.default_dir(), "install a timer for")
    trk = trk_path()
    directory.mkdir(parents=True, exist_ok=True)
    if launchd():
        content, log = plist(trk, home)
        log.parent.mkdir(parents=True, exist_ok=True)
        tmp = directory / (PLIST + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, directory / PLIST)
        launchctl("bootout", f"{_domain()}/{LABEL}", check=False)  # a loaded old copy keeps its old settings
        launchctl("bootstrap", _domain(), str(directory / PLIST))
        return {**status(), "exec_start": shlex.join([trk, "pages", "tick"]), "trk_home": home}
    for name, content in units(trk, home).items():
        tmp = directory / (name + ".tmp")
        tmp.write_text(content)
        os.replace(tmp, directory / name)
    systemctl("daemon-reload")
    systemctl("enable", "--now", TIMER)
    return {**status(), "exec_start": exec_start(trk), "trk_home": home}


def remove():
    """Stop and disable the timer, delete both units and reload. Removing what isn't there is fine."""
    directory = unit_dir()
    if launchd():
        present = [PLIST] if (directory / PLIST).exists() else []
        if present:
            launchctl("bootout", f"{_domain()}/{LABEL}", check=False)  # an unloaded agent is already off
            (directory / PLIST).unlink()
        return {**status(), "removed": present}
    present = [name for name in (SERVICE, TIMER) if (directory / name).exists()]
    if present:
        systemctl("disable", "--now", TIMER, check=False)  # an unloaded timer is already off
    for name in present:
        (directory / name).unlink()
    if present:
        systemctl("daemon-reload")
    return {**status(), "removed": present}


def status():
    """Whether the unit files exist, and what systemd says of the timer."""
    directory = unit_dir()
    if launchd():
        loaded = launchctl("print", f"{_domain()}/{LABEL}", check=False).returncode == 0
        return {"unit_dir": str(directory), "installed": (directory / PLIST).exists(),
                "enabled": "enabled" if loaded else "disabled", "active": "active" if loaded else "inactive"}
    installed = all((directory / name).exists() for name in (SERVICE, TIMER))
    enabled = systemctl("is-enabled", TIMER, check=False).stdout.strip() or "unknown"
    active = systemctl("is-active", TIMER, check=False).stdout.strip() or "unknown"
    return {"unit_dir": str(directory), "installed": installed, "enabled": enabled, "active": active}
