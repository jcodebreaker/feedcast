"""launchd jobs: `feedcast schedule install|remove|show` (ARCHITECTURE "Schedule").

One plist per job, all labelled com.feedcast.*. Commands run through capture.run so tests
never touch launchctl.
"""
from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import sys
from pathlib import Path

from . import capture, config

PREFIX = "com.feedcast."
PROJECT = Path(__file__).resolve().parent.parent


def launchd_dir() -> Path:
    return Path(os.environ.get("FEEDCAST_LAUNCHD_DIR") or Path.home() / "Library" / "LaunchAgents")


def feedcast_cmd() -> str:
    """Shell words that start this same feedcast: the installed script, else `uv run` in the project."""
    exe = Path(sys.argv[0])
    if exe.name == "feedcast" and exe.exists():
        return shlex.quote(str(exe.resolve()))
    found = shutil.which("feedcast")
    if found:
        return shlex.quote(found)
    uv = shutil.which("uv") or "uv"
    return f"{shlex.quote(uv)} run --project {shlex.quote(str(PROJECT))} feedcast"


def jobs(cfg: dict) -> dict[str, tuple[str, str]]:
    """label -> ("HH:MM", feedcast arguments as shell text)."""
    fc = feedcast_cmd()
    s = cfg["schedule"]
    out = {f"{PREFIX}profile": (s["profile"], f"{fc} profile")}
    for t in s["captures"]:
        out[f"{PREFIX}capture-{t.replace(':', '')}"] = (t, f"{fc} capture --scheduled && {fc} ingest")
    out[f"{PREFIX}report"] = (s["digest"], f"{fc} report")
    return out


def plist(label: str, at: str, shell: str) -> bytes:
    hour, minute = (int(x) for x in at.split(":"))
    logs = config.path("logs")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")}  # so claude, uv, gh are found
    if os.environ.get("FEEDCAST_HOME"):
        env["FEEDCAST_HOME"] = os.environ["FEEDCAST_HOME"]
    job = label.removeprefix(PREFIX)
    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": ["/bin/sh", "-c", shell],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "EnvironmentVariables": env,
        "StandardOutPath": str(logs / f"{job}.log"),
        "StandardErrorPath": str(logs / f"{job}.log"),
        "WorkingDirectory": str(config.home()),
    })


def _domain() -> str:
    return f"gui/{os.getuid()}"


def installed() -> list[Path]:
    return sorted(launchd_dir().glob(f"{PREFIX}*.plist"))


def install(cfg: dict) -> None:
    d = launchd_dir()
    d.mkdir(parents=True, exist_ok=True)
    config.path("logs").mkdir(parents=True, exist_ok=True)
    want = jobs(cfg)
    for old in installed():  # capture times may have changed
        if old.stem not in want:
            _unload(old)
    for label, (at, shell) in want.items():
        p = d / f"{label}.plist"
        capture.run(["launchctl", "bootout", f"{_domain()}/{label}"])  # reload if already there
        p.write_bytes(plist(label, at, shell))
        res = capture.run(["launchctl", "bootstrap", _domain(), str(p)])
        print(f"{'✓' if res.returncode == 0 else '✗'} {label} at {at}")


def _unload(p: Path) -> None:
    capture.run(["launchctl", "bootout", f"{_domain()}/{p.stem}"])
    p.unlink(missing_ok=True)


def remove() -> None:
    found = installed()
    for p in found:
        _unload(p)
        print(f"Removed {p.stem}")
    if not found:
        print("No Feedcast jobs installed.")


def show() -> None:
    found = installed()
    if not found:
        print("No Feedcast jobs installed. Run `feedcast schedule install`.")
    for p in found:
        data = plistlib.loads(p.read_bytes())
        t = data.get("StartCalendarInterval", {})
        loaded = capture.run(["launchctl", "print", f"{_domain()}/{p.stem}"]).returncode == 0
        print(f"{p.stem:<28} {t.get('Hour', 0):02d}:{t.get('Minute', 0):02d}  {'loaded' if loaded else 'not loaded'}")


def cmd_schedule(args) -> int:
    if args.action == "install":
        install(config.load())
    elif args.action == "remove":
        remove()
    else:
        show()
    return 0


def register(sub) -> None:
    p = sub.add_parser("schedule", help="install, remove or show the launchd jobs")
    p.add_argument("action", choices=["install", "remove", "show"], nargs="?", default="show")
    p.set_defaults(func=cmd_schedule)
