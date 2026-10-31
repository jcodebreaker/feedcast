"""The setup page (`GET /setup` on `feedcast serve`) and the actions behind it, plus `feedcast setup`.

The page is rendered from the files Feedcast already keeps (config.yaml, profile.yaml, linkedin-self.json,
the runs table, the launchd plists). Each action edits one of them and the page reloads. Long work
(reading the feed, reading the user's own profile, rebuilding the profile) runs as one background
`feedcast` job at a time through SPAWN, which tests replace.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
from datetime import datetime
from html import escape
from pathlib import Path
from string import Template

from . import capture, config, db, feedback, profile, schedule

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "templates" / "setup.html"
STYLE = Path(__file__).resolve().parent / "templates" / "style.css"
CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_CAPTURES = 4
THRESHOLDS = {1: "1+", 2: "2+", 3: "3 only"}
JOBS = {
    "linkedin": ("{fc} profile --linkedin", "Reading…"),
    "profile": ("{fc} profile", "Rebuilding…"),
    "read": ("{fc} capture --now && {fc} ingest && {fc} report --no-open", "Reading…"),
}


# ---------- background jobs ----------

def _default_spawn(name: str, shell: str):
    log = config.path("logs", "setup-job.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        return subprocess.Popen(["/bin/sh", "-c", shell], stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True, env=os.environ.copy())


SPAWN = _default_spawn  # (name, shell) -> object with poll(); tests replace it
_job: dict = {"name": None, "proc": None, "last": None}


def job_state() -> dict:
    proc = _job["proc"]
    if proc is not None and (code := proc.poll()) is not None:
        _job.update(name=None, proc=None, last={"name": _job["name"], "code": code})
    return {"running": _job["name"], "last": _job["last"]}


def start_job(name: str) -> tuple[int, dict]:
    if name not in JOBS:
        return 400, {"error": "unknown job"}
    if job_state()["running"]:
        return 409, {"error": "Something is already running."}
    if name != "profile" and capture.paused_reason():
        return 409, {"error": "Paused. Resume first."}
    shell = JOBS[name][0].format(fc=schedule.feedcast_cmd())
    _job.update(name=name, proc=SPAWN(name, shell))
    return 200, job_state()


# ---------- actions ----------

def _text(v, cap: int) -> str | None:
    return " ".join(v.split())[:cap] if isinstance(v, str) and v.strip() else None


def _quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()):
        fn(*args)


def _roots(body: dict) -> tuple[int, dict]:
    cfg = config.load()
    if add := _text(body.get("add"), 500):
        p = Path(add).expanduser()
        if not p.is_dir():
            return 400, {"error": f"No folder at {add}"}
        if str(p) not in cfg["roots"]:
            cfg["roots"] = [*cfg["roots"], str(p)]
    elif isinstance(body.get("remove"), str):
        cfg["roots"] = [r for r in cfg["roots"] if r != body["remove"]]
    else:
        return 400, {"error": "add or remove"}
    config.save(cfg)
    return 200, {"ok": True}


def _sessions(body: dict) -> tuple[int, dict]:
    if not isinstance(body.get("on"), bool):
        return 400, {"error": "on must be true or false"}
    cfg = config.load()
    cfg["use_sessions"] = body["on"]
    config.save(cfg)
    return 200, {"ok": True}


def _profile(body: dict) -> tuple[int, dict]:
    """Hand edits: entries added here carry no `auto`, so a refresh keeps them; a role set here is kept too."""
    prof = profile.load() or {}
    key = profile._key
    interests = [i for i in prof.get("interests") or [] if isinstance(i, dict)]
    never = [x for x in prof.get("not_interested") or [] if isinstance(x, str)]
    if "role" in body:
        role = _text(body.get("role"), 140)
        if not role:
            return 400, {"error": "Say what you do"}
        prof.update(role=role, role_hand=True)
    elif t := _text(body.get("add_interest"), 80):
        if not any(key(i.get("topic")) == key(t) for i in interests):
            interests.append({"topic": t, "weight": 0.8})
    elif t := _text(body.get("remove_interest"), 200):
        interests = [i for i in interests if key(i.get("topic")) != key(t)]
    elif t := _text(body.get("add_never"), 80):
        never = list(dict.fromkeys([*never, t]))
    elif t := _text(body.get("remove_never"), 200):
        never = [x for x in never if key(x) != key(t)]
    else:
        return 400, {"error": "nothing to change"}
    prof["interests"], prof["not_interested"] = interests, never
    profile.save(prof)
    return 200, {"ok": True}


def _schedule(body: dict) -> tuple[int, dict]:
    cfg = config.load()
    s = cfg["schedule"]
    if "captures" in body:
        caps = body["captures"] if isinstance(body["captures"], list) else None
        if not caps or not all(isinstance(t, str) and CLOCK.match(t) for t in caps) or len(set(caps)) > MAX_CAPTURES:
            return 400, {"error": f"One to {MAX_CAPTURES} times, like 09:00"}
        s["captures"] = sorted(set(caps))
    if "digest" in body:
        if not (isinstance(body["digest"], str) and CLOCK.match(body["digest"])):
            return 400, {"error": "A time like 19:30"}
        s["digest"] = body["digest"]
    if "threshold" in body:
        if body["threshold"] not in THRESHOLDS or isinstance(body["threshold"], bool):
            return 400, {"error": "1, 2 or 3"}
        cfg["relevance_threshold"] = body["threshold"]
    config.save(cfg)
    if schedule.installed() and ("captures" in body or "digest" in body):
        _quiet(schedule.install, cfg)  # new times take effect now
    return 200, {"ok": True}


def action(name: str, body) -> tuple[int, dict]:
    """POST /api/<name> with a JSON object. Returns (HTTP status, JSON reply)."""
    body = body if isinstance(body, dict) else {}
    if name == "roots":
        return _roots(body)
    if name == "sessions":
        return _sessions(body)
    if name == "profile":
        return _profile(body)
    if name == "schedule":
        return _schedule(body)
    if name == "schedule/on":
        _quiet(schedule.install, config.load())
        return 200, {"ok": True}
    if name == "schedule/off":
        _quiet(schedule.remove)
        return 200, {"ok": True}
    if name == "jobs":
        return start_job(body.get("job"))
    if name == "resume":
        config.paused_path().unlink(missing_ok=True)
        return 200, {"ok": True}
    return 404, {"error": "not found"}


# ---------- page ----------

def next_read(captures: list[str], now: datetime) -> str | None:
    times = sorted(t for t in captures if CLOCK.match(t))
    return next((t for t in times if t > f"{now:%H:%M}"), times[0] if times else None)


def status_line(cfg: dict, now: datetime | None = None) -> tuple[bool, str]:
    if not schedule.installed():
        return False, "Daily reads off"
    nxt = next_read(cfg["schedule"]["captures"], now or datetime.now())
    return True, "Daily reads on" + (f" · next read {nxt}" if nxt else "")


def _linkedin() -> dict | None:
    try:
        li = json.loads(config.linkedin_self_path().read_text())
        return li if isinstance(li, dict) else None
    except (OSError, ValueError):
        return None


def _runs() -> list:
    conn = db.connect()
    try:
        return conn.execute("SELECT started_at, posts_seen, posts_new, status FROM runs ORDER BY started_at DESC LIMIT 8").fetchall()
    finally:
        conn.close()


def _when(iso: str | None, today: str) -> str:
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return "?"
    dt = dt.astimezone() if dt.tzinfo else dt
    return f"{dt:%H:%M}" if dt.date().isoformat() == today else f"{dt:%d %b}"


def _latest_report() -> str:
    pages = sorted(p.stem for p in config.reports_dir().glob("????-??-??.html"))
    return f"/reports/{pages[-1]}.html" if pages else "/reports/index.html"


def _n(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _x(label: str, act: str, body: dict) -> str:
    return (f'<button type="button" aria-label="Remove {escape(label)}" data-act="{act}" '
            f'data-body="{escape(json.dumps(body))}">×</button>')


def _job_button(name: str, label: str, cls: str, state: dict, blocked: bool = False) -> str:
    busy = state["running"] == name
    off = " disabled" if state["running"] or blocked else ""
    return (f'<button class="btn{cls}" type="button" data-job="{name}" data-busy="{escape(JOBS[name][1])}"{off}>'
            f'{escape(JOBS[name][1] if busy else label)}</button>')


def render_page(token: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    cfg = config.load()
    prof = profile.load()
    li = _linkedin()
    runs = _runs()
    state = job_state()
    paused = capture.paused_reason()
    on, status = status_line(cfg, now)
    today = now.date().isoformat()

    roots = [r for r in cfg["roots"] if isinstance(r, str)]
    repos = sum(len(profile.find_repos(Path(r).expanduser())) for r in roots if Path(r).expanduser().is_dir())
    root_chips = "".join(f'<span class="chip mono">{escape(r)}{_x(r, "roots", {"remove": r})}</span>' for r in roots)
    work = (f'<section class="pane" id="work"><header><h2>Your work</h2>'
            + (f'<span class="ok-pill">{_n(repos, "repo")} found</span>' if roots else "")
            + '</header><p class="sub">Folders with your code. Feedcast reads repo names, READMEs, dependencies and '
              'commit titles. Never your code.</p>'
            f'<div class="chips">{root_chips}<button class="chip add" type="button" data-reveal="root-in">+ Add folder</button>'
            '<input class="chip in mono" id="root-in" hidden data-enter="roots" data-field="add" placeholder="~/code" '
            'aria-label="Folder path"></div>'
            f'<label class="toggle"><input type="checkbox" data-toggle="sessions"{" checked" if cfg["use_sessions"] else ""}>'
            '<span></span>Also use Claude Code session titles</label></section>')

    if li:
        initial = escape((li.get("headline") or "?").strip()[:1].upper())
        about = str(li.get("about") or "")
        linked = (f'<div class="li"><span class="av lg" style="--h:210">{initial}</span><div>'
                  f'<b>{escape(str(li.get("headline") or ""))}</b>'
                  + (f'<p class="sub">{escape(about[:180])}{"…" if len(about) > 180 else ""}</p>' if about else "")
                  + f'<p class="mono small">{_n(len(li.get("experience") or []), "role")} · {_n(len(li.get("skills") or []), "skill")}'
                    f' · read {escape(str(li.get("read_at") or "")[:10])}</p></div>'
                  f'{_job_button("linkedin", "Read again", "", state, bool(paused))}</div>')
    else:
        linked = ('<div class="li"><span class="av lg" style="--h:210">in</span><div><p class="sub">Your own profile, read once '
                  'in your Chrome, tells Feedcast what you do.</p></div>'
                  f'{_job_button("linkedin", "Read my profile", " primary", state, bool(paused))}</div>')
    linkedin = f'<section class="pane" id="linkedin"><header><h2>LinkedIn</h2></header>{linked}</section>'

    p = prof or {}
    ints = "".join(
        f'<li class="int"><span class="t" title="{escape(str(i.get("topic")))}">{escape(str(i.get("topic")))}</span>'
        f'<span class="w"><i style="--v:{min(max(float(i.get("weight") or 0), 0), 1):.2f}"></i></span>'
        f'{_x(str(i.get("topic")), "profile", {"remove_interest": str(i.get("topic"))})}</li>'
        for i in p.get("interests") or [] if isinstance(i, dict) and i.get("topic"))
    never = "".join(f'<span class="chip">{escape(x)}{_x(x, "profile", {"remove_never": x})}</span>'
                    for x in p.get("not_interested") or [] if isinstance(x, str))
    sees = profile.compact_text(prof, cfg) if prof else ""
    interests = (
        '<section class="pane" id="interests"><header><h2>What you care about</h2>'
        f'{_job_button("profile", "Rebuild from my work", " ghost", state)}</header>'
        f'<input class="role" value="{escape(str(p.get("role") or ""))}" placeholder="What you do, e.g. AI engineer: agents, evals" '
        'aria-label="Your role" data-change="profile" data-field="role">'
        f'<ul class="ints">{ints}<li class="int add"><input placeholder="Add a topic, e.g. browser agents" '
        'aria-label="Add a topic" data-enter="profile" data-field="add_interest"></li></ul>'
        f'<h4>Never show</h4><div class="chips">{never}<button class="chip add" type="button" data-reveal="never-in">+ Add</button>'
        '<input class="chip in" id="never-in" hidden data-enter="profile" data-field="add_never" placeholder="crypto" '
        'aria-label="Never show"></div>'
        + (f'<details class="sent"><summary>What Claude sees about you</summary><pre>{escape(sees)}</pre></details>' if sees else "")
        + "</section>")

    s = cfg["schedule"]
    times = "".join(f'<input class="time" type="time" value="{escape(t)}" data-cap aria-label="Read at">' for t in s["captures"])
    seg = "".join(f'<button type="button" class="{"on" if cfg["relevance_threshold"] == n else ""}" data-act="schedule" '
                  f'data-body="{escape(json.dumps({"threshold": n}))}">{label}</button>' for n, label in THRESHOLDS.items())
    toggle = ('<button class="btn" type="button" data-act="schedule/off">Turn off</button>' if on else
              '<button class="btn primary" type="button" data-act="schedule/on">Turn on daily reads</button>')
    sched = (
        '<section class="pane" id="schedule"><header><h2>Schedule</h2>'
        + ('<span class="ok-pill">On</span>' if on else '<span class="warn-pill">Not installed</span>')
        + f'</header><div class="sched"><div><h4>Reads your feed</h4><div class="times" id="caps">{times}'
          f'<button class="time add" type="button" data-addtime aria-label="Add a read">+</button></div></div>'
          f'<div><h4>Evening page</h4><div class="times"><input class="time" type="time" value="{escape(s["digest"])}" '
          f'data-digest aria-label="Evening page at"></div></div>'
          f'<div><h4>Show items scoring</h4><div class="seg">{seg}</div></div></div>{toggle}</section>')

    rows = "".join(
        f'<li><span class="dot {"ok" if r["status"] == "ok" else "warn"}"></span><b>{escape(_when(r["started_at"], today))}</b>'
        f'<span>{r["posts_seen"] or 0} posts · {r["posts_new"] or 0} new</span><span class="st">{escape(str(r["status"]).replace("_", " "))}</span></li>'
        for r in runs)
    banner = (f'<div class="banner"><span>Paused: {escape(paused)}</span>'
              '<button class="btn primary" type="button" data-act="resume">Resume</button></div>' if paused else "")
    last = state["last"]
    failed = (f'<p class="sub small warn-text">The last {"read" if last["name"] == "read" else "job"} stopped early. '
              'See logs/setup-job.log.</p>' if last and last["code"] else "")
    reads = (f'<section class="pane" id="reads"><header><h2>Reads</h2>{_job_button("read", "Read now", " primary", state, bool(paused))}'
             f'</header>{banner}{failed}' + (f'<ul class="runs">{rows}</ul>' if rows else "")
             + '<p class="sub small">Stops on any LinkedIn security check and waits for you. Nothing is ever clicked, liked or followed.</p></section>')

    done = {"work": bool(roots), "linkedin": bool(li), "interests": bool(prof), "schedule": on, "reads": bool(runs)}
    done_cls = ' class="done"'
    steps = "".join(f'<a href="#{k}"{done_cls if done[k] else ""}><b>{i}</b>{label}</a>' for i, (k, label) in enumerate(
        [("work", "Your work"), ("linkedin", "LinkedIn"), ("interests", "What you care about"), ("schedule", "Schedule"),
         ("reads", "Reads")], 1))
    dot = "" if on else ' style="background:var(--warn);box-shadow:none"'
    body = (f'<header class="top"><div class="wrap row"><a class="logo" href="{_latest_report()}"><span class="mark"></span>Feedcast</a>'
            f'<span class="status"><i{dot}></i>{escape(status)}</span>'
            f'<a class="btn ghost" href="{_latest_report()}">Tonight\'s page</a></div></header>'
            f'<main class="wrap two"><nav class="steps">{steps}</nav><div class="panes">'
            f'{work}{linkedin}{interests}{sched}{reads}</div></main>')
    return Template(TEMPLATE.read_text()).substitute(
        css=STYLE.read_text(), token=escape(token), running="1" if state["running"] else "", body=body)


# ---------- CLI ----------

def open_setup(port: int | None = None) -> int:
    port = port or feedback.PORT  # feedback imports this module, so read its PORT at call time
    url = f"http://127.0.0.1:{port}/setup"
    if not feedback.ensure_server(port):
        print(f"Couldn't start feedcast serve. Run it, then open {url}")
        return 1
    capture.run(["open", url])
    print(url)
    return 0


def register(sub) -> None:
    p = sub.add_parser("setup", help="open the setup page")
    p.set_defaults(func=lambda args: open_setup())
