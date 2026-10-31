"""Where Feedcast keeps its files and what it does by default (CONTRACT §1–2)."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

DEFAULTS = {
    # Empty on purpose: the user names their work folders, we never guess.
    "roots": [],
    "use_sessions": True,
    "schedule": {"profile": "02:00", "captures": ["09:00", "13:30", "18:45"], "digest": "19:30"},
    "relevance_threshold": 2,
    "max_posts": 50,
    "max_items": 8,
    "pins": [],
    "mutes": [],
    "models": {"classify": "claude-haiku-5-5", "digest": "claude-opus-5-5"},
}


def home() -> Path:
    return Path(os.environ.get("FEEDCAST_HOME") or Path.home() / ".feedcast")


def path(*parts: str) -> Path:
    return home().joinpath(*parts)


def config_path() -> Path:
    return path("config.yaml")


def profile_path() -> Path:
    return path("profile.yaml")


def linkedin_self_path() -> Path:
    return path("linkedin-self.json")


def db_path() -> Path:
    return path("feedcast.db")


def runs_dir() -> Path:
    return path("runs")


def reports_dir() -> Path:
    return path("reports")


def paused_path() -> Path:
    return path("PAUSED")


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load() -> dict:
    try:
        data = yaml.safe_load(config_path().read_text()) or {}
    except FileNotFoundError:
        data = {}
    return _merge(DEFAULTS, data)


def save(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
