"""SQLite schema and connection (CONTRACT §5). Local only, never synced."""
from __future__ import annotations

import hashlib
import re
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  file TEXT UNIQUE,              -- runs/<date>/<HHMM>.json, relative to home
  started_at TEXT, ended_at TEXT,
  posts_seen INT, posts_new INT,
  status TEXT, notes TEXT,
  ingested_at TEXT
);
CREATE TABLE IF NOT EXISTS posts (
  key TEXT PRIMARY KEY,
  author TEXT, author_headline TEXT, author_url TEXT, author_avatar TEXT, url TEXT, text TEXT,
  image TEXT,                    -- media.licdn.com URL from the capture; downloaded copies live in media/
  first_seen_run INT REFERENCES runs(id),
  first_seen_at TEXT,
  posted_age_at_first_seen TEXT,
  is_suggested INT, is_promoted INT,
  link_in_hidden_comment INT DEFAULT 0,
  relevance INT, project TEXT,   -- project: v1 only, no longer written
  topic TEXT, why TEXT, takeaway TEXT,
  substance INT, hype INT,
  scored_at TEXT
);
CREATE TABLE IF NOT EXISTS sightings (
  post_key TEXT REFERENCES posts(key), run_id INT REFERENCES runs(id),
  reactions INT, comments INT, reposts INT, position INT,
  PRIMARY KEY (post_key, run_id)
);
CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY,
  post_key TEXT REFERENCES posts(key),
  url TEXT, resolved_url TEXT, source TEXT,
  kind TEXT,                     -- github|paper|model|article|video|linkedin|other
  title TEXT, description TEXT,
  meta_json TEXT,                -- stars, language, last_push, authors, downloads, read_min
  fetched_at TEXT,
  UNIQUE (post_key, resolved_url)
);
CREATE TABLE IF NOT EXISTS feedback (
  post_key TEXT, verdict TEXT CHECK (verdict IN ('not_relevant','more_like_this')), at TEXT
);
CREATE TABLE IF NOT EXISTS reports (
  date TEXT PRIMARY KEY, json TEXT, html_path TEXT, opened_at TEXT, passes INT
);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY, at TEXT, purpose TEXT, model TEXT,
  input_tokens INT, output_tokens INT, usd REAL
);
CREATE INDEX IF NOT EXISTS links_resolved ON links(resolved_url);
"""


def connect(path=None) -> sqlite3.Connection:
    p = path or config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    # Databases from before these columns: add each once.
    have = {r["name"] for r in conn.execute("PRAGMA table_info(posts)")}
    for col in ("topic", "author_url", "image", "author_avatar"):
        if col not in have:
            conn.execute(f"ALTER TABLE posts ADD COLUMN {col} TEXT")
    conn.commit()
    return conn


FOLD = re.compile(r"(?:…|\.\.\.)\s*(?:see\s+)?more\b|\bsee\s+more\b")
KEY_CHARS = 120


def key_text(text: str) -> str:
    """The part of a post's text that survives the "…more" fold: live reads cut it at different places."""
    t = FOLD.sub(" ", " ".join((text or "").lower().split()))
    t = " ".join(t.split()).rstrip(" .…")
    return t[:KEY_CHARS].rstrip(" .…")


def post_key(urn: str | None, author: str, text: str) -> str:
    if urn:
        return urn
    return "h:" + hashlib.sha1(f"{author}\n{key_text(text)}".encode()).hexdigest()


def legacy_key(author: str, text: str) -> str:
    """The key rows stored before key_text existed; ingest still matches them."""
    return "h:" + hashlib.sha1(f"{author}\n{text[:200]}".encode()).hexdigest()
