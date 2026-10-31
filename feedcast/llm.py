"""The one place that talks to the Claude API (CONTRACT §6). Stdlib only; fakes for tests and dev runs."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.anthropic.com/v1/messages"
# USD per million tokens (input, output), prompts under 100K tokens.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-opus-5-5": (4.0, 20.0)}

_fake = None


def set_fake(fn) -> None:
    """fn(purpose, model, system, user) -> str. Pass None to clear."""
    global _fake
    _fake = fn


class LLMError(RuntimeError):
    pass


def api_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        return subprocess.run(
            ["security", "find-generic-password", "-s", "feedcast", "-a", "anthropic", "-w"],
            capture_output=True, text=True, check=True,
        ).stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _complete_cli(purpose: str, model: str, system: str, user: str) -> str:
    # No API key: run the same prompt through Claude Code on the user's own plan, with no tools.
    cmd = ["claude", "-p", "--model", model, "--system-prompt", system, "--tools", "",
           "--strict-mcp-config", "--no-session-persistence", "--output-format", "json"]
    try:
        r = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        raise LLMError("No API key and no `claude` CLI. Set ANTHROPIC_API_KEY or run `feedcast init`.") from None
    except subprocess.TimeoutExpired:
        raise LLMError("claude -p timed out") from None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise LLMError(f"claude -p failed: {(r.stderr or r.stdout).strip()[-300:]}") from None
    if data.get("is_error"):
        raise LLMError(f"claude -p failed: {str(data.get('result'))[:300]}")
    _log(purpose, model, data.get("usage") or {})
    return data.get("result") or ""


def _log(purpose, model, usage, conn=None) -> None:
    from . import db

    inp, out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    pin, pout = PRICES.get(model, (0, 0))
    c = conn or db.connect()
    with c:
        c.execute(
            "INSERT INTO llm_calls (at, purpose, model, input_tokens, output_tokens, usd) VALUES (?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), purpose, model, inp, out, (inp * pin + out * pout) / 1e6),
        )


def complete(purpose: str, model: str, system: str, user: str, max_tokens: int = 4000, effort: str | None = None) -> str:
    if _fake:
        return _fake(purpose, model, system, user)
    fake_dir = os.environ.get("FEEDCAST_FAKE_LLM")
    if fake_dir:
        return (Path(fake_dir) / f"{purpose}.json").read_text()

    key = api_key()
    if not key:
        return _complete_cli(purpose, model, system, user)
    body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]}
    if effort:
        body["output_config"] = {"effort": effort}
    req = urllib.request.Request(
        API, data=json.dumps(body).encode(), method="POST",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise LLMError(f"API {e.code}: {e.read().decode(errors='replace')[:300]}") from None
    _log(purpose, model, data.get("usage", {}))
    if data.get("stop_reason") == "refusal":
        raise LLMError("The model declined this request.")
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def _first_json(text: str):
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "[{":
            try:
                return dec.raw_decode(text[i:])[0]
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON in reply")


def complete_json(purpose: str, model: str, system: str, user: str, max_tokens: int = 4000, effort: str | None = None):
    text = complete(purpose, model, system, user, max_tokens, effort)
    try:
        return _first_json(text)
    except ValueError:
        text = complete(purpose, model, system, user + "\n\nReply with JSON only.", max_tokens, effort)
        return _first_json(text)
