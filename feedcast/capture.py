"""Reading the feed: `feedcast init --terminal`, `capture`, `resume`, and the one-off read of the user's own
profile behind `feedcast profile --linkedin` (CONTRACT §3, §4, §7).

The browser agent is Claude in Chrome driven by `claude -p --chrome`. Everything that
leaves the process (the agent, pgrep, pmset, notifications, Keychain) goes through `run`,
so tests swap one function and nothing real is touched.
"""
from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import random
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, models

PROMPT = Path(__file__).resolve().parent / "prompts" / "capture.md"
SELF_PROMPT = PROMPT.with_name("self.md")
AGENT_TIMEOUT = 600
MAX_DELAY = 8 * 60
MIN_BATTERY = 20
PAUSING = {"aborted_challenge", "aborted_not_signed_in"}
PAUSED_NOTE = "Feedcast paused. LinkedIn asked for a check. Nothing was clicked. Run feedcast resume when you're ready."

# Only what reading needs: tabs, navigation, scrolling and screenshots (computer), reading the page
# (read_page is where post permalinks and outbound hrefs come from).
# Left out on purpose: form_input, javascript_tool, file_upload, upload_image, shortcuts_execute.
CHROME_TOOLS = [
    "mcp__claude-in-chrome__tabs_context_mcp",
    "mcp__claude-in-chrome__tabs_create_mcp",
    "mcp__claude-in-chrome__tabs_close_mcp",
    "mcp__claude-in-chrome__navigate",
    "mcp__claude-in-chrome__computer",
    "mcp__claude-in-chrome__read_page",
    "mcp__claude-in-chrome__get_page_text",
    "mcp__claude-in-chrome__find",
]


