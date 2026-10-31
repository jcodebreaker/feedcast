"""`feedcast ingest`: store a capture, dedupe by post key, resolve links, score what's new."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, links, llm, media, models, relevance


def _rel(path: Path) -> str:
    """Runs are recorded relative to home so the DB survives moving the folder."""
    p = path.resolve()
    try:
        return str(p.relative_to(config.home().resolve()))
    except ValueError:
        return str(p)


def pending(conn) -> list[Path]:
    done = {r[0] for r in conn.execute("SELECT file FROM runs")}
    return [p for p in sorted(config.runs_dir().glob("**/*.json")) if _rel(p) not in done]


def _clock(started: str | None, path: Path) -> str:
    try:
        return datetime.fromisoformat(started).strftime("%H:%M")
    except (TypeError, ValueError):
        s = path.stem
        return f"{s[:2]}:{s[2:]}" if len(s) == 4 and s.isdigit() else s


def _key(conn, p: dict) -> str:
    """The post's key; a row stored under the pre-fold key keeps that key, so old posts still dedupe."""
    key = db.post_key(p["urn"], p["author"], p["text"])
    if not p["urn"]:
        old = db.legacy_key(p["author"], p["text"])
        if old != key and conn.execute("SELECT 1 FROM posts WHERE key = ?", (old,)).fetchone():
            return old
    return key


def ingest_file(conn, path: Path, cfg: dict | None = None) -> dict | None:
    """Ingest one capture. Returns a summary, or None if this file was already ingested."""
    rel = _rel(path)
    if conn.execute("SELECT 1 FROM runs WHERE file = ?", (rel,)).fetchone():
        return None
    capture, _errors = models.validate_capture(json.loads(path.read_text()))
    started = capture["run_started_at"]
    now = datetime.now(timezone.utc).isoformat()
    new: dict[str, dict] = {}
    with conn:
        run_id = conn.execute(
            "INSERT INTO runs (file, started_at, ended_at, posts_seen, posts_new, status, notes, ingested_at)"
            " VALUES (?,?,?,?,0,?,?,?)",
            (rel, started, capture["run_ended_at"], len(capture["posts"]), capture["status"], capture["notes"], now),
        ).lastrowid
        for p in capture["posts"]:
            key = _key(conn, p)
            if key not in new and not conn.execute("SELECT 1 FROM posts WHERE key = ?", (key,)).fetchone():
                conn.execute(
                    "INSERT INTO posts (key, author, author_headline, author_url, author_avatar, url, text, image,"
                    " first_seen_run, first_seen_at, posted_age_at_first_seen, is_suggested, is_promoted, link_in_hidden_comment)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, p["author"], p["author_headline"], p["author_url"], p.get("author_avatar"), p["url"], p["text"],
                     p.get("image"), run_id, started or now, p["posted_age"], int(p["is_suggested"]), int(p["is_promoted"]),
                     int(p["link_in_hidden_comment"])),
                )
                new[key] = p
            elif p.get("image") or p.get("author_avatar"):  # a later read may show what the first one missed
                conn.execute("UPDATE posts SET image = COALESCE(image, ?), author_avatar = COALESCE(author_avatar, ?)"
                             " WHERE key = ?", (p.get("image"), p.get("author_avatar"), key))
            # A post shown twice in one scroll is still one sighting.
            conn.execute(
                "INSERT OR IGNORE INTO sightings (post_key, run_id, reactions, comments, reposts, position)"
                " VALUES (?,?,?,?,?,?)",
                (key, run_id, p["reactions"], p["comments"], p["reposts"], p["position"]),
            )
        conn.execute("UPDATE runs SET posts_new = ? WHERE id = ?", (len(new), run_id))

    # Promoted posts are kept for the record but never linked or scored.
    organic = {k: p for k, p in new.items() if not p["is_promoted"]}
    resolved = links.process(conn, {k: p["links"] for k, p in organic.items()})
    cfg = cfg or config.load()
    scored, error = {}, None
    try:
        # Everything still unscored, so posts an API outage left behind catch up here.
        scored = relevance.score(conn, None, cfg)
    except llm.LLMError as e:
        error = str(e)
    thr = cfg["relevance_threshold"]
    media.fetch_for(conn, [k for k, v in scored.items() if v >= thr])
    return {
        "clock": _clock(started, path), "status": capture["status"], "seen": len(capture["posts"]), "new": len(new),
        "kept": sum(1 for v in scored.values() if v >= thr), "resolved": resolved, "error": error,
    }


def line(s: dict) -> str:
    out = f"{s['clock']} read: {s['seen']} posts, {s['new']} new, {s['kept']} kept, {s['resolved']} links resolved"
    return out if s["status"] == "ok" else f"{out} ({s['status'].replace('_', ' ')})"


def run(args) -> int:
    conn = db.connect()
    files = [Path(args.run_file)] if args.run_file else pending(conn)
    if not files:
        print("Nothing new to read.")
        return 0
    code = 0
    for f in files:
        try:
            s = ingest_file(conn, f)
        except (OSError, ValueError) as e:
            print(f"{f}: can't read it ({e})", file=sys.stderr)
            code = 1
            continue
        if s is None:
            print(f"{_rel(f)}: already read")
            continue
        print(line(s))
        if s["error"]:
            print(f"Scoring waits for the next run: {s['error']}", file=sys.stderr)
            code = 1
    return code


def register(sub) -> None:
    p = sub.add_parser("ingest", help="store captured runs, resolve their links and score new posts")
    p.add_argument("run_file", nargs="?", metavar="RUN_FILE", help="one capture file (default: every run not read yet)")
    p.set_defaults(func=run)
