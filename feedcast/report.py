"""`feedcast report`: the evening page. Counts, links and authors come from the DB; one Opus call writes the copy."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import quote, urlencode, urlparse

import yaml

from . import config, db, feedback, llm, media

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "templates" / "report.html"
PROMPT = Path(__file__).resolve().parent / "prompts" / "digest.md"
GOOD = ("ok", "degraded")
NO_READS = {
    "aborted_challenge": "No reads today. LinkedIn asked for a check.",
    "aborted_not_signed_in": "No reads today. LinkedIn was signed out.",
    "aborted_no_chrome": "No reads today. Chrome was closed.",
}
KIND_ORDER = ["github", "paper", "model", "article", "video", "other", "linkedin"]
KIND_LABEL = {"github": "GitHub", "paper": "Paper", "model": "Model", "article": "Article",
              "video": "Video", "linkedin": "LinkedIn"}
MAX_CHIPS = 3
MAX_SKIPPED = 12
MAX_GROUPS = 4
ALSO = "Also"
SEARCH = "https://www.linkedin.com/search/results/content/"
SEARCH_WORDS = 8
STOP = set("""a an and are as at be but by for from has have i if in is it its it's just me my of on or our so that
the their this to was we were what when with you your""".split())


# ---------- dates ----------

def _day(iso: str | None) -> str | None:
    """Local calendar day of a stored timestamp (llm_calls are UTC, captures carry their offset)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:10]
    return (dt.astimezone() if dt.tzinfo else dt).date().isoformat()


def _rows_on(conn, sql_from: str, col: str, day: date, back: int = 0) -> list:
    """Rows whose `col` falls on day-back … day; a coarse SQL window, then exact local-day filtering."""
    lo, hi = (day - timedelta(days=back + 1)).isoformat(), (day + timedelta(days=1)).isoformat()
    rows = conn.execute(f"SELECT * FROM {sql_from} WHERE substr({col},1,10) BETWEEN ? AND ?", (lo, hi)).fetchall()
    want = {(day - timedelta(days=i)).isoformat() for i in range(back + 1)}
    return [r for r in rows if _day(r[col]) in want]


# ---------- gather (DB only) ----------

def _meta(raw) -> dict:
    try:
        m = json.loads(raw or "{}")
        return m if isinstance(m, dict) else {}
    except ValueError:
        return {}


def _profile_role() -> str:
    try:
        prof = yaml.safe_load(config.profile_path().read_text()) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return ""
    return str(prof.get("role") or "").strip() if isinstance(prof, dict) else ""


def _topic(p) -> str:
    return p["topic"] or "Other"


