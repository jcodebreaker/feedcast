import contextlib
import http.cookiejar
import http.server
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures" / "ingest"
SAMPLE = ROOT / "fixtures" / "capture-sample.json"


def resp(status=200, body=b"", **headers):
    from feedcast.links import Response
    return Response(status, "", {k.lower().replace("_", "-"): v for k, v in headers.items()}, body)


class FakeWeb:
    """The injectable fetch: url -> Response, records every call. Unknown URLs are dead."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, method="GET", headers=None):
        self.calls.append((method, url, headers or {}))
        r = self.routes.get(url)
        return r(method) if callable(r) else r

    def hits(self, url):
        return sum(1 for _, u, _ in self.calls if u == url)


def sample_web():
    redirect = resp(301, location="https://github.com/oyelaran-labs/agent-replay/?utm_source=linkedin&trk=public_post")
    return FakeWeb({
        "https://lnkd.in/eX7qPz": redirect,
        "https://lnkd.in/b7Rk2a": lambda m: resp(200, (FIX / "interstitial.html").read_bytes() if m == "GET" else b""),
        "https://api.github.com/repos/oyelaran-labs/agent-replay": resp(200, (FIX / "github-repo.json").read_bytes()),
        "https://api.github.com/gists/abc123": resp(200, (FIX / "github-gist.json").read_bytes()),
        "https://export.arxiv.org/api/query?id_list=2610.01234": resp(200, (FIX / "arxiv.xml").read_bytes()),
        "https://weiming.engineering/tool-search": resp(200, (FIX / "article.html").read_bytes(), content_type="text/html"),
        "https://weiming.engineering/card.png": resp(200, b"PNG", content_type="image/png"),
        "https://media.licdn.com/dms/image/sample-post-1": resp(200, b"JPEG", content_type="image/jpeg; charset=binary"),
        "https://media.licdn.com/dms/image/sample-avatar-1": resp(200, b"<svg onload=alert(1)>", content_type="image/svg+xml"),
        "https://opengraph.githubassets.com/1/oyelaran-labs/agent-replay": resp(200, b"PNG", content_type="image/png"),
    })


class FakeLLM:
    """Scores every post it's given; records the prompts it saw."""

    def __init__(self, drop=()):
        self.calls, self.drop = [], set(drop)

    def __call__(self, purpose, model, system, user):
        self.calls.append((purpose, model, system, user))
        posts = json.loads(user[user.index("<posts>") + 7:user.index("</posts>")])
        out = {"urn:li:activity:not-asked": {"relevance": 3}}
        for p in posts:
            if p["key"] in self.drop:
                continue
            hit = "eval" in p["text"].lower() or "agent-replay" in json.dumps(p["links"])
            out[p["key"]] = {"relevance": 3 if hit else 0, "topic": "agent evals" if hit else None,
                             "why": "A CI gate for agent evals." if hit else "", "takeaway": p["text"][:60],
                             "substance": 4, "hype": 1, "is_ad": "download the guide" in p["text"].lower()}
        return "Here you go:\n" + json.dumps(out)


class Base(unittest.TestCase):
    def setUp(self):
        from feedcast import links, llm
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["FEEDCAST_HOME"] = self.tmp.name
        shutil.copy(FIX / "profile.yaml", self.home / "profile.yaml")
        links.set_github_token("")
        self.web = sample_web()
        links.set_fetch(self.web)
        self.llm = FakeLLM()
        llm.set_fake(self.llm)

    def tearDown(self):
        from feedcast import links, llm
        llm.set_fake(None)
        links.set_fetch(None)
        links.set_github_token(None)
        self.tmp.cleanup()

    def write_run(self, name, capture):
        p = self.home / "runs" / "2026-10-30" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(capture))
        return p

    def ingest(self, *argv):
        from feedcast import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["ingest", *argv])
        return code, out.getvalue().strip()

    def db(self):
        from feedcast import db
        return db.connect()