def _default_runner(cmd, input=None, timeout=None, cwd=None):
    try:
        return subprocess.run(cmd, input=input, timeout=timeout, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")


# Tests replace these.
RUNNER = _default_runner
SLEEP = time.sleep
INPUT = input
GETPASS = getpass.getpass


def run(cmd, input=None, timeout=None, cwd=None) -> subprocess.CompletedProcess:
    """The one way Feedcast runs another program. May raise subprocess.TimeoutExpired."""
    return RUNNER(cmd, input=input, timeout=timeout, cwd=cwd)


# ---- small probes -------------------------------------------------------------------------

def chrome_running() -> bool:
    return run(["pgrep", "-x", "Google Chrome"]).returncode == 0


def battery_low() -> bool:
    """True only when unplugged and under MIN_BATTERY. Desktops and unknown output count as fine."""
    out = run(["pmset", "-g", "batt"]).stdout or ""
    m = re.search(r"(\d+)%", out)
    return "Battery Power" in out and bool(m) and int(m.group(1)) < MIN_BATTERY


def notify(message: str) -> None:
    script = f'display notification "{message}" with title "Feedcast"'
    run(["osascript", "-e", script])


def paused_reason() -> str | None:
    try:
        return config.paused_path().read_text().strip() or "paused"
    except FileNotFoundError:
        return None


def _pause(reason: str) -> None:
    p = config.paused_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(reason + "\n")
    notify(PAUSED_NOTE)


# ---- the agent ----------------------------------------------------------------------------

def render_prompt(max_posts: int, out_path: Path) -> str:
    # str.replace, not format(): the prompt holds a JSON example full of braces.
    return PROMPT.read_text().replace("{max_posts}", str(max_posts)).replace("{out_path}", str(out_path))


def agent_command(prompt: str, out_path: Path) -> list[str]:
    # File rules are written as Edit(...): a Write(path) rule is not matched and the write is denied.
    allowed = CHROME_TOOLS + [f"Edit(/{out_path})"]  # `//abs/path` is an absolute path rule
    return [
        "claude", "-p", "--chrome",
        "--tools", "Write",                 # no Bash, Read, WebFetch, …
        "--allowedTools", ",".join(allowed),
        "--permission-mode", "dontAsk",     # anything not allowed above is denied, never prompted
        "--no-session-persistence",
        prompt,
    ]


def _agent(prompt: str, out: Path) -> dict:
    """Run the browser agent once; it writes JSON to `out` and nothing else."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    note = ""
    try:
        res = run(agent_command(prompt, out), timeout=AGENT_TIMEOUT, cwd=str(out.parent))
        if res.returncode != 0:
            note = f"agent exited {res.returncode}: {(res.stderr or '').strip()[-200:]}"
    except subprocess.TimeoutExpired:
        note = "agent timed out"
    try:
        obj = json.loads(out.read_text())
        out.unlink()
    except FileNotFoundError:
        return {"status": "degraded", "notes": note or "agent wrote no file", "posts": []}
    except json.JSONDecodeError:
        out.unlink(missing_ok=True)
        return {"status": "degraded", "notes": "agent wrote invalid JSON", "posts": []}
    if note and isinstance(obj, dict):
        obj["notes"] = f"{obj.get('notes') or ''} ({note})".strip()
    return obj


def _run_agent(cfg: dict, started: datetime) -> dict:
    out = config.path("runs", ".incoming") / f"{started:%Y%m%d-%H%M%S}.json"
    return _agent(render_prompt(cfg["max_posts"], out), out)


def _run_file(started: datetime) -> Path:
    day = config.runs_dir() / f"{started:%Y-%m-%d}"
    p = day / f"{started:%H%M}.json"
    return p if not p.exists() else day / f"{started:%H%M%S}.json"


def save_capture(obj, started: datetime, ended: datetime | None = None) -> tuple[Path, dict]:
    """Validate, write runs/<date>/<HHMM>.json, pause on a LinkedIn check."""
    clean, errors = models.validate_capture(obj)
    clean["run_started_at"] = clean.get("run_started_at") or started.isoformat(timespec="seconds")
    clean["run_ended_at"] = clean.get("run_ended_at") or (ended or started).isoformat(timespec="seconds")
    if errors and clean["status"] not in PAUSING:
        clean["notes"] = (clean["notes"] + f" ({len(errors)} dropped: {errors[0]})").strip()[:1000]
    path = _run_file(started)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n")
    if clean["status"] in PAUSING:
        _pause(f"{clean['status']} at {clean['run_started_at']}: {clean['notes'] or 'LinkedIn asked for a check'}")
    return path, clean


def _started_from(obj) -> datetime:
    try:
        return datetime.fromisoformat(obj["run_started_at"])
    except (TypeError, KeyError, ValueError):
        return datetime.now().astimezone()


def _report(path: Path, clean: dict) -> int:
    rel = path.relative_to(config.home())
    print(f"{clean['status']}: {len(clean['posts'])} posts → {rel}")
    if clean["status"] in PAUSING:
        print("Paused. Nothing was clicked. Run `feedcast resume` when you're ready.")
        return 1
    return 1 if clean["status"] == "aborted_no_chrome" else 0


# ---- commands -----------------------------------------------------------------------------

def _locked(fn) -> int:
    """One browser agent at a time, and none while paused."""
    reason = paused_reason()
    if reason:
        print(f"Paused: {reason}\nRun `feedcast resume` when you're ready.")
        return 2
    lock_path = config.path("capture.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another capture is running.")
            return 0
        return fn()


def cmd_capture(args) -> int:
    if args.from_file:
        obj = json.loads(Path(args.from_file).expanduser().read_text())
        return _report(*save_capture(obj, _started_from(obj)))
    return _locked(lambda: _capture_live(args))


# ---- the user's own profile -----------------------------------------------------------------

def _strings(v, n: int, cap: int) -> list[str]:
    out = []
    for x in v if isinstance(v, list) else []:
        if isinstance(x, dict):  # {"title", "company"} for experience
            x = " at ".join(str(x[k]).strip() for k in ("title", "company") if x.get(k))
        if isinstance(x, str) and x.strip():
            out.append(" ".join(x.split())[:cap])
    return list(dict.fromkeys(out))[:n]


def clean_self(obj, now: datetime) -> dict:
    """What `profile` keeps from the self read: visible text only, capped."""
    obj = obj if isinstance(obj, dict) else {}
    text = lambda k, cap: " ".join(str(obj.get(k) or "").split())[:cap]  # noqa: E731
    return {"headline": text("headline", 300), "about": text("about", 2000),
            "experience": _strings(obj.get("experience"), 12, 160), "skills": _strings(obj.get("skills"), 40, 60),
            "read_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def read_self() -> tuple[str, dict | None]:
    """Open linkedin.com/in/me in a new tab, read it without clicking, write linkedin-self.json.
    Returns (status, cleaned profile or None)."""
    now = datetime.now().astimezone()
    if not chrome_running():
        return "aborted_no_chrome", None
    out = config.path(".incoming") / f"self-{now:%Y%m%d-%H%M%S}.json"
    obj = _agent(SELF_PROMPT.read_text().replace("{out_path}", str(out)), out)
    status = obj.get("status") if isinstance(obj, dict) else "degraded"
    if status in PAUSING:
        _pause(f"{status} at {now.isoformat(timespec='seconds')} (own profile): {obj.get('notes') or 'LinkedIn asked for a check'}")
        return status, None
    clean = clean_self(obj, now)
    if status != "ok" or not (clean["headline"] or clean["experience"] or clean["skills"]):
        return status or "degraded", None
    p = config.linkedin_self_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n")
    return "ok", clean


def cmd_read_self(args) -> int:
    def go() -> int:
        status, clean = read_self()
        if clean:
            print(f"Read your LinkedIn profile: {len(clean['experience'])} roles, {len(clean['skills'])} skills "
                  f"→ {config.linkedin_self_path().name}. Run feedcast profile to use it.")
            return 0
        if status in PAUSING:
            print("Paused. Nothing was clicked. Run `feedcast resume` when you're ready.")
        else:
            print("Chrome isn't running." if status == "aborted_no_chrome" else "Couldn't read your LinkedIn profile.")
        return 1
    return _locked(go)


def _capture_live(args) -> int:
    if args.scheduled and not args.now:
        SLEEP(random.uniform(0, MAX_DELAY))  # don't hit the feed on the same minute every day
    now = datetime.now().astimezone()
    if not chrome_running():
        return _report(*save_capture({"status": "aborted_no_chrome", "notes": "Chrome wasn't running", "posts": []}, now))
    if battery_low():
        print(f"Skipped: on battery under {MIN_BATTERY}%.")
        return 0
    obj = _run_agent(config.load(), now)
    return _report(*save_capture(obj, now, datetime.now().astimezone()))


def cmd_resume(args) -> int:
    reason = paused_reason()
    if reason is None:
        print("Not paused.")
        return 0
    config.paused_path().unlink()
    print(f"Removed PAUSED ({reason}). Captures will run again.")
    return 0


# ---- init ---------------------------------------------------------------------------------

def _ask(prompt: str) -> str:
    return INPUT(prompt).strip()


def _yes(prompt: str, default: bool) -> bool:
    ans = _ask(prompt).lower()
    return default if not ans else ans.startswith("y")


def _ask_roots(current: list[str]) -> list[str]:
    hint = f" [{', '.join(current)}]" if current else ""
    while True:
        raw = _ask(f"Work folders, comma separated{hint}: ")
        if not raw and current:
            return current
        roots = [str(Path(r.strip()).expanduser()) for r in raw.split(",") if r.strip()]
        missing = [r for r in roots if not Path(r).is_dir()]
        if roots and not missing:
            return roots
        print(f"Not found: {', '.join(missing)}" if missing else "Name at least one folder.")


def store_key(key: str) -> bool:
    """Keychain item feedcast/anthropic. The key goes in on stdin to `security -i`, never in argv."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", key):
        print("That doesn't look like an API key.")
        return False
    res = run(["security", "-i"], input=f'add-generic-password -U -s feedcast -a anthropic -w "{key}"\n')
    ok = res.returncode == 0 and not (res.stderr or "").strip()
    print("Key saved to Keychain." if ok else "Couldn't save the key to Keychain.")
    return ok


