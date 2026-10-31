"""End-to-end run on invented sample data: import a read, ingest, build the evening page.

No network, no Claude API, no LinkedIn: links and the model are faked. Writes into a temp
FEEDCAST_HOME and prints the report path.

    uv run python scripts/demo.py [--keep DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REPOS = {
    "oyelaran-labs/agent-replay": ("Replays recorded agent sessions against a new build and fails CI on regression.", 2310, "Python"),
    "khoury-ai/agent-failure-cases": ("1,100 eval cases in 9 failure modes, JSONL, MIT.", 640, "Python"),
    "lindqvist/slidefit": ("Fits text and images to the box in python-pptx before rendering.", 212, "Python"),
}
SHORT = {
    "https://lnkd.in/eX7qPz": "https://github.com/oyelaran-labs/agent-replay",
    "https://lnkd.in/b7Rk2a": "https://github.com/lindqvist/slidefit",
}
# Invented scores for the sample posts, keyed by the last digits of their URN.
SCORES = {
    "01": (3, "Agent Evals", "Replays recorded sessions against a new build and fails CI on regression.", "Teams fail deploys on eval regressions, using this harness."),
    "03": (3, "MCP", "Real numbers on letting clients search tools instead of merging them.", "One search step fixed wrong-tool picks across 212 tools."),
    "05": (2, "Slide Layout", "Fits text before render instead of catching overflow after.", "slidefit 0.3 fits pptx text to the box up front."),
    "07": (3, "Agent Evals", "Same harness again, from another engineer.", "agent-replay recommended for eval regressions in CI."),
    "08": (3, "Agent Evals", "Measures where model judges and humans disagree on tool calls.", "LLM judges disagree with humans 31% of the time on tool use."),
    "09": (2, "Agent Evals", "A ready-made eval set, sorted by how agents fail.", "1,100 agent eval cases, sorted by failure mode."),
    "10": (2, "MCP", "Both auth models laid out with trade-offs.", "MCP auth: per-user tokens or one server credential."),
    "11": (2, "Prompting", "Retrieved examples instead of an ever-growing rule list.", "Three retrieved examples beat a 2,400-token rule list."),
}


def fake_fetch(url, method="GET", headers=None):
    from feedcast.links import Response

    if url in SHORT:
        return Response(301, url, {"location": SHORT[url]})
    m = re.match(r"https://api\.github\.com/repos/([^/]+/[^/?]+)", url)
    if m and m.group(1) in REPOS:
        d, stars, lang = REPOS[m.group(1)]
        body = {"full_name": m.group(1), "description": d, "stargazers_count": stars, "language": lang, "pushed_at": "2026-10-28T10:00:00Z"}
        return Response(200, url, {"content-type": "application/json"}, json.dumps(body).encode())
    if "weiming.engineering" in url:
        html = b'<title>212 tools, 9 MCP servers, one search step</title><meta property="og:description" content="Wrong-tool rate fell from 7% to under 1%.">'
        return Response(200, url, {"content-type": "text/html"}, html)
    return Response(404, url)


def fake_llm(purpose, model, system, user):
    if purpose == "relevance":
        out = {}
        for key in set(re.findall(r"urn:li:activity:\d+", user)):
            rel, topic, why, take = SCORES.get(key[-2:], (1, None, "Off your interests.", "Off-topic for you today."))
            out[key] = {"relevance": rel, "topic": topic, "why": why, "takeaway": take, "substance": 3, "hype": 1}
        return json.dumps(out)
    if purpose == "digest":
        keys = list(dict.fromkeys(re.findall(r"urn:li:activity:\d+", user)))
        return json.dumps({
            "summary": "Agent evals in CI kept coming up, and **two people shared the same harness repo**. One tool-search write-up brought **real numbers on MCP**. Quiet on slide layout.",
            # Broad groups; slide layout is left out on purpose, so it lands in "Also".
            "groups": [{"name": "Agents", "line": "one harness, shared twice",
                        "keys": [k for k in keys if SCORES.get(k[-2:], (0, ""))[1] in ("Agent Evals", "MCP")]},
                       {"name": "Prompting", "line": "retrieved examples over rules",
                        "keys": [k for k in keys if SCORES.get(k[-2:], (0, ""))[1] == "Prompting"]}],
            "items": [{"key": k} for k in keys],
            "open_three": keys[:3],
        })
    raise ValueError(purpose)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", help="use this dir as FEEDCAST_HOME and keep it")
    args = ap.parse_args()
    home = Path(args.keep or tempfile.mkdtemp(prefix="feedcast-demo-"))
    os.environ["FEEDCAST_HOME"] = str(home)
    home.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "fixtures/demo/profile.yaml", home / "profile.yaml")

    from feedcast import cli, links, llm

    links.set_fetch(fake_fetch)
    llm.set_fake(fake_llm)
    steps = [
        ["capture", "--from-file", str(ROOT / "fixtures/capture-sample.json")],
        ["ingest"],
        ["report", "--date", "2026-10-30", "--no-open", "--no-notify"],
    ]
    for argv in steps:
        print(f"$ feedcast {' '.join(argv)}")
        rc = cli.main(argv)
        if rc:
            return rc
    print(f"\nReport: {home / 'reports' / '2026-10-30.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
