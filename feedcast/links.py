"""Shared links: resolve shorteners without cookies, classify by domain, enrich, cache 24 h (CONTRACT §8)."""
from __future__ import annotations

import html
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

MAX_HOPS = 2
MAX_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 3 * 1024 * 1024  # media.MAX_BYTES; one byte more is read so an oversized image is seen
TIMEOUT = 10
CACHE = timedelta(hours=24)
UA = "feedcast/0.1 (personal feed reader)"

SHORTENERS = {
    "lnkd.in", "bit.ly", "t.co", "buff.ly", "tinyurl.com", "ow.ly", "goo.gl", "dlvr.it", "rebrand.ly",
    "shorturl.at", "is.gd", "cutt.ly", "tiny.cc", "trib.al", "hubs.ly", "hubs.li", "aka.ms", "amzn.to",
    "goo.gle", "dub.sh",
}
TRACKING = {
    "trk", "trkcampaign", "trackingid", "lipi", "li_fat_id", "fbclid", "gclid", "dclid", "msclkid", "mc_cid",
    "mc_eid", "igshid", "ref_src", "ref_url", "_hsenc", "_hsmi", "mkt_tok", "si", "s_cid", "cmpid", "rcm",
}


@dataclass
class Response:
    status: int
    url: str
    headers: dict = field(default_factory=dict)  # lower-case names
    body: bytes = b""


class _PassThrough(urllib.request.HTTPErrorProcessor):
    # Hand every status back untouched, so redirects are ours to follow (and count).
    def http_response(self, request, response):
        return response

    https_response = http_response


def _default_fetch(url: str, method: str = "GET", headers: dict | None = None) -> Response | None:
    if not url.startswith(("http://", "https://")):
        return None
    # A fresh opener with no cookie processor: nothing from a jar, global opener or browser ever rides along.
    opener = urllib.request.build_opener(_PassThrough)
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, **(headers or {})})
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            image = (r.headers.get("content-type") or "").lower().startswith("image/")
            body = r.read(MAX_IMAGE_BYTES + 1 if image else MAX_BYTES) if method == "GET" else b""
            return Response(r.status, url, {k.lower(): v for k, v in r.headers.items()}, body)
    except (urllib.error.URLError, OSError, ValueError):
        return None


_fetch = _default_fetch
_gh_token = None  # None = not looked up yet


def set_fetch(fn) -> None:
    """fn(url, method="GET", headers=None) -> Response | None. Pass None to restore urllib."""
    global _fetch
    _fetch = fn or _default_fetch


def set_github_token(token: str | None) -> None:
    """Tests and callers can pin the token; "" means anonymous."""
    global _gh_token
    _gh_token = token


def github_token() -> str:
    global _gh_token
    if _gh_token is None:
        try:
            _gh_token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            _gh_token = ""
    return _gh_token


# --- URLs -------------------------------------------------------------------

def host(url: str) -> str:
    h = (urllib.parse.urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def is_shortener(url: str) -> bool:
    return host(url) in SHORTENERS


def clean(url: str) -> str:
    """Drop tracking params and the fragment so the same resource dedupes across posts."""
    p = urllib.parse.urlsplit(url.strip())
    query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING]
    path = p.path
    if host(url) == "github.com":
        path = re.sub(r"(\.git)?/*$", "", path) or "/"
    return urllib.parse.urlunsplit((p.scheme.lower(), (p.netloc or "").lower(), path, urllib.parse.urlencode(query), ""))


def classify(url: str) -> tuple[str, str | None]:
    """(kind, subtype) by domain. kind ∈ github|paper|model|article|video|linkedin|other."""
    if not url.startswith(("http://", "https://")):
        return "other", None
    h, path = host(url), urllib.parse.urlsplit(url).path
    parts = [s for s in path.split("/") if s]
    if h == "gist.github.com":
        return "github", "gist"
    if h in ("github.com", "raw.githubusercontent.com"):
        if "releases" in parts:
            return "github", "release"
        return "github", "repo" if len(parts) >= 2 else None
    if h in ("arxiv.org", "export.arxiv.org", "openreview.net", "aclanthology.org") or path.lower().endswith(".pdf"):
        return "paper", None
    if h in ("huggingface.co", "hf.co"):
        return ("paper", None) if parts[:1] == ["papers"] else ("model", None)
    if h in ("youtube.com", "m.youtube.com", "youtu.be", "loom.com", "vimeo.com"):
        return "video", None
    if h == "linkedin.com" or h.endswith(".linkedin.com"):
        return "linkedin", None
    if is_shortener(url):
        return "other", None  # never resolved
    return "article", None


# --- resolution ---------------------------------------------------------------

