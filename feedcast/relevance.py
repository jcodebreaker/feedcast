"""Score new posts against the interest profile: one Haiku call per 20 posts, strict JSON, validated.
Each kept post gets a short reusable topic label; recent labels go back into the prompt so they stay consistent."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import config, llm
from .models import _cta_ending

BATCH = 20
PROMPT = Path(__file__).resolve().parent / "prompts" / "relevance.md"


# --- profile (the profile module is optional; fall back to reading the YAML) ----------------

def load_profile() -> dict:
    try:
        from . import profile as profile_mod
        return profile_mod.load() or {}
    except (ImportError, AttributeError, FileNotFoundError):
        pass
    try:
        return yaml.safe_load(config.profile_path().read_text()) or {}
    except FileNotFoundError:
        return {}


def compact_text(p: dict) -> str:
    try:
        from . import profile as profile_mod
        return profile_mod.compact_text(p)
    except (ImportError, AttributeError):
        pass
    out = f"Role: {p.get('role') or ''}"
    if p.get("stack"):
        out += "\nStack: " + ", ".join(map(str, p["stack"]))
    topics = [x.get("topic") if isinstance(x, dict) else x for x in p.get("interests") or []]
    if topics:
        out += "\nInterests: " + ", ".join(map(str, filter(None, topics)))
    if p.get("not_interested"):
        out += "\nNot interested: " + ", ".join(map(str, p["not_interested"]))
    return out[:2500]


def recent_topics(conn, days: int = 7, limit: int = 40) -> list[str]:
    """Topic labels used in the last `days`, most used first."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT topic, COUNT(*) n FROM posts WHERE topic IS NOT NULL AND scored_at >= ? "
                        "GROUP BY topic ORDER BY n DESC, topic LIMIT ?", (since, limit)).fetchall()
    return [r["topic"] for r in rows]


# --- validation ----------------------------------------------------------------

