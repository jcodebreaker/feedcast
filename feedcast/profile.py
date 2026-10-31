"""Interest profile (CONTRACT §3): who the reader is, seeded from local work and their own LinkedIn profile.

Local repos and session titles are only signals for one Haiku call that describes the person (role, stack,
interests). Project names never reach compact_text(), which is what every later call sees.
Only `signals_text()` (to the profile call) and `compact_text()` (to every later call) ever leave the machine.
Source files and diffs are never opened: from a repo we read the README, manifest files and commit subjects.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import config, db, llm

WINDOW = timedelta(days=14)
MAX_DEPTH = 3
SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", "target", "dist", "build"}
MAX_REPOS = 15
MAX_SESSIONS = 40
MAX_SESSION_LINES = 5000  # stop reading a transcript early; titles and the first prompt sit near the top
MAX_SIGNALS = 16000
MAX_COMPACT = 2500
DECAY = 0.9
MIN_WEIGHT = 0.1
PROMPT = Path(__file__).resolve().parent / "prompts" / "profile.md"
LINKEDIN_MAX_AGE = timedelta(days=30)
NO_ROOTS = "No work folders set. Add them to ~/.feedcast/config.yaml under roots, or run feedcast init."
NO_LINKEDIN = "Run feedcast profile --linkedin once to add your LinkedIn profile."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ---------- git ----------

def _git(repo: Path, *args: str) -> str:
    # No shell; --end-of-options keeps any later value from being read as a flag.
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args, "--end-of-options"],
            capture_output=True, text=True, timeout=15, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def find_repos(root: Path) -> list[Path]:
    """Git repos at most MAX_DEPTH levels under root, skipping hidden and dependency folders."""
    found: list[Path] = []

    def walk(d: Path, depth: int) -> None:
        if (d / ".git").exists():
            found.append(d)
            return
        if depth >= MAX_DEPTH:
            return
        try:
            children = sorted(d.iterdir())
        except OSError:
            return
        for c in children:
            if c.name.startswith(".") or c.name in SKIP_DIRS or c.is_symlink() or not c.is_dir():
                continue
            walk(c, depth + 1)

    if root.is_dir():
        walk(root, 0)
    return found


def _readme(repo: Path) -> str:
    for name in ("README.md", "readme.md", "Readme.md", "README.rst", "README.txt", "README"):
        p = repo / name
        if p.is_file():
            with p.open(errors="replace") as f:
                text = f.read(20000)
            break
    else:
        return ""
    heading, para = "", []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            if para:
                break
            heading = heading or s.lstrip("#").strip()
        elif not s or s.startswith(("<", "![", "[![", "---", "===", "```")):
            if para:
                break
        else:
            para.append(s)
    return _clip(" — ".join(x for x in (heading, " ".join(para)) if x), 400)


def _dep_name(spec: str) -> str:
    return re.split(r"[\s<>=!~;\[(@]", spec.strip(), maxsplit=1)[0]


def _deps(repo: Path) -> list[str]:
    names: list[str] = []
    try:
        if (repo / "package.json").is_file():
            pkg = json.loads((repo / "package.json").read_text(errors="replace"))
            for k in ("dependencies", "devDependencies", "peerDependencies"):
                names += list((pkg.get(k) or {}).keys())
        if (repo / "pyproject.toml").is_file():
            py = tomllib.loads((repo / "pyproject.toml").read_text(errors="replace"))
            proj = py.get("project", {})
            names += [_dep_name(d) for d in proj.get("dependencies", [])]
            for group in (proj.get("optional-dependencies") or {}).values():
                names += [_dep_name(d) for d in group]
            names += [k for k in py.get("tool", {}).get("poetry", {}).get("dependencies", {}) if k != "python"]
        if (repo / "Cargo.toml").is_file():
            cargo = tomllib.loads((repo / "Cargo.toml").read_text(errors="replace"))
            for k in ("dependencies", "dev-dependencies"):
                names += list((cargo.get(k) or {}).keys())
        if (repo / "go.mod").is_file():
            block = False
            for line in (repo / "go.mod").read_text(errors="replace").splitlines():
                s = line.split("//")[0].strip()
                if s.startswith("require ("):
                    block = True
                elif block and s == ")":
                    block = False
                elif block and s:
                    names.append(s.split()[0])
                elif s.startswith("require "):
                    names.append(s.split()[1])
    except (ValueError, OSError, AttributeError, TypeError, IndexError):
        pass  # a broken manifest just means fewer names
    return list(dict.fromkeys(n for n in names if n))[:40]


def read_repos(roots: list, now: datetime) -> list[dict]:
    """Repos with a commit in the last 14 days, newest first."""
    repos = []
    for root in roots:
        for repo in find_repos(Path(os.path.expanduser(str(root)))):
            last = _git(repo, "log", "-1", "--format=%ct").strip()
            if not last.isdigit():
                continue
            last_at = datetime.fromtimestamp(int(last), timezone.utc)
            if now - last_at > WINDOW:
                continue
            repos.append({
                "name": repo.name,
                "last_commit": last_at,
                "readme": _readme(repo),
                "deps": _deps(repo),
                "commits": [s for s in _git(repo, "log", "-n", "30", "--format=%s").splitlines() if s.strip()],
            })
    repos.sort(key=lambda r: r["last_commit"], reverse=True)
    return repos[:MAX_REPOS]


# ---------- Claude Code sessions ----------

def sessions_dir() -> Path:
    return Path(os.environ.get("FEEDCAST_CLAUDE_PROJECTS") or Path.home() / ".claude" / "projects")


TITLE_KEYS = {"custom-title": "customTitle", "summary": "summary", "ai-title": "aiTitle", "title": "title"}


def _prompt_text(msg) -> str:
    content = (msg or {}).get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str):
        return ""
    s = content.strip()
    # Slash commands, hook output and reminders are wrapped in tags; they aren't what the user asked.
    return "" if not s or s.startswith("<") else s


def _session(path: Path) -> dict:
    title, prompt = "", ""
    with path.open(errors="replace") as f:
        for i, line in enumerate(f):
            if i >= MAX_SESSION_LINES or (title and prompt):
                break
            # Cheap substring check first so long tool lines are never parsed.
            if '"user"' not in line and "title" not in line and '"summary"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            if not title and kind in TITLE_KEYS and isinstance(obj.get(TITLE_KEYS[kind]), str):
                title = _clip(obj[TITLE_KEYS[kind]], 120)
            elif not prompt and kind == "user" and not obj.get("isMeta") and not obj.get("isSidechain"):
                prompt = _clip(_prompt_text(obj.get("message")), 200)
    return {"project": path.parent.name, "title": title, "prompt": prompt}


def read_sessions(now: datetime, base: Path | None = None) -> list[dict]:
    base = base or sessions_dir()
    if not base.is_dir():
        return []
    recent = []
    for p in base.glob("*/*.jsonl"):
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
        except OSError:
            continue
        if now - mtime <= WINDOW:
            recent.append((mtime, p))
    recent.sort(reverse=True)
    out = []
    for mtime, p in recent[:MAX_SESSIONS]:
        try:
            s = _session(p)
        except OSError:
            continue
        if s["title"] or s["prompt"]:
            out.append({**s, "modified": mtime})
    return out


# ---------- feedback ----------

def read_feedback() -> list[dict]:
    if not config.db_path().exists():
        return []
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT f.verdict, p.topic, p.takeaway FROM feedback f LEFT JOIN posts p ON p.key = f.post_key "
            "ORDER BY f.at DESC LIMIT 30"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ---------- the reader's own LinkedIn profile (written by `feedcast profile --linkedin`) ----------

def read_linkedin(now: datetime) -> dict | None:
    """linkedin-self.json when present and younger than 30 days."""
    try:
        data = json.loads(config.linkedin_self_path().read_text())
    except (FileNotFoundError, ValueError):
        return None
    read_at = _parse_iso(data.get("read_at")) if isinstance(data, dict) else None
    if not read_at or now - read_at > LINKEDIN_MAX_AGE:
        return None
    return {"headline": data.get("headline") or "", "about": data.get("about") or "",
            "experience": list(data.get("experience") or []), "skills": list(data.get("skills") or []),
            "read_at": data["read_at"]}


# ---------- signals ----------

def gather(cfg: dict, now: datetime) -> dict:
    return {
        "repos": read_repos(cfg.get("roots") or [], now),
        "sessions": read_sessions(now) if cfg.get("use_sessions") else [],
        "feedback": read_feedback(),
        "linkedin": read_linkedin(now),
    }


def signals_text(signals: dict, current: dict | None = None) -> str:
    """The exact text the profile call sends. `feedcast profile --dry-run` prints it."""
    out = []
    li = signals.get("linkedin")
    if li:
        out.append("## Their LinkedIn profile")
        if li["headline"]:
            out.append(f"Headline: {_clip(li['headline'], 200)}")
        if li["about"]:
            out.append(f"About: {_clip(li['about'], 1200)}")
        if li["experience"]:
            out.append("Experience:\n" + "\n".join(f"- {_clip(e, 120)}" for e in li["experience"][:10]))
        if li["skills"]:
            out.append("Skills: " + ", ".join(_clip(x, 40) for x in li["skills"][:30]))
        out.append("")
    for r in signals["repos"]:
        out.append(f"## repo {r['name']} (last commit {r['last_commit']:%Y-%m-%d})")
        if r["readme"]:
            out.append(f"README: {r['readme']}")
        if r["deps"]:
            out.append("Dependencies: " + ", ".join(r["deps"]))
        if r["commits"]:
            out.append("Commits:\n" + "\n".join(f"- {_clip(c, 120)}" for c in r["commits"]))
        out.append("")
    if signals["sessions"]:
        out.append("## Claude Code sessions")
        for s in signals["sessions"]:
            bits = [x for x in (s["title"], s["prompt"] and f'"{s["prompt"]}"') if x]
            out.append(f"- [{s['project']}] " + " · ".join(bits))
        out.append("")
    if signals["feedback"]:
        out.append("## Feedback on posts")
        for f in signals["feedback"]:
            out.append(f"- {f['verdict']}: [{f['topic'] or '-'}] {_clip(f['takeaway'] or '', 150)}")
        out.append("")
    if current:
        out.append("## Current profile")
        out.append(compact_text(current))
    text = "\n".join(out).strip()
    return text[:MAX_SIGNALS]


# ---------- profile ----------

def load() -> dict | None:
    try:
        return yaml.safe_load(config.profile_path().read_text()) or {}
    except FileNotFoundError:
        return None


def save(profile: dict) -> None:
    p = config.profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, width=120))


def _key(s) -> str:
    return " ".join(str(s or "").lower().split())


def _clean_auto(reply: dict) -> dict:
    """Keep only well-formed model output, marked auto."""
    interests = []
    for i in reply.get("interests") or []:
        if isinstance(i, dict) and str(i.get("topic") or "").strip():
            try:
                w = min(max(float(i.get("weight", 0.5)), 0.0), 1.0)
            except (TypeError, ValueError):
                w = 0.5
            interests.append({"topic": _clip(i["topic"], 80), "weight": round(w, 2), "auto": True})
    strings = lambda v: [_clip(x, 80) for x in (v or []) if isinstance(x, str) and x.strip()]  # noqa: E731
    role = _clip(reply["role"], 140) if isinstance(reply.get("role"), str) else ""
    return {"role": role, "stack": strings(reply.get("stack"))[:15],
            "interests": interests[:25], "not_interested": strings(reply.get("not_interested"))}


def merge(old: dict | None, reply: dict, signals: dict, cfg: dict, now: datetime) -> dict:
    """Hand entries (no `auto`) always survive and win clashes; auto entries are replaced.
    Old auto interests the model no longer returns fade by 10% a day instead of vanishing."""
    old = old or {}
    new = _clean_auto(reply)

    hand_interests = [i for i in old.get("interests") or [] if isinstance(i, dict) and not i.get("auto")]
    auto = {_key(i["topic"]): i for i in new["interests"]}
    since = _parse_iso(old.get("generated_at"))
    if since:
        days = max((now - since).total_seconds() / 86400, 0)
        for i in old.get("interests") or []:
            if isinstance(i, dict) and i.get("auto") and i.get("topic") and _key(i["topic"]) not in auto:
                auto[_key(i["topic"])] = {"topic": i["topic"], "weight": round(float(i.get("weight") or 0) * DECAY ** days, 2),
                                          "auto": True}
    taken = {_key(i.get("topic")) for i in hand_interests}
    interests = hand_interests + sorted(
        (i for k, i in auto.items() if k not in taken and i["weight"] >= MIN_WEIGHT),
        key=lambda i: -i["weight"],
    )

    def union(*lists):
        return list(dict.fromkeys(x for xs in lists for x in (xs or []) if isinstance(x, str) and x.strip()))

    out = {
        "generated_at": _iso(now),
        # A role typed on the setup page (role_hand) is the user's; otherwise the model's latest wins.
        "role": (str(old["role"]) if old.get("role_hand") and old.get("role") else new["role"] or str(old.get("role") or "")),
        "stack": new["stack"] or list(old.get("stack") or []),
        "interests": interests,
        # Plain strings carry no `auto` mark, so what was there before is kept as a possible hand edit.
        "not_interested": union(old.get("not_interested"), new["not_interested"]),
        "pins": union(cfg.get("pins"), old.get("pins")),
        "mutes": union(cfg.get("mutes"), old.get("mutes")),
    }
    if old.get("role_hand") and old.get("role"):
        out["role_hand"] = True
    li = signals.get("linkedin") or old.get("linkedin")
    if li:
        out["linkedin"] = li
    return out


def compact_text(profile: dict, cfg: dict | None = None) -> str:
    """Who the reader is, the only profile text ever sent with a request. At most 2,500 characters.
    It describes the person, never their projects."""
    cfg = cfg or {}

    def strings(key):
        return list(dict.fromkeys([*(profile.get(key) or []), *(cfg.get(key) or [])]))

    interests = sorted((i for i in profile.get("interests") or [] if isinstance(i, dict)),
                       key=lambda i: -float(i.get("weight") or 0))[:25]
    headline = (profile.get("linkedin") or {}).get("headline") if isinstance(profile.get("linkedin"), dict) else None

    def render(interests):
        lines = []
        if profile.get("role"):
            lines.append(f"Role: {_clip(profile['role'], 140)}")
        if headline:
            lines.append(f"LinkedIn headline: {_clip(headline, 200)}")
        if profile.get("stack"):
            lines.append("Stack: " + ", ".join(_clip(x, 40) for x in profile["stack"]))
        if interests:
            lines.append("Interests:")
            lines += [f"- {_clip(i.get('topic'), 80)} {float(i.get('weight') or 0):.1f}" for i in interests]
        for label, key in (("Pins", "pins"), ("Mutes", "mutes")):
            if strings(key):
                lines.append(f"{label}: " + ", ".join(_clip(x, 80) for x in strings(key)))
        if profile.get("not_interested"):
            lines.append("Not interested: " + ", ".join(_clip(x, 80) for x in profile["not_interested"]))
        return "\n".join(lines)

    text = render(interests)
    while len(text) > MAX_COMPACT and interests:
        interests = interests[:-1]
        text = render(interests)
    return text[:MAX_COMPACT]


def refresh(cfg: dict, now: datetime | None = None) -> tuple[dict, dict]:
    """Read signals, make the one profile call, merge, write profile.yaml."""
    now = now or _now()
    signals = gather(cfg, now)
    old = load()
    reply = llm.complete_json("profile", cfg["models"]["classify"], PROMPT.read_text(),
                              signals_text(signals, old), max_tokens=2000)
    profile = merge(old, reply if isinstance(reply, dict) else {}, signals, cfg, now)
    save(profile)
    return profile, signals


# ---------- CLI ----------

def _cmd(args) -> int:
    cfg = config.load()
    if args.linkedin:
        from . import capture  # owns the browser agent
        return capture.cmd_read_self(args)
    if args.show:
        current = load()
        if current is None:
            print("No profile yet. Run feedcast profile.", file=sys.stderr)
            return 1
        print(compact_text(current, cfg))
        return 0
    has_linkedin = read_linkedin(_now()) is not None
    if not cfg.get("roots") and not has_linkedin:
        print(NO_ROOTS, file=sys.stderr)
        return 1
    if args.dry_run:
        print(signals_text(gather(cfg, _now()), load()))
        return 0
    profile, signals = refresh(cfg)
    topics = ", ".join(i["topic"] for i in profile["interests"][:5]) or "none yet"
    print(f"Profile written to {config.profile_path()}")
    print(f"Role: {profile['role'] or 'unknown'}")
    print(f"Interests: {topics}")
    print(f"Read {len(signals['repos'])} repos and {len(signals['sessions'])} sessions.")
    if not signals["linkedin"]:
        print(NO_LINKEDIN)
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("profile", help="Rebuild your interest profile from local work and your LinkedIn profile.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="Print what would be sent, without sending it.")
    g.add_argument("--show", action="store_true", help="Print the profile text sent with each request.")
    g.add_argument("--linkedin", action="store_true", help="Read your own LinkedIn profile once, in your Chrome.")
    p.set_defaults(func=_cmd)
