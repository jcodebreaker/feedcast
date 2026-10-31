"""Images for the evening page: post images, avatars and link previews, downloaded once into media/.

Fetched through links' injectable fetch, so no cookie ever rides along and tests stay offline.
Only https, at most two redirects, image types a browser renders without running anything
(no SVG), at most 3 MB. Files are named by the sha1 of the source URL.
"""
from __future__ import annotations

import hashlib
import json
import urllib.parse

from . import config, links

MAX_BYTES = 3 * 1024 * 1024
TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/avif": "avif"}
EXTS = set(TYPES.values())


def media_dir():
    return config.path("media")


def _stem(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()


def local(url: str | None) -> str | None:
    """The downloaded file's name for this URL, if there is one."""
    if not isinstance(url, str) or not url:
        return None
    stem = _stem(url)
    return next((f"{stem}.{e}" for e in sorted(EXTS) if (media_dir() / f"{stem}.{e}").is_file()), None)


def download(url: str | None) -> str | None:
    """Fetch one image into media/<sha1>.<ext>. Returns the file name, or None when it isn't a usable image."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    if have := local(url):
        return have
    cur = url
    for hop in range(links.MAX_HOPS + 1):
        r = links._fetch(cur, "GET", {"Accept": "image/*"})
        if r is None:
            return None
        loc = r.headers.get("location")
        if 300 <= r.status < 400 and loc:
            cur = urllib.parse.urljoin(cur, loc)
            if hop == links.MAX_HOPS or not cur.startswith("https://"):
                return None
            continue
        break
    ext = TYPES.get((r.headers.get("content-type") or "").split(";")[0].strip().lower())
    try:
        declared = int(r.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if r.status != 200 or not ext or not r.body or len(r.body) > MAX_BYTES or declared > MAX_BYTES:
        return None
    d = media_dir()
    d.mkdir(parents=True, exist_ok=True)
    name = f"{_stem(url)}.{ext}"
    tmp = d / f".{name}.part"
    tmp.write_bytes(r.body)
    tmp.replace(d / name)
    return name


def fetch_for(conn, keys) -> int:
    """Download the post image, the author's photo and the first link image for these posts. Returns files fetched."""
    n = 0
    for key in keys:
        p = conn.execute("SELECT image, author_avatar FROM posts WHERE key = ?", (key,)).fetchone()
        if not p:
            continue
        for u in (p["image"], p["author_avatar"], link_image(conn, key)):
            if u and not local(u) and download(u):
                n += 1
    return n


def link_image(conn, key: str) -> str | None:
    for (meta,) in conn.execute("SELECT meta_json FROM links WHERE post_key = ? ORDER BY id", (key,)):
        try:
            img = json.loads(meta or "{}").get("image")
        except (ValueError, AttributeError):
            continue
        if isinstance(img, str) and img.startswith("https://"):
            return img
    return None