def _clamp(v, hi: int) -> int | None:
    if isinstance(v, bool):
        return None
    try:
        return max(0, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return None


def _line(v, limit: int) -> str | None:
    s = " ".join(str(v).split()) if isinstance(v, (str, int, float)) else ""
    return (s[: limit - 1] + "…" if len(s) > limit else s) or None


SMALL = {"a", "an", "and", "for", "in", "of", "on", "or", "the", "to", "vs"}


def topic_label(v, known: list[str]) -> str | None:
    """1–3 words, at most 32 characters; reuses a known label's spelling when it matches."""
    if not isinstance(v, str):
        return None
    words = re.sub(r"[^\w\s+#./-]", "", v).split()[:3]
    # Title Case, leaving acronyms and mixed case ("MCP", "vLLM") as written.
    words = [w if (i and w in SMALL) or not w[0].islower() or not w.islower() else w[0].upper() + w[1:]
             for i, w in enumerate(words)]
    label = " ".join(words)[:32].strip()
    if not label:
        return None
    return {k.lower(): k for k in known}.get(label.lower(), label)


def validate(item, topics: list[str]) -> dict | None:
    """One post's verdict, clamped. None when there's no usable relevance."""
    if not isinstance(item, dict):
        return None
    rel = _clamp(item.get("relevance"), 3)
    if rel is None:
        return None
    if item.get("is_ad") is True:  # an ad the capture missed: kept for the count, never shown
        return {"relevance": 0, "why": "ad", "is_ad": True}
    return {
        "relevance": rel,
        "topic": topic_label(item.get("topic"), topics) if rel else None,
        "why": _line(item.get("why"), 160),
        "takeaway": _line(item.get("takeaway"), 140),
        "substance": _clamp(item.get("substance"), 5),
        "hype": _clamp(item.get("hype"), 5),
    }


# --- mutes ---------------------------------------------------------------------

def muted_by(text: str, patterns: list[str]) -> str | None:
    hay = text.lower()
    for pat in patterns:
        words = str(pat).strip().strip("\"'“”").lower()
        if words and re.search(r"(?<!\w)" + re.escape(words) + r"(?!\w)", hay):
            return str(pat)
    return None


# --- scoring -------------------------------------------------------------------

def _post_payload(conn, row) -> dict:
    links = conn.execute("SELECT kind, title, resolved_url FROM links WHERE post_key = ?", (row["key"],)).fetchall()
    return {
        "key": row["key"], "author_headline": row["author_headline"], "text": row["text"],
        "links": [{"kind": l["kind"], "title": l["title"], "url": l["resolved_url"]} for l in links],
    }


def _ask(system: str, posts: list[dict], model: str, topics: list[str]) -> dict[str, dict]:
    user = "<posts>\n" + json.dumps(posts, ensure_ascii=False, indent=1) + "\n</posts>"
    try:
        reply = llm.complete_json("relevance", model, system, user, max_tokens=300 * len(posts) + 200)
    except ValueError:
        return {}
    if isinstance(reply, list):  # tolerate [{"key": …, …}]
        reply = {r.get("key"): r for r in reply if isinstance(r, dict)}
    if not isinstance(reply, dict):
        return {}
    wanted = {p["key"] for p in posts}
    out = {}
    for k, v in reply.items():
        if k in wanted and (verdict := validate(v, topics)):
            out[k] = verdict
    return out


def _save(conn, key: str, v: dict) -> None:
    conn.execute(
        "UPDATE posts SET relevance=?, topic=?, why=?, takeaway=?, substance=?, hype=?, scored_at=?,"
        " is_promoted = is_promoted OR ? WHERE key=?",
        (v["relevance"], v.get("topic"), v.get("why"), v.get("takeaway"), v.get("substance"), v.get("hype"),
         datetime.now(timezone.utc).isoformat(), int(bool(v.get("is_ad"))), key),
    )


def score(conn, keys: list[str] | None = None, cfg: dict | None = None, profile: dict | None = None) -> dict[str, int]:
    """Score unscored, non-promoted posts (only `keys` if given). Returns {key: relevance}.
    Posts left unscored because the API failed are picked up by the next ingest."""
    cfg = cfg or config.load()
    profile = load_profile() if profile is None else profile
    rows = conn.execute("SELECT * FROM posts WHERE is_promoted = 0 AND scored_at IS NULL ORDER BY first_seen_at, key").fetchall()
    if keys is not None:
        wanted = set(keys)
        rows = [r for r in rows if r["key"] in wanted]
    patterns = list(cfg.get("mutes") or []) + list(profile.get("mutes") or []) + list(profile.get("not_interested") or [])
    topics = recent_topics(conn)
    result: dict[str, int] = {}

    todo = []
    for r in rows:
        payload = _post_payload(conn, r)
        # Lead-gen cards often carry the call to action only in the resolved page's description.
        descs = [d["description"] or "" for d in conn.execute("SELECT description FROM links WHERE post_key = ?", (r["key"],))]
        if any(_cta_ending(d) for d in descs):
            with conn:
                conn.execute("UPDATE posts SET is_promoted = 1 WHERE key = ?", (r["key"],))
                _save(conn, r["key"], {"relevance": 0, "why": "ad"})
            result[r["key"]] = 0
            continue
        hit = muted_by(" ".join([r["text"] or "", r["author_headline"] or ""] + [l["title"] or "" for l in payload["links"]]), patterns)
        if hit:
            # Muted topics never reach the model.
            with conn:
                _save(conn, r["key"], {"relevance": 0, "why": f"muted: {hit}"[:160]})
            result[r["key"]] = 0
        else:
            todo.append(payload)
    if not todo:
        return result

    system = PROMPT.read_text().replace("{{profile}}", compact_text(profile)).replace(
        "{{topics}}", ", ".join(topics) or "(none yet)")
    model = cfg["models"]["classify"]
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        got = _ask(system, batch, model, topics)
        missing = [p for p in batch if p["key"] not in got]
        if missing:
            got.update(_ask(system, missing, model, topics))
        topics += [v["topic"] for v in got.values() if v.get("topic") and v["topic"] not in topics]
        with conn:
            for p in batch:
                v = got.get(p["key"]) or {"relevance": 0, "why": "not scored"}
                _save(conn, p["key"], v)
                result[p["key"]] = v["relevance"]
    return result