def _follow(url: str, method: str) -> tuple[str, Response | None]:
    """Follow at most MAX_HOPS redirects by hand. Returns the last URL reached and its response."""
    cur, resp = url, None
    for hop in range(MAX_HOPS + 1):
        resp = _fetch(cur, method)
        if resp is None:
            return cur, None
        loc = resp.headers.get("location")
        if not (300 <= resp.status < 400 and loc):
            return cur, resp
        nxt = urllib.parse.urljoin(cur, loc)
        if hop == MAX_HOPS or not nxt.startswith(("http://", "https://")):
            break
        cur = nxt
    # Hop limit: keep the furthest URL we know, without fetching it.
    return urllib.parse.urljoin(cur, resp.headers["location"]), None


_EXTERNAL = re.compile(r'<a\b[^>]*data-tracking-control-name="external_url_click"[^>]*>', re.I)
_HREF = re.compile(r'href="(https?://[^"]+)"', re.I)


def _interstitial_target(body: bytes) -> str | None:
    """lnkd.in sometimes answers 200 with a 'you're leaving LinkedIn' page; the target is in an anchor."""
    page = body.decode("utf-8", errors="replace")
    m = _EXTERNAL.search(page)
    candidates = _HREF.findall(m.group(0)) if m else _HREF.findall(page)
    for href in candidates:
        href = html.unescape(href)
        if not re.search(r"(^|\.)(linkedin\.com|licdn\.com|lnkd\.in)$", host(href)):
            return href
    return None


def resolve(url: str) -> tuple[str, bool]:
    """(resolved url, reached). HEAD first, GET if HEAD fails or lands on a page; never more than two hops."""
    final, resp = _follow(url, "HEAD")
    if (resp is None and final == url) or (resp is not None and (resp.status >= 400 or is_shortener(final))):
        final, resp = _follow(url, "GET")
    if resp is not None and resp.status < 400 and is_shortener(final):
        target = _interstitial_target(resp.body)
        return (target, True) if target else (url, False)
    return final, final != url or (resp is not None and resp.status < 400)


# --- enrichment ---------------------------------------------------------------

class _Head(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (a.get("property") or a.get("name") or "").lower()
            if name and a.get("content") and name not in self.meta:
                self.meta[name] = a["content"].strip()

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and len(self.title) < 300:
            self.title += data


def _json(url: str, headers: dict | None = None):
    r = _fetch(url, "GET", headers)
    if r is None or r.status >= 400:
        return None
    try:
        return json.loads(r.body)
    except ValueError:
        return None


def _github(url: str, sub: str | None) -> dict | None:
    parts = [s for s in urllib.parse.urlsplit(url).path.split("/") if s]
    tok = github_token()
    headers = {"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {tok}"} if tok else {})}
    if sub == "gist" and parts:
        g = _json(f"https://api.github.com/gists/{parts[-1]}", headers)
        if not g:
            return None
        files = list((g.get("files") or {}).values())
        return {"title": (files[0].get("filename") if files else None) or g.get("description"),
                "description": g.get("description") or None,
                "meta": {"type": "gist", "language": files[0].get("language") if files else None,
                         "last_push": g.get("updated_at")}}
    if len(parts) < 2:
        return None
    repo = _json(f"https://api.github.com/repos/{parts[0]}/{parts[1]}", headers)
    if not repo:
        return None
    meta = {"type": sub or "repo", "stars": repo.get("stargazers_count"), "language": repo.get("language"),
            "last_push": repo.get("pushed_at")}
    title = repo.get("full_name") or f"{parts[0]}/{parts[1]}"
    if sub == "release" and "tag" in parts and parts.index("tag") + 1 < len(parts):
        meta["version"] = parts[parts.index("tag") + 1]
        title = f"{title} {meta['version']}"
    return {"title": title, "description": repo.get("description") or None, "meta": meta}


_ARXIV_ID = re.compile(r"/(?:abs|pdf|html)/([\w.\-/]+?)(?:v\d+)?(?:\.pdf)?/?$")
_ATOM = "{http://www.w3.org/2005/Atom}"


def first_sentence(text: str) -> str:
    text = " ".join(text.split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text)[:300]


def _arxiv(url: str) -> dict | None:
    m = _ARXIV_ID.search(urllib.parse.urlsplit(url).path)
    if not m:
        return None
    r = _fetch(f"https://export.arxiv.org/api/query?id_list={urllib.parse.quote(m.group(1))}", "GET")
    if r is None or r.status >= 400:
        return None
    try:
        entry = ET.fromstring(r.body).find(f"{_ATOM}entry")
    except ET.ParseError:
        return None
    if entry is None or entry.find(f"{_ATOM}title") is None:
        return None
    authors = [a.findtext(f"{_ATOM}name", "").strip() for a in entry.findall(f"{_ATOM}author")]
    return {"title": " ".join(entry.findtext(f"{_ATOM}title", "").split()),
            "description": first_sentence(entry.findtext(f"{_ATOM}summary", "")) or None,
            "meta": {"authors": [a for a in authors if a][:10], "arxiv_id": m.group(1)}}