class IngestTest(Base):
    def test_sample_run_line_links_and_promoted(self):
        self.write_run("0900.json", json.loads(SAMPLE.read_text()))
        code, out = self.ingest()
        self.assertEqual(code, 0)
        # Posts 1, 6, 7, 9 mention evals or link agent-replay; the promoted ad is never scored.
        self.assertEqual(out, "09:00 read: 12 posts, 12 new, 4 kept, 2 links resolved")
        conn = self.db()
        ad = conn.execute("SELECT * FROM posts WHERE author = 'Acme AI'").fetchone()
        self.assertEqual(ad["is_promoted"], 1)
        self.assertIsNone(ad["scored_at"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM links WHERE post_key = ?", (ad["key"],)).fetchone()[0], 0)
        self.assertNotIn("Acme", "".join(c[3] for c in self.llm.calls))
        self.assertEqual(len(self.llm.calls), 1)  # 11 organic posts, one batch

        # lnkd.in → repo with tracking stripped; shared with post 7, enriched once.
        from feedcast import links
        repo = "https://github.com/oyelaran-labs/agent-replay"
        self.assertEqual(links.shared_by(conn, repo), 2)
        self.assertEqual(self.web.hits("https://api.github.com/repos/oyelaran-labs/agent-replay"), 1)
        row = conn.execute("SELECT * FROM links WHERE resolved_url = ? AND url = 'https://lnkd.in/eX7qPz'", (repo,)).fetchone()
        self.assertEqual((row["kind"], row["title"]), ("github", "oyelaran-labs/agent-replay"))
        self.assertEqual(json.loads(row["meta_json"])["stars"], 2310)

        # Interstitial page: target pulled from the anchor, tracking stripped.
        row = conn.execute("SELECT * FROM links WHERE url = 'https://lnkd.in/b7Rk2a'").fetchone()
        self.assertEqual(row["resolved_url"], "https://github.com/lindqvist/slidefit")

        paper = conn.execute("SELECT * FROM links WHERE kind = 'paper'").fetchone()
        self.assertEqual(paper["title"], "When Does the Judge Disagree?")
        self.assertEqual(paper["description"], "We grade 1,800 agent trajectories with LLM judges and humans.")
        self.assertEqual(json.loads(paper["meta_json"])["authors"], ["Anneliese Brandt", "Kofi Mensah"])

        art = conn.execute("SELECT * FROM links WHERE resolved_url = 'https://weiming.engineering/tool-search'").fetchone()
        self.assertEqual((art["kind"], art["title"]), ("article", "212 tools, 9 MCP servers, one search step"))
        self.assertEqual(json.loads(art["meta_json"])["read_min"], 6)

        gist = conn.execute("SELECT * FROM links WHERE resolved_url LIKE 'https://gist.github.com/%'").fetchone()
        self.assertEqual((gist["title"], json.loads(gist["meta_json"])["type"]), ("example-retrieval-prompt.py", "gist"))

        # Dead repo (API unknown): still a row with kind and domain, no description.
        dead = conn.execute("SELECT * FROM links WHERE resolved_url = 'https://github.com/khoury-ai/agent-failure-cases'").fetchone()
        self.assertEqual(dead["kind"], "github")
        self.assertIsNone(dead["description"])
        self.assertEqual(json.loads(dead["meta_json"])["domain"], "github.com")

        scored = conn.execute("SELECT * FROM posts WHERE key = 'urn:li:activity:7251000000000000001'").fetchone()
        self.assertEqual(scored["author_url"], "https://www.linkedin.com/in/mariana-oyelaran-example/")

        # Images: stored from the capture, previews recorded per link, kept posts' images downloaded cookie-less.
        from feedcast import media
        self.assertEqual(scored["image"], "https://media.licdn.com/dms/image/sample-post-1")
        self.assertEqual(media.link_image(conn, scored["key"]), "https://opengraph.githubassets.com/1/oyelaran-labs/agent-replay")
        self.assertEqual(json.loads(art["meta_json"])["image"], "https://weiming.engineering/card.png")
        self.assertRegex(media.local(scored["image"]) or "", r"^[0-9a-f]{40}\.jpg$")
        self.assertIsNone(media.local(scored["author_avatar"]))  # SVG is never kept
        self.assertTrue(media.local("https://opengraph.githubassets.com/1/oyelaran-labs/agent-replay"))
        self.assertEqual(self.web.hits("https://opengraph.githubassets.com/1/oyelaran-labs/agent-replay"), 1)
        self.assertEqual(self.web.hits("https://weiming.engineering/card.png"), 0)  # not kept, not fetched
        self.assertTrue(all("Cookie" not in h for _, _, h in self.web.calls))
        # https only, two redirects at most, 3 MB at most
        self.web.routes.update({
            "https://img.example/big": resp(200, b"x" * (media.MAX_BYTES + 1), content_type="image/png"),
            "https://img.example/r1": resp(302, location="https://img.example/r2"),
            "https://img.example/r2": resp(302, location="https://img.example/r3"),
            "https://img.example/r3": resp(302, location="https://img.example/ok"),
            "https://img.example/down": resp(302, location="http://img.example/ok"),
            "https://img.example/ok": resp(200, b"GIF", content_type="image/gif"),
        })
        for bad in ("https://img.example/big", "https://img.example/r1", "https://img.example/down", "http://img.example/ok"):
            self.assertIsNone(media.download(bad), bad)
        self.assertTrue(media.download("https://img.example/r2").endswith(".gif"))
        # The model's "agent evals" becomes a Title Case label; the project column is no longer written.
        self.assertEqual((scored["relevance"], scored["topic"], scored["project"], scored["substance"]), (3, "Agent Evals", None, 4))

    def test_second_run_dedupes_sights_and_reuses_cache(self):
        capture = json.loads(SAMPLE.read_text())
        head = ("Small models are underrated for routing. We moved intent detection off the big model and kept quality while "
                "cutting the bill in half.")
        capture["posts"].append({"author": "Lena Okafor", "text": head + " Here is wh…more"})
        self.write_run("0900.json", capture)
        self.ingest()
        later = json.loads(SAMPLE.read_text())
        # No permalink, and this read cut the text past the fold somewhere else: still the same post.
        later["posts"].append({"author": "Lena Okafor", "text": head + " Here is what we measured over six weeks."})
        later["run_started_at"] = "2026-10-30T13:30:00+01:00"
        later["posts"][0]["reactions"] = 2000
        later["posts"].append({"urn": "urn:li:activity:7251000000000000099", "author": "Rafael Bento",
                               "text": "agent-replay caught our regression before deploy.",
                               "links": [{"url": "https://github.com/oyelaran-labs/agent-replay", "source": "body"}]})
        later["posts"].append(dict(later["posts"][-1]))  # shown twice in one scroll
        self.write_run("1330.json", later)
        code, out = self.ingest()
        self.assertEqual(out, "13:30 read: 15 posts, 1 new, 1 kept, 0 links resolved")

        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 14)
        self.assertEqual([r[0] for r in conn.execute("SELECT posts_new FROM runs ORDER BY id")], [13, 1])
        sightings = conn.execute(
            "SELECT reactions FROM sightings WHERE post_key = 'urn:li:activity:7251000000000000001' ORDER BY run_id").fetchall()
        self.assertEqual([r[0] for r in sightings], [1240, 2000])
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM sightings WHERE post_key = 'urn:li:activity:7251000000000000099'").fetchone()[0], 1)
        from feedcast import links
        self.assertEqual(links.shared_by(conn, "https://github.com/oyelaran-labs/agent-replay"), 3)
        self.assertEqual(self.web.hits("https://api.github.com/repos/oyelaran-labs/agent-replay"), 1)  # 24 h cache
        self.assertEqual(len(self.llm.calls), 2)  # only the new post went out

        # Idempotent: nothing pending, and naming a file again changes nothing.
        self.assertEqual(self.ingest(), (0, "Nothing new to read."))
        self.assertEqual(self.ingest(str(self.home / "runs/2026-10-30/1330.json")), (0, "runs/2026-10-30/1330.json: already read"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0], 27)
        self.assertEqual(len(self.llm.calls), 2)

    def test_github_token_sent_when_available(self):
        from feedcast import links
        links.set_github_token("tok-placeholder")
        links.enrich("https://github.com/oyelaran-labs/agent-replay", "github", "repo")
        self.assertEqual(self.web.calls[-1][2]["Authorization"], "Bearer tok-placeholder")


class UrlTest(unittest.TestCase):
    def test_classification_table(self):
        from feedcast.links import classify
        table = {
            "https://github.com/a/b": ("github", "repo"),
            "https://github.com/a/b/releases/tag/v1.2": ("github", "release"),
            "https://gist.github.com/a/123": ("github", "gist"),
            "https://arxiv.org/abs/2610.01234": ("paper", None),
            "https://openreview.net/forum?id=x": ("paper", None),
            "https://aclanthology.org/2026.acl-long.1/": ("paper", None),
            "https://cs.example.edu/~x/paper.pdf": ("paper", None),
            "https://huggingface.co/org/model": ("model", None),
            "https://www.youtube.com/watch?v=x": ("video", None),
            "https://youtu.be/x": ("video", None),
            "https://www.loom.com/share/x": ("video", None),
            "https://www.linkedin.com/pulse/x": ("linkedin", None),
            "https://example.substack.com/p/x": ("article", None),
            "https://lnkd.in/unresolved": ("other", None),
            "mailto:a@example.com": ("other", None),
        }
        for url, want in table.items():
            self.assertEqual(classify(url), want, url)

    def test_tracking_params_stripped(self):
        from feedcast.links import clean
        self.assertEqual(clean("https://Example.com/p?utm_source=li&utm_medium=x&id=7&trk=a&fbclid=z#top"),
                         "https://example.com/p?id=7")
        self.assertEqual(clean("https://github.com/a/b.git/"), "https://github.com/a/b")


class _Handler(http.server.BaseHTTPRequestHandler):
    seen = []
    routes = {"/set-cookie": "/a", "/a": "/b", "/b": "/c", "/h1": "/h2", "/h2": "/h3", "/h3": "/h4", "/h4": "/end"}

    def _go(self, method):
        type(self).seen.append((method, self.path, self.headers.get("Cookie")))
        if self.path == "/nohead" and method == "HEAD":
            self.send_response(405)
        elif self.path == "/nohead":
            self.send_response(302)
            self.send_header("Location", "/end")
        elif self.path in self.routes:
            self.send_response(302)
            self.send_header("Location", self.routes[self.path])
            self.send_header("Set-Cookie", "li_at=secret-placeholder; Path=/")
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self._go("HEAD")

    def do_GET(self):
        self._go("GET")

    def log_message(self, *a):
        pass


class ResolveTest(unittest.TestCase):
    """The real urllib fetch against a local server."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        from feedcast import links
        links.set_fetch(None)
        _Handler.seen = []

    def tearDown(self):
        urllib.request.install_opener(None)

    def test_no_cookie_ever_reaches_the_server(self):
        from feedcast import links
        # Even with a global cookie-carrying opener holding a cookie, resolution sends none.
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        urllib.request.install_opener(opener)
        opener.open(self.base + "/end").read()
        jar.set_cookie(http.cookiejar.Cookie(0, "li_at", "x", None, False, "127.0.0.1", True, False, "/", True,
                                             False, None, False, None, None, {}))
        _Handler.seen = []
        resolved, ok = links.resolve(self.base + "/set-cookie")
        self.assertEqual((resolved, ok), (self.base + "/c", True))
        self.assertEqual([p for _, p, _ in _Handler.seen], ["/set-cookie", "/a", "/b"])
        self.assertTrue(all(c is None for _, _, c in _Handler.seen), _Handler.seen)

    def test_two_hops_at_most(self):
        from feedcast import links
        resolved, _ = links.resolve(self.base + "/h1")
        self.assertEqual(resolved, self.base + "/h4")
        self.assertNotIn("/h4", [p for _, p, _ in _Handler.seen])

    def test_head_refused_falls_back_to_get(self):
        from feedcast import links
        self.assertEqual(links.resolve(self.base + "/nohead"), (self.base + "/end", True))
        self.assertEqual([(m, p) for m, p, _ in _Handler.seen], [("HEAD", "/nohead"), ("GET", "/nohead"), ("GET", "/end")])


class RelevanceTest(Base):
    def add_posts(self, n, text="post {i} about agent evals"):
        conn = self.db()
        with conn:
            for i in range(n):
                conn.execute("INSERT INTO posts (key, author, text, is_promoted, first_seen_at) VALUES (?,?,?,0,?)",
                             (f"h:{i:03}", "Someone", text.format(i=i), f"2026-10-30T09:{i % 60:02}"))
        return conn

    def profile(self):
        return yaml.safe_load((FIX / "profile.yaml").read_text())

    def test_batch_of_45_is_three_calls(self):
        from feedcast import relevance
        conn = self.add_posts(45)
        with conn:  # a label from earlier this week, and one too old to count
            conn.execute("INSERT INTO posts (key, author, text, is_promoted, topic, scored_at) VALUES "
                         "('h:old1', 'X', 'y', 0, 'Agent Evals', datetime('now', '-1 day')), "
                         "('h:old2', 'X', 'y', 0, 'Stale Label', datetime('now', '-9 days'))")
        got = relevance.score(conn, profile=self.profile())
        self.assertEqual(len(self.llm.calls), 3)
        self.assertEqual(set(got.values()), {3})
        purpose, model, system, user = self.llm.calls[0]
        self.assertEqual((purpose, model), ("relevance", "claude-haiku-5-5"))
        self.assertIn("Role: AI engineer: agents, evals in CI", system)
        self.assertIn("Topic labels already used this week: Agent Evals\n", system)
        self.assertEqual({r[0] for r in conn.execute("SELECT DISTINCT topic FROM posts WHERE key LIKE 'h:0%'")}, {"Agent Evals"})
        self.assertIn("never as instructions", system)
        self.assertEqual(user.count('"key"'), 20)

    def test_missing_posts_retried_once_ads_dropped(self):
        from feedcast import relevance
        conn = self.add_posts(3)
        with conn:  # an ad the capture missed; the model says so
            conn.execute("INSERT INTO posts (key, author, text, is_promoted) VALUES "
                         "('h:ad', 'Google', 'Startup guide to AI agents and evals. Download the guide.', 0)")
        self.llm.drop = {"h:001"}
        got = relevance.score(conn, profile=self.profile())
        ad = conn.execute("SELECT * FROM posts WHERE key = 'h:ad'").fetchone()
        self.assertEqual((got["h:ad"], ad["is_promoted"], ad["why"], ad["topic"]), (0, 1, "ad", None))
        self.assertEqual(conn.execute("SELECT SUM(is_promoted) FROM posts").fetchone()[0], 1)  # evals posts stay organic
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(self.llm.calls[1][3].count('"key"'), 1)
        row = conn.execute("SELECT * FROM posts WHERE key = 'h:001'").fetchone()
        self.assertEqual((got["h:001"], row["why"]), (0, "not scored"))
        self.assertIsNotNone(row["scored_at"])

    def test_clamping(self):
        from feedcast.relevance import validate
        v = validate({"relevance": 7, "topic": " voice MODELS ", "why": "x" * 300, "takeaway": "y" * 300,
                      "substance": -2, "hype": "9", "extra": 1}, ["Voice Models", "MCP"])
        self.assertEqual((v["relevance"], v["topic"], v["substance"], v["hype"]), (3, "Voice Models", 0, 5))
        self.assertEqual((len(v["why"]), len(v["takeaway"])), (160, 140))
        self.assertEqual(validate({"relevance": 2, "topic": "browser speech recognition models"}, [])["topic"], "Browser Speech Recognition")
        self.assertEqual(validate({"relevance": 2, "topic": "evals for vLLM"}, [])["topic"], "Evals for vLLM")
        self.assertIsNone(validate({"relevance": 0, "topic": "MCP"}, ["MCP"])["topic"])
        self.assertIsNone(validate({"relevance": 2, "topic": 3}, [])["topic"])
        self.assertIsNone(validate({"relevance": "high"}, []))
        self.assertIsNone(validate("3", []))

    def test_mutes_never_reach_the_model(self):
        from feedcast import config, relevance
        cfg = config.load()
        cfg["mutes"] = ["vibe coding"]
        conn = self.add_posts(1, "Vibe coding is dead. Here's what's next.")
        with conn:
            conn.execute("INSERT INTO posts (key, author, text, is_promoted) VALUES ('h:c', 'X', 'Crypto agents are here', 0)")
            conn.execute("INSERT INTO posts (key, author, text, is_promoted) VALUES ('h:h', 'X', 'My hiring takes, a thread', 0)")
            # A lead-gen card whose call to action sits only in the resolved page's description.
            conn.execute("INSERT INTO posts (key, author, text, is_promoted) VALUES ('h:g', 'X', 'Agents in production', 0)")
            conn.execute("INSERT INTO links (post_key, url, resolved_url, description) VALUES "
                         "('h:g', 'https://goo.gle/x', 'https://example.com/x', 'A deep dive into agents. Download now.')")
        got = relevance.score(conn, cfg=cfg, profile=self.profile())
        self.assertEqual(got, {"h:000": 0, "h:c": 0, "h:h": 0, "h:g": 0})
        self.assertEqual(conn.execute("SELECT is_promoted, why FROM posts WHERE key = 'h:g'").fetchone()[:], (1, "ad"))
        self.assertEqual(self.llm.calls, [])
        whys = {r[0]: r[1] for r in conn.execute("SELECT key, why FROM posts")}
        self.assertEqual(whys, {"h:000": "muted: vibe coding", "h:c": "muted: crypto", "h:h": "muted: hiring takes", "h:g": "ad"})


if __name__ == "__main__":
    unittest.main()
