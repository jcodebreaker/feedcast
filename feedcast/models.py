"""Validation of the raw capture file (CONTRACT §4). Shared by capture (write) and ingest (read)."""
from __future__ import annotations

import re

STATUSES = {"ok", "degraded", "aborted_challenge", "aborted_not_signed_in", "aborted_no_chrome"}
LINK_SOURCES = {"body", "preview", "first_comment"}
URN = re.compile(r"^urn:li:(activity|share|ugcPost):\d+$")
MIN_VALID = 10
URN_IN_URL = re.compile(r"/feed/update/(urn:li:(?:activity|share|ugcPost):\d+)")
# Ad markers, kept conservative: a whole line that says it, never a word inside a sentence ("I got promoted").
AD_LINE = re.compile(r"^(promoted|sponsored)(\s+by\s+.+)?$", re.I)
AD_CTA = {"download now", "learn more", "sign up", "register now", "get started", "apply now", "install now",
          "book a demo", "request a demo", "try it free", "try for free", "get the guide", "get the report"}
AUTHOR_URL = re.compile(r"^(?:https://www\.linkedin\.com)?(/(?:in|company)/[^/?#\s]+)")


def _cta_ending(title: str) -> bool:
    """A link title whose last sentence is a call to action ("Startup guide to AI agents. Download now.")."""
    parts = re.split(r"(?<=[.!?])\s+", " ".join(title.split()))
    return len(parts) > 1 and parts[-1].rstrip(".!").lower() in AD_CTA


def looks_promoted(p: dict) -> bool:
    """An ad the capture didn't flag: a "Promoted"/"Sponsored" line anywhere on the post, a link title
    ending in a call-to-action sentence ("… Download now."), or a call-to-action card ("Download now")
    on a post with no age, which organic posts always show."""
    lines = [p.get("author_headline") or "", p.get("posted_age") or ""]
    lines += (p.get("text") or "").splitlines()
    titles = [l.get("title") or "" for l in p.get("links") or []]
    if any(AD_LINE.match(" ".join(x.split()).strip(" ·•")) for x in lines + titles) or any(map(_cta_ending, titles)):
        return True
    return not (p.get("posted_age") or "").strip() and any(" ".join(t.lower().split()) in AD_CTA for t in titles)


def _int(v) -> int:
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return max(v, 0)
    if isinstance(v, str):
        m = re.match(r"^\s*([\d.,]+)\s*([kKmM]?)", v)
        if m:
            n = float(m.group(1).replace(",", ""))
            return int(n * {"k": 1e3, "m": 1e6}.get(m.group(2).lower(), 1))
    return 0


def licdn_image(v) -> str | None:
    """Only LinkedIn's own image CDN: images are fetched later without cookies, never from arbitrary hosts."""
    if isinstance(v, str) and v.startswith("https://media.licdn.com/") and len(v) < 2000:
        return v
    return None


def author_url(v) -> str | None:
    """The author's profile or company page, query and trailing path dropped; None for anything else."""
    m = AUTHOR_URL.match(v.strip()) if isinstance(v, str) else None
    return f"https://www.linkedin.com{m.group(1)}/" if m else None


def _post(p: dict, i: int):
    if not isinstance(p, dict):
        return None, f"post {i}: not an object"
    author = str(p.get("author") or "").strip()
    text = str(p.get("text") or "").strip()
    if not author or not text:
        return None, f"post {i}: missing author or text"
    urn = p.get("urn") if isinstance(p.get("urn"), str) and URN.match(p["urn"]) else None
    links = []
    for l in p.get("links") or []:
        if isinstance(l, dict) and isinstance(l.get("url"), str) and l["url"].startswith(("http://", "https://")):
            links.append({
                "url": l["url"][:2000],
                "source": l.get("source") if l.get("source") in LINK_SOURCES else "body",
                "title": (str(l["title"])[:300] if l.get("title") else None),
            })
    url = p.get("url") if isinstance(p.get("url"), str) and p["url"].startswith("https://www.linkedin.com/") else None
    if not urn and url and (m := URN_IN_URL.search(url)):
        urn = m.group(1)
    if urn and not url:
        url = f"https://www.linkedin.com/feed/update/{urn}"
    clean = {
        "urn": urn, "url": url,
        "author": author[:200], "author_headline": str(p.get("author_headline") or "")[:300],
        "author_url": author_url(p.get("author_url")),
        "author_avatar": licdn_image(p.get("author_avatar")),
        "image": licdn_image(p.get("image")),
        "text": text[:5000], "posted_age": str(p.get("posted_age") or "")[:20],
        "reactions": _int(p.get("reactions")), "comments": _int(p.get("comments")), "reposts": _int(p.get("reposts")),
        "is_suggested": bool(p.get("is_suggested")), "is_promoted": bool(p.get("is_promoted")),
        "position": _int(p.get("position")) or i + 1,
        "links": links[:20],
        "link_in_hidden_comment": bool(p.get("link_in_hidden_comment")),
    }
    clean["is_promoted"] = clean["is_promoted"] or looks_promoted(clean)
    return clean, None


def validate_capture(obj) -> tuple[dict, list[str]]:
    errors: list[str] = []
    if not isinstance(obj, dict):
        return {"status": "degraded", "notes": "not an object", "posts": []}, ["capture is not an object"]
    status = obj.get("status") if obj.get("status") in STATUSES else "degraded"
    posts = []
    for i, p in enumerate(obj.get("posts") or []):
        clean, err = _post(p, i)
        if err:
            errors.append(err)
        else:
            posts.append(clean)
    if status == "ok" and len(posts) < MIN_VALID:
        status = "degraded"
        errors.append(f"only {len(posts)} valid posts")
    return {
        "run_started_at": obj.get("run_started_at"), "run_ended_at": obj.get("run_ended_at"),
        "status": status, "notes": str(obj.get("notes") or "")[:1000], "posts": posts,
    }, errors