def _huggingface(url: str) -> dict | None:
    parts = [s for s in urllib.parse.urlsplit(url).path.split("/") if s]
    kind = "models"
    if parts[:1] and parts[0] in ("datasets", "spaces"):
        kind, parts = parts[0], parts[1:]
    if len(parts) < 2:
        return None
    d = _json(f"https://huggingface.co/api/{kind}/{parts[0]}/{parts[1]}")
    if not d:
        return None
    return {"title": d.get("id") or f"{parts[0]}/{parts[1]}", "description": None,
            "meta": {"type": kind[:-1], "downloads": d.get("downloads"), "pipeline_tag": d.get("pipeline_tag")}}


_READ = re.compile(r"(\d+)\s*min(?:ute)?s?\b", re.I)


def _page(url: str) -> dict | None:
    final, r = _follow(url, "GET")
    if r is None or r.status >= 400 or "html" not in r.headers.get("content-type", "text/html"):
        return None
    p = _Head()
    try:
        p.feed(r.body.decode("utf-8", errors="replace"))
    except Exception:  # truncated or broken HTML still gives what it gave
        pass
    title = p.meta.get("og:title") or " ".join(p.title.split()) or None
    desc = p.meta.get("og:description") or p.meta.get("description")
    meta = {}
    for k in ("article:reading_time", "reading_time", "og:reading_time", "twitter:data1", "twitter:data2"):
        v = p.meta.get(k, "")
        m = (re.fullmatch(r"\s*(\d+)\s*", v) if "reading_time" in k else None) or _READ.search(v)
        if m:
            meta["read_min"] = int(m.group(1))
            break
    img = p.meta.get("og:image") or p.meta.get("og:image:secure_url") or p.meta.get("twitter:image") or p.meta.get("twitter:image:src")
    if img and img.startswith("https://") and len(img) < 2000:
        meta["image"] = img
    return {"title": title and title[:300], "description": desc and desc[:500], "meta": meta}


def preview_image(url: str, kind: str, sub: str | None) -> str | None:
    """Social cards these hosts publish at a known address, so no page has to be read for them."""
    parts = [s for s in urllib.parse.urlsplit(url).path.split("/") if s]
    if kind == "github" and sub in ("repo", "release") and host(url) == "github.com" and len(parts) >= 2:
        return f"https://opengraph.githubassets.com/1/{parts[0]}/{parts[1]}"
    if kind == "model" and host(url) in ("huggingface.co", "hf.co"):
        group = "models"
        if parts[:1] and parts[0] in ("datasets", "spaces"):
            group, parts = parts[0], parts[1:]
        if len(parts) >= 2:
            return f"https://cdn-thumbnails.huggingface.co/social-thumbnails/{group}/{parts[0]}/{parts[1]}.png"
    return None


def enrich(url: str, kind: str, sub: str | None) -> dict | None:
    """{title, description, meta} or None when the resource can't be reached."""
    h = host(url)
    if kind == "github":
        return _github(url, sub)
    if h in ("arxiv.org", "export.arxiv.org"):
        return _arxiv(url)
    if kind == "model":
        return _huggingface(url)
    if kind == "linkedin" or url.lower().endswith(".pdf"):
        return None  # LinkedIn pages need a login we never send; PDFs have no head to read
    if kind in ("article", "video", "paper"):
        return _page(url)
    return None


# --- store --------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fresh(conn, column: str, value: str):
    cutoff = (_now() - CACHE).isoformat()
    return conn.execute(
        f"SELECT * FROM links WHERE {column} = ? AND fetched_at >= ? ORDER BY fetched_at DESC LIMIT 1", (value, cutoff)
    ).fetchone()


def process(conn, post_links: dict[str, list[dict]]) -> int:
    """Store one row per (post, resolved url). Returns how many shortened links resolved."""
    resolved_count = 0
    for key, items in post_links.items():
        for item in items:
            url = item["url"]
            if is_shortener(url):
                cached = _fresh(conn, "url", url)
                if cached:
                    resolved, ok = cached["resolved_url"], cached["resolved_url"] != clean(url)
                else:
                    resolved, ok = resolve(url)
                resolved_count += ok and not is_shortener(resolved)
            else:
                resolved = url
            resolved = clean(resolved)
            kind, sub = classify(resolved)
            row = _fresh(conn, "resolved_url", resolved)
            if row:
                title, desc, meta_json, fetched = row["title"], row["description"], row["meta_json"], row["fetched_at"]
            else:
                info = enrich(resolved, kind, sub) or {}
                meta = {"domain": host(resolved), **({"type": sub} if sub else {}),
                        "image": preview_image(resolved, kind, sub), **(info.get("meta") or {})}
                title, desc = info.get("title"), info.get("description")
                meta_json, fetched = json.dumps({k: v for k, v in meta.items() if v is not None}), _now().isoformat()
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO links (post_key, url, resolved_url, source, kind, title, description,"
                    " meta_json, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (key, url, resolved, item.get("source"), kind, title or item.get("title"), desc, meta_json, fetched),
                )
    return resolved_count


def shared_by(conn, resolved_url: str) -> int:
    return conn.execute("SELECT COUNT(DISTINCT post_key) FROM links WHERE resolved_url = ?", (resolved_url,)).fetchone()[0]