def cmd_init(args) -> int:
    from . import doctor, schedule, setup  # they import capture for `run`

    if not args.terminal:
        return setup.open_setup()
    cfg = config.load()
    cfg["roots"] = _ask_roots(cfg["roots"])
    cfg["use_sessions"] = _yes(f"Use Claude Code sessions as a signal? [{'Y/n' if cfg['use_sessions'] else 'y/N'}] ", cfg["use_sessions"])
    config.save(cfg)
    key = GETPASS("Anthropic API key (Enter to keep the current one): ").strip()
    if key:
        store_key(key)
    if _yes("Install the schedule? [Y/n] ", True):
        schedule.install(cfg)
    print()
    doctor.run_checks()
    print()
    if _yes("First read is supervised. Run it now and watch? [y/N] ", False):
        return cmd_capture(argparse.Namespace(from_file=None, now=True, scheduled=False))
    return 0


def register(sub) -> None:
    p = sub.add_parser("init", help="set up Feedcast (opens the setup page)")
    p.add_argument("--terminal", action="store_true", help="answer the setup questions here instead")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("capture", help="read the feed once")
    p.add_argument("--now", action="store_true", help="no start delay")
    p.add_argument("--scheduled", action="store_true", help="run from launchd: random start delay")
    p.add_argument("--from-file", metavar="F", help="import a capture file instead of reading the feed")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("resume", help="allow captures again after a pause")
    p.set_defaults(func=cmd_resume)
