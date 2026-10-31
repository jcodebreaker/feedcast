"""`feedcast doctor`: one line per thing a capture or report needs. Never prints secrets."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import capture, config, schedule

PROFILE_MAX_AGE_H = 48


def _roots(cfg):
    roots = cfg["roots"]
    if not roots:
        return False, "work folders", "none set, run `feedcast setup`"
    missing = [r for r in roots if not Path(r).expanduser().is_dir()]
    if missing:
        return False, "work folders", f"missing: {', '.join(missing)}"
    return True, "work folders", ", ".join(roots)


def _profile(cfg):
    try:
        prof = yaml.safe_load(config.profile_path().read_text()) or {}
        at = datetime.fromisoformat(str(prof["generated_at"]).replace("Z", "+00:00"))
    except (FileNotFoundError, KeyError, ValueError, TypeError, yaml.YAMLError):
        return False, "profile", "not built yet, run `feedcast setup`"
    hours = (datetime.now(timezone.utc) - at).total_seconds() / 3600
    age = f"{hours:.0f}h old" if hours < 48 else f"{hours / 24:.0f} days old"
    return hours < PROFILE_MAX_AGE_H, "profile", age


def _api_key(cfg):
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True, "API key", "from ANTHROPIC_API_KEY"
    # No -w: this asks whether the item exists and never returns the secret.
    res = capture.run(["security", "find-generic-password", "-s", "feedcast", "-a", "anthropic"])
    return (True, "API key", "in Keychain") if res.returncode == 0 else (True, "models", "no API key, using claude -p on your plan")


def _chrome(cfg):
    return (True, "Chrome", "running") if capture.chrome_running() else (False, "Chrome", "not running")


def _claude(cfg):
    res = capture.run(["claude", "--help"])
    if res.returncode == 127:
        return False, "claude", "not on PATH"
    if "--chrome" not in (res.stdout or ""):
        return False, "claude", "no --chrome option, update Claude Code"
    return True, "claude", "has --chrome"


def _paused(cfg):
    reason = capture.paused_reason()
    return (True, "captures", "not paused") if reason is None else (False, "captures", f"paused ({reason}), run `feedcast resume`")


def _last_run(cfg):
    runs = sorted(config.runs_dir().glob("*/*.json"))
    if not runs:
        return False, "last run", "none yet"
    p = runs[-1]
    try:
        status = json.loads(p.read_text()).get("status", "?")
    except ValueError:
        status = "unreadable"
    return status == "ok", "last run", f"{status}, {p.parent.name} {p.stem[:2]}:{p.stem[2:4]}"


def _jobs(cfg):
    want = set(schedule.jobs(cfg))
    have = {p.stem for p in schedule.installed()}
    if want <= have:
        return True, "schedule", f"{len(want)} jobs installed"
    return False, "schedule", f"{len(want - have)} of {len(want)} jobs missing, turn on daily reads in `feedcast setup`"


def _gh(cfg):
    ok = capture.run(["gh", "auth", "token"]).returncode == 0  # output is the token: never shown
    return (True, "gh token", "available") if ok else (False, "gh token", "none (optional, raises GitHub limits)")


CHECKS = [_roots, _profile, _api_key, _chrome, _claude, _paused, _last_run, _jobs, _gh]
OPTIONAL = {"gh token"}


def run_checks() -> bool:
    cfg = config.load()
    healthy = True
    for check in CHECKS:
        ok, name, detail = check(cfg)
        print(f"{'✓' if ok else '✗'} {name}: {detail}")
        healthy &= ok or name in OPTIONAL
    return healthy


def cmd_doctor(args) -> int:
    return 0 if run_checks() else 1


def register(sub) -> None:
    p = sub.add_parser("doctor", help="check that everything is in place")
    p.set_defaults(func=cmd_doctor)