def gather(conn, day: date, cfg: dict) -> dict:
    d = day.isoformat()
    runs = _rows_on(conn, "runs", "started_at", day)
    good = [r for r in runs if r["status"] in GOOD]
    data = {"date": d, "runs": runs, "passes": len(good)}
    if not good:
        return data

    week = _rows_on(conn, "posts", "first_seen_at", day, back=7)
    posts = {p["key"]: dict(p) for p in week if _day(p["first_seen_at"]) == d}
    run_ids = [r["id"] for r in good]
    q = ",".join("?" * len(run_ids))
    sightings = conn.execute(f"SELECT post_key, reactions FROM sightings WHERE run_id IN ({q})", run_ids).fetchall()
    reactions = defaultdict(int)
    for s in sightings:
        reactions[s["post_key"]] = max(reactions[s["post_key"]], s["reactions"] or 0)

    links_by_post = defaultdict(list)
    keys = list(posts)
    for i in range(0, len(keys), 500):
        part = keys[i:i + 500]
        for l in conn.execute(f"SELECT * FROM links WHERE post_key IN ({','.join('?' * len(part))})", part):
            u = l["resolved_url"] or l["url"]
            links_by_post[l["post_key"]].append({**dict(l), "u": u, "meta": _meta(l["meta_json"])})
    shared = Counter()  # shared_by = COUNT(DISTINCT post_key) per resolved URL, across all of today's posts
    sharers = defaultdict(list)
    for k, ls in links_by_post.items():
        for u in {l["u"] for l in ls}:
            shared[u] += 1
            sharers[u].append(k)

    thr = cfg["relevance_threshold"]
    scored = [p for p in posts.values() if not p["is_promoted"] and p["relevance"] is not None]
    kept = [p for p in scored if p["relevance"] >= thr]
    skipped = sorted((p for p in scored if p["relevance"] < thr),
                     key=lambda p: (-p["relevance"], -reactions[p["key"]], p["key"]))

    def rank(p):
        return (-p["relevance"], -max((shared[l["u"]] for l in links_by_post[p["key"]]), default=0),
                -reactions[p["key"]], p["key"])

    items, claimed = [], {}
    for p in sorted(kept, key=rank):
        ls = links_by_post[p["key"]]
        hit = next((claimed[l["u"]] for l in ls if l["u"] in claimed), None)
        if hit:  # same resource, another sharer: one item, "shared by N"
            hit["merged"].append(p)
            continue
        uniq = {}
        for l in sorted(ls, key=lambda l: (KIND_ORDER.index(l["kind"]) if l["kind"] in KIND_ORDER else 5, l["id"])):
            uniq.setdefault(l["u"], {**l, "shared_by": shared[l["u"]]})
        item = {"key": p["key"], "post": p, "links": list(uniq.values())[:MAX_CHIPS], "merged": []}
        items.append(item)
        for u in uniq:
            claimed[u] = item
    for it in items:
        group = [it["post"]] + it["merged"]
        main = it["links"][0]["u"] if it["links"] else None
        if main:
            group += [posts[k] for k in sharers[main] if posts[k] not in group]
        group.sort(key=lambda p: (-reactions[p["key"]], p["key"]))
        it["shared_by"] = it["links"][0]["shared_by"] if it["links"] else 1
        authors = list(dict.fromkeys(p["author"] for p in group))
        it["authors"] = authors
        it["open_url"] = next((p["url"] for p in group if p["url"]), None)
        it["lead"] = group[0]  # the post behind authors[0]
        it["reactions"] = reactions[group[0]["key"]]

    sight_n = len(sightings)
    data.update({
        "posts_seen": sum(r["posts_seen"] or 0 for r in good),
        "posts_n": len(posts),
        "max_posts": cfg["max_posts"],
        "minutes": _minutes(good),
        "items": items,
        "skipped": skipped,
        "skipped_n": len(skipped),
        "promoted_n": sum(1 for p in posts.values() if p["is_promoted"]),
        "repeats_n": max(sight_n - len(posts), 0),
        "role": _profile_role(),
        "history": _history(conn, day, week, items, thr),
    })
    return data


def _minutes(runs) -> int | None:
    total = 0.0
    for r in runs:
        try:
            total += (datetime.fromisoformat(r["ended_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds()
        except (TypeError, ValueError):
            return None
    return round(total / 60)


def _history(conn, day: date, week: list, items: list, thr: int) -> dict:
    d = day.isoformat()
    kept_by = defaultdict(Counter)
    day_of = {}
    for p in week:
        pd = _day(p["first_seen_at"])
        day_of[p["key"]] = pd
        if pd != d and not p["is_promoted"] and (p["relevance"] or 0) >= thr:
            kept_by[_topic(p)][pd] += 1
    topics = {}
    for t in {_topic(it["post"]) for it in items}:
        n = 1
        while kept_by[t][(day - timedelta(days=n)).isoformat()]:
            n += 1
        topics[t] = {"days_building": n, "kept_last_7_days": {k: v for k, v in sorted(kept_by[t].items())}}
    today_urls = {l["u"] for it in items for l in it["links"]}
    recurring = []
    if today_urls:
        earlier = [k for k, pd in day_of.items() if pd != d]
        seen = defaultdict(set)
        for i in range(0, len(earlier), 500):
            part = earlier[i:i + 500]
            for l in conn.execute(
                f"SELECT post_key, COALESCE(resolved_url, url) AS u FROM links WHERE post_key IN ({','.join('?' * len(part))})", part
            ):
                if l["u"] in today_urls:
                    seen[l["u"]].add(day_of[l["post_key"]])
        recurring = [{"url": u, "earlier_days": len(s)} for u, s in sorted(seen.items())]
    return {"topics": topics, "recurring_links": recurring}


# ---------- digest (one Opus call, validated) ----------

def _digest_input(data: dict, cfg: dict) -> dict:
    return {
        "date": data["date"], "reads": data["passes"], "max_items": cfg["max_items"],
        "reader": data["role"], "history": data["history"],
        "items": [{
            "key": it["key"], "topic": _topic(it["post"]), "takeaway": it["post"]["takeaway"],
            "why": it["post"]["why"], "author": it["post"]["author"], "text": (it["post"]["text"] or "")[:600],
            "shared_by": it["shared_by"],
            "links": [{"kind": l["kind"], "title": l["title"], "description": l["description"],
                       "domain": _host(l["u"])} for l in it["links"]],
        } for it in data["items"][:30]],
    }


def _text(v, cap: int) -> str | None:
    return v.strip()[:cap] if isinstance(v, str) and v.strip() else None


def _groups(raw, final: list) -> list[dict]:
    """The model's 2–4 broad groups, in its order, each item in exactly one. Unknown keys are dropped;
    an item left out joins the group named like its topic, else "Also"; past four, the smallest go to "Also"."""
    keys = [it["key"] for it in final]
    groups, owner = [], {}
    for g in raw if isinstance(raw, list) else []:
        name = " ".join(str(g.get("name") or "").split()[:3])[:32] if isinstance(g, dict) else ""
        if not name or name.lower() == ALSO.lower():
            continue
        mine = [k for k in dict.fromkeys(x for x in g.get("keys") or [] if isinstance(x, str)) if k in keys and k not in owner]
        if mine:
            owner.update(dict.fromkeys(mine, len(groups)))
            groups.append({"name": name, "line": _text(g.get("line"), 80), "keys": mine})
    if not groups:  # no usable grouping: fall back to the stored topics
        for it in final:
            t = _topic(it["post"])
            if not any(g["name"] == t for g in groups):
                groups.append({"name": t, "line": None, "keys": []})
    by_name = {g["name"].lower(): g for g in groups}
    also = {"name": ALSO, "line": None, "keys": []}
    for it in final:
        if it["key"] not in owner:
            by_name.get(_topic(it["post"]).lower(), also)["keys"].append(it["key"])
    groups = [g for g in groups if g["keys"]]
    while len(groups) + bool(also["keys"]) > MAX_GROUPS:
        small = min(reversed(groups), key=lambda g: len(g["keys"]))
        groups.remove(small)
        also["keys"] += small["keys"]
    rank = {k: i for i, k in enumerate(keys)}
    for g in groups + [also]:
        g["keys"].sort(key=rank.__getitem__)  # best first inside each group
    return groups + ([also] if also["keys"] else [])


def validate_digest(out, items: list, max_items: int) -> dict:
    """Keep only what the model may decide: copy and ordering, keyed by posts we gave it."""
    out = out if isinstance(out, dict) else {}
    by_key = {it["key"]: it for it in items}
    order, copy = [], {}
    for x in out.get("items") or []:
        if isinstance(x, dict) and x.get("key") in by_key and x["key"] not in copy:
            order.append(x["key"])
            copy[x["key"]] = {"takeaway": _text(x.get("takeaway"), 300), "why": _text(x.get("why"), 400)}
    order += [k for k in by_key if k not in copy]  # missing items keep their stored copy
    order = order[:max_items]
    final = []
    for k in order:
        p = by_key[k]["post"]
        c = copy.get(k, {})
        final.append({**by_key[k], "takeaway": c.get("takeaway") or p["takeaway"] or (p["text"] or "")[:160],
                      "why": c.get("why") or p["why"] or ""})
    groups = _groups(out.get("groups"), final)
    keys = {it["key"] for it in final if it["links"]}
    three = [k for k in dict.fromkeys(x for x in out.get("open_three") or [] if isinstance(x, str)) if k in keys][:3]
    three += [it["key"] for it in final if it["links"] and it["key"] not in three][:3 - len(three)]
    return {"summary": _text(out.get("summary"), 700), "groups": groups, "items": final, "open_three": three}


def digest(data: dict, cfg: dict) -> dict:
    if not data["items"]:
        return validate_digest({}, [], cfg["max_items"])
    try:
        out = llm.complete_json("digest", cfg["models"]["digest"], PROMPT.read_text(),
                                json.dumps(_digest_input(data, cfg), ensure_ascii=False), effort="medium")
    except (llm.LLMError, ValueError, OSError) as e:
        print(f"Digest call failed, using stored copy: {e}", file=sys.stderr)
        out = {}
    return validate_digest(out, data["items"], cfg["max_items"])


# ---------- render ----------

def _host(u: str | None) -> str:
    h = urlparse(u or "").hostname or ""
    return h[4:] if h.startswith("www.") else h


def _href(u: str | None) -> str | None:
    return u if isinstance(u, str) and u.startswith(("https://", "http://")) else None


def _emph(s: str) -> str:
    """Escape, then allow the model's **bold** (safe: markers are matched after escaping)."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escape(s))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _ago(iso, day: date) -> str | None:
    try:
        n = (day - date.fromisoformat(str(iso)[:10])).days
    except ValueError:
        return None
    return "pushed today" if n <= 0 else "pushed yesterday" if n == 1 else f"{n} d ago"


def _num(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _chip_parts(l: dict, day: date) -> tuple[str, str, str, str | None]:
    """(kind label, title, bold meta, second meta line), all from the DB row."""
    u, m, kind, host = l["u"], l["meta"], l["kind"], _host(l["u"])
    label = "Gist" if host.startswith("gist.") else KIND_LABEL.get(kind, "Link")
    title = l["title"] or (host + urlparse(u).path).rstrip("/")[:90]
    if kind == "github":
        path = [s for s in urlparse(u).path.split("/") if s]
        if host == "github.com" and len(path) >= 2:
            title = f"{path[0]}/{path[1]}"
        bold = f"{_num(m.get('stars')):,} ★" if _num(m.get("stars")) is not None else label.lower() if label == "Gist" else host
        rest = " · ".join(x for x in [m.get("language"), _ago(m["last_push"], day) if m.get("last_push") else None] if x)
    elif kind == "paper":
        bold = "arXiv" if host.endswith("arxiv.org") else host
        authors = m.get("authors") if isinstance(m.get("authors"), list) else []
        rest = f"{_num(m.get('pages'))} pp" if _num(m.get("pages")) else (
            f"{authors[0]}{' et al.' if len(authors) > 1 else ''}" if authors else "")
    elif kind == "model":
        bold = f"{_num(m.get('downloads')):,} dl" if _num(m.get("downloads")) is not None else host
        rest = m.get("pipeline_tag") or ""
    else:
        bold = host
        rest = f"{_num(m.get('read_min'))} min" if _num(m.get("read_min")) else ""
    return label, str(title), bold, str(rest) if rest else None


def _new_today(it: dict, day: date, recurring: set) -> bool:
    """The resource itself appeared today or yesterday (its own date, from enrichment) and isn't a repeat."""
    if not it["links"] or it["links"][0]["u"] in recurring:
        return False
    m = it["links"][0]["meta"]
    for k in ("created_at", "published", "published_at"):
        try:
            if 0 <= (day - date.fromisoformat(str(m.get(k))[:10])).days <= 1:
                return True
        except ValueError:
            continue
    return False


def find_url(text: str) -> str:
    """A LinkedIn content search for the post's first distinctive words: the way back when there's no permalink."""
    text = re.sub(r"https?://\S+|[#@]\w+", " ", text or "")
    words = [w for w in re.findall(r"[^\W_](?:[\w'’.-]*[^\W_])?", text) if w.lower() not in STOP]
    return SEARCH + "?" + urlencode({"keywords": " ".join(list(dict.fromkeys(words))[:SEARCH_WORDS])}, quote_via=quote)


def _hue(s: str) -> int:
    return int(hashlib.md5((s or "").encode()).hexdigest()[:2], 16) * 360 // 256


def _initials(name: str) -> str:
    return "".join(w[0] for w in (name or "?").split()[:2]).upper() or "?"


def _media(url) -> str | None:
    """Relative path to a downloaded image: works from reports/ as a file and under /reports/ on the server."""
    f = media.local(url)
    return f"../media/{f}" if f else None


def _avatar(p: dict, size: str = "md") -> str:
    src = _media(p.get("author_avatar"))
    if src:
        return f'<img class="av {size}" src="{escape(src)}" alt="" loading="lazy">'
    return f'<span class="av {size}" style="--h:{_hue(p["author"])}">{escape(_initials(p["author"]))}</span>'


def _res(l: dict, day: date) -> str:
    label, title, bold, _rest = _chip_parts(l, day)
    inner = f'<span class="k">{escape(label)}</span><span class="rt">{escape(title)}</span><span class="rs">{escape(bold)}</span>'
    href = _href(l["u"])
    if not href:
        return f'<span class="res">{inner}</span>'
    return f'<a class="res" href="{escape(href)}" target="_blank" rel="noopener noreferrer">{inner}</a>'


def _picture(it: dict) -> str:
    """The post's own image, else the shared page's preview, else a quote card from the post text."""
    p, lead = it["post"], it["lead"]
    src = _media(p.get("image")) or next((_media(l["meta"].get("image")) for l in it["links"] if _media(l["meta"].get("image"))), None) \
        or _media(lead.get("image"))
    if src:
        return f'<div class="img"><img src="{escape(src)}" alt="" loading="lazy"></div>'
    text = " ".join((lead["text"] or "").split())
    quote = text if len(text) <= 150 else text[:150].rsplit(" ", 1)[0] + "…"
    return f'<div class="img quote" style="--h:{_hue(_topic(p))}"><p>“{escape(quote)}”</p></div>'


def _card(it: dict, day: date, recurring: set, group: str, hero: bool = False) -> str:
    p, lead = it["post"], it["lead"]
    if it["shared_by"] >= 2:
        badge = f'<span class="badge">shared by {it["shared_by"]}</span>'
    else:
        badge = '<span class="badge">new today</span>' if _new_today(it, day, recurring) else ""
    links = "".join(_res(l, day) for l in it["links"])
    if not links and p["link_in_hidden_comment"]:
        links = '<span class="res miss"><span class="k">Link</span><span class="rt">The link is in a hidden comment. Open the post to get it.</span></span>'
    name = escape(it["authors"][0])
    if (lead["author_url"] or "").startswith("https://www.linkedin.com/"):
        name = f'<a href="{escape(lead["author_url"])}" target="_blank" rel="noopener noreferrer">{name}</a>'
    others = len(it["authors"]) - 1
    small = f"and {_plural(others, 'other')}" if others else (lead.get("author_headline") or "")[:60]
    open_url = _href(it["open_url"])
    opener = (f'<a class="open" href="{escape(open_url)}" target="_blank" rel="noopener noreferrer">Open post ↗</a>' if open_url else
              f'<a class="open" href="{escape(find_url(lead["text"]))}" target="_blank" rel="noopener noreferrer">Find post ↗</a>')
    k = escape(it["key"])
    why = f'<p class="why">{escape(it["why"])}</p>' if it["why"] else ""
    return (f'<article class="card{" hero" if hero else ""}" data-g="{escape(group)}">{_picture(it)}<div class="body">'
            f'<div class="meta"><span class="topic">{escape(_topic(p))}</span>{badge}</div>'
            f'<h3>{escape(it["takeaway"])}</h3>{why}' + (f'<div class="links">{links}</div>' if links else "")
            + f'<footer>{_avatar(lead)}<span class="who"><span>{name}</span><small>{escape(small)}</small></span>'
              f'<span class="stat">{it["reactions"]:,} ♥</span>{opener}'
              f'<span class="fb"><button type="button" aria-label="More like this" title="More like this" data-key="{k}" data-verdict="more_like_this">＋</button>'
              f'<button type="button" aria-label="Not relevant" title="Not relevant" data-key="{k}" data-verdict="not_relevant">－</button></span>'
              f'</footer></div></article>')


def _skipped(data: dict) -> str:
    if not data["skipped_n"]:
        return ""
    shown = data["skipped"][:MAX_SKIPPED]
    stack = "".join(_avatar(p, "sm") for p in shown[:6])
    rows = "".join(
        f'<li>{_avatar(p, "sm")}<span>{escape(p["author"])}<small>{escape(p["takeaway"] or (p["text"] or "")[:90])}</small></span>'
        f'<button type="button" aria-label="More like this" title="More like this" data-key="{escape(p["key"])}" data-verdict="more_like_this">＋</button></li>'
        for p in shown)
    return (f'<details class="skipped"><summary><span class="stack">{stack}</span><span><b>{_plural(data["skipped_n"], "post")} skipped</b>'
            f' as unrelated to you</span><span class="peek">Peek</span></summary><ul>{rows}</ul></details>')


def _clock(iso) -> str | None:
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    return f"{(dt.astimezone() if dt.tzinfo else dt):%H:%M}"


def _top(right: str, middle: str = "") -> str:
    return (f'<header class="top"><div class="wrap row"><a class="logo" href="index.html"><span class="mark"></span>Feedcast</a>'
            f'{middle or "<span class=reads></span>"}{right}</div></header>')


def _setup_link() -> str:
    return f'<a class="btn ghost" href="http://127.0.0.1:{feedback.PORT}/setup">Setup</a>'


def _page(title: str, body: str) -> str:
    css = (TEMPLATE.parent / "style.css").read_text()
    return Template(TEMPLATE.read_text()).substitute(title=escape(title), css=css, body=body)


def _when(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def render(data: dict, dig: dict, cost: float, day: date, sample: bool = False) -> str:
    items = dig["items"]
    cap = max(data.get("max_posts") or 50, 1)
    reads = "".join(
        f'<li><b>{escape(c)}</b><i style="--v:{min((r["posts_seen"] or 0) / cap, 1):.2f}"></i><small>{r["posts_seen"]}</small></li>'
        for r in sorted(data["runs"], key=lambda r: r["started_at"] or "")
        if r["status"] in GOOD and (r["posts_seen"] or 0) > 0 and (c := _clock(r["started_at"])))
    summary = dig["summary"] or ("Here's what in your feed matched your interests today." if items
                                 else "Nothing in today's feed matched your interests.")
    pills = [f'<span class="pill strong">{len(items)} for you</span>',
             f'<span class="pill">{data["posts_n"]} posts read</span>',
             f'<span class="pill">{data["skipped_n"]} skipped</span>']
    if data["promoted_n"]:
        pills.append(f'<span class="pill">{_plural(data["promoted_n"], "ad")} dropped</span>')
    if sample:
        pills.append('<span class="pill">Sample data · invented people and repos</span>')
    group_of = {k: g["name"] for g in dig["groups"] for k in g["keys"]}
    tabs = ""
    if len(dig["groups"]) > 1:
        tabs = (f'<nav class="tabs" aria-label="Topics"><button class="tab on" type="button" data-f="all">All <span>{len(items)}</span></button>'
                + "".join(f'<button class="tab" type="button" data-f="{escape(g["name"])}">{escape(g["name"])} <span>{len(g["keys"])}</span></button>'
                          for g in dig["groups"]) + "</nav>")
    recurring = {r["url"] for r in data["history"]["recurring_links"]}
    cards = [_card(it, day, recurring, group_of.get(it["key"], ALSO), hero=i == 0) for i, it in enumerate(items)]
    colo = ["Read in your own Chrome",
            _plural(data["posts_seen"], "post") + (f" over {data['minutes']} min" if data["minutes"] else ""),
            "nothing clicked, liked or followed", "links opened without your session", f"${cost:.2f} today"]
    if data["role"]:
        colo.append(f"matched to: {data['role']}")
    body = (_top(_setup_link(), f'<ol class="reads" aria-label="Today\'s reads">{reads}</ol>')
            + f'<main class="wrap"><section class="intro"><p class="date">{day:%A} evening · {day.day} {day:%B}</p>'
              f'<h1>{_emph(summary)}</h1><div class="pills">{"".join(pills)}</div></section>{tabs}'
            + (cards[0] if cards else "")
            + (f'<section class="grid">{"".join(cards[1:])}</section>' if len(cards) > 1 else "")
            + f'{_skipped(data)}<p class="colo">{escape(" · ".join(colo))}</p></main>')
    return _page(f"Feedcast · {_when(day)}", body)


def render_index(conn) -> str:
    rows = conn.execute("SELECT date, json FROM reports ORDER BY date DESC").fetchall()
    cards = []
    for r in rows:
        try:
            stored = json.loads(r["json"] or "{}")
            d = date.fromisoformat(r["date"])
        except ValueError:
            continue
        n = len(stored.get("items") or [])
        cards.append(f'<a class="card" href="{escape(r["date"])}.html"><div class="body"><p class="date">{escape(_when(d))}</p>'
                     f'<h3>{escape(_first_sentence(stored.get("summary") or ""))}</h3>'
                     f'<div class="meta"><span class="topic">{n} for you</span></div></div></a>')
    main = f'<section class="days">{"".join(cards)}</section>' if cards else '<p class="empty">No pages yet.</p>'
    return _page("Feedcast", _top(_setup_link()) + f'<main class="wrap">{main}</main>')


def _first_sentence(s: str) -> str:
    s = s.replace("**", "").strip()
    return re.split(r"(?<=[.!?])\s+", s, maxsplit=1)[0][:200] if s else ""


# ---------- side effects (injectable) ----------

def notify(text: str, run=subprocess.run) -> None:
    # argv, not string interpolation, so post text can't break out of the AppleScript.
    run(["osascript", "-e", "on run argv", "-e", 'display notification (item 1 of argv) with title "Feedcast"',
         "-e", "end run", text], capture_output=True)


def _cost(conn, day: date) -> float:
    return sum(r["usd"] or 0 for r in _rows_on(conn, "llm_calls", "at", day))


def generate(day: date, *, conn=None, do_notify=True, do_open=True, run=subprocess.run,
             server_up=feedback.server_up, start_server=feedback.start_detached, port=feedback.PORT, sample=False):
    """Build, store and deliver one evening page. Returns its path, or None when there were no reads."""
    cfg = config.load()
    conn = conn or db.connect()
    data = gather(conn, day, cfg)
    if not data["passes"]:
        if do_notify:
            last = max(data["runs"], key=lambda r: r["started_at"] or "", default=None)
            notify(NO_READS.get(last["status"] if last else "", NO_READS["aborted_no_chrome"]), run)
        return None
    dig = digest(data, cfg)
    html = render(data, dig, _cost(conn, day), day, sample)
    out = config.reports_dir() / f"{day.isoformat()}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    stored = {"summary": dig["summary"], "groups": dig["groups"], "items": [it["key"] for it in dig["items"]],
              "open_three": dig["open_three"], "skipped": data["skipped_n"], "promoted": data["promoted_n"],
              "repeats": data["repeats_n"]}
    with conn:
        conn.execute("INSERT OR REPLACE INTO reports (date, json, html_path, opened_at, passes) VALUES (?,?,?,NULL,?)",
                     (day.isoformat(), json.dumps(stored), str(out), data["passes"]))
    (config.reports_dir() / "index.html").write_text(render_index(conn))
    if do_notify:
        notify(_first_sentence(dig["summary"] or "") or f"{_plural(len(dig['items']), 'thing')} for you today.", run)
    if do_open:
        run(["open", _page_url(day, port, out, server_up, start_server)], capture_output=True)
    return out


def _page_url(day: date, port: int, path: Path, server_up, start_server) -> str:
    """Prefer the local server so the buttons save; fall back to the file."""
    if not feedback.ensure_server(port, server_up, start_server):
        return path.as_uri()
    return f"http://127.0.0.1:{port}/reports/{day.isoformat()}.html"


def cmd_report(args) -> int:
    try:
        day = date.fromisoformat(args.date) if args.date else date.today()
    except ValueError:
        print("Use --date YYYY-MM-DD", file=sys.stderr)
        return 2
    out = generate(day, do_notify=not args.no_notify, do_open=not args.no_open)
    print(out if out else "No reads that day, so no page.")
    return 0


def register(sub) -> None:
    p = sub.add_parser("report", help="Write the evening page.")
    p.add_argument("--date", help="YYYY-MM-DD (default: today)")
    p.add_argument("--no-open", action="store_true", help="don't open the page")
    p.add_argument("--no-notify", action="store_true", help="no macOS notification")
    p.set_defaults(func=cmd_report)
