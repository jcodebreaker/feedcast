"""Report + feedback server. Seeds a temp DB from invented fixtures; the LLM is faked; no network beyond 127.0.0.1."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures" / "report"
DAY = date(2026, 10, 30)
K = "urn:li:activity:73000000000000000"  # + two digits


def seed(conn, mutate=None):
    """Load fixtures/report/day.json into the DB; `mutate(data)` tweaks it first."""
    from feedcast import config
    data = json.loads((FIX / "day.json").read_text())
    if mutate:
        mutate(data)
    config.profile_path().parent.mkdir(parents=True, exist_ok=True)
    config.profile_path().write_text(yaml.safe_dump(data["profile"]))
    with conn:
        for r in data["runs"]:
            conn.execute("INSERT INTO runs (id, file, started_at, ended_at, posts_seen, posts_new, status) VALUES (?,?,?,?,?,?,?)",
                         (r["id"], r["file"], r["started_at"], r["ended_at"], r["posts_seen"], r["posts_new"], r["status"]))
        for p in data["posts"]:
            conn.execute(
                "INSERT INTO posts (key, author, author_headline, author_url, author_avatar, url, text, image, first_seen_run, first_seen_at,"
                " is_suggested, is_promoted, link_in_hidden_comment, relevance, topic, why, takeaway) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["key"], p["author"], p.get("author_headline"), p.get("author_url"), p.get("author_avatar"), p["url"], p["text"],
                 p.get("image"), p["run"], p["seen"], p["is_suggested"], p.get("is_promoted", 0),
                 p.get("link_in_hidden_comment", 0), p["relevance"], p["topic"], p["why"], p["takeaway"]))
            conn.execute("INSERT INTO sightings (post_key, run_id, reactions) VALUES (?,?,?)", (p["key"], p["run"], p["reactions"]))
        for key, run in data["sightings"]:
            conn.execute("INSERT INTO sightings (post_key, run_id, reactions) VALUES (?,?,0)", (key, run))
        for l in data["links"]:
            conn.execute("INSERT INTO links (post_key, url, resolved_url, kind, title, description, meta_json) VALUES (?,?,?,?,?,?,?)",
                         (l["post_key"], l["url"], l["resolved_url"], l["kind"], l["title"], l["description"], json.dumps(l["meta"])))
        for c in data["llm_calls"]:
            conn.execute("INSERT INTO llm_calls (at, purpose, model, usd) VALUES (?,?,?,?)", (c["at"], c["purpose"], c["model"], c["usd"]))


def fake_digest(reply=None):
    reply = reply if reply is not None else json.loads((FIX / "digest.json").read_text())
    calls = []

    def fn(purpose, model, system, user):
        calls.append((purpose, model, json.loads(user)))
        return json.dumps(reply)
    return fn, calls


class Effects:
    def __init__(self, up=True):
        self.cmds, self.started, self.up = [], [], up

    def run(self, cmd, **kw):
        self.cmds.append(cmd)

    def server_up(self, port):
        return self.up

    def start(self, port):
        self.started.append(port)
        self.up = True


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FEEDCAST_HOME"] = self.tmp.name
        from feedcast import db
        self.conn = db.connect()

    def tearDown(self):
        from feedcast import llm
        llm.set_fake(None)
        self.conn.close()
        self.tmp.cleanup()

    def make(self, reply=None, mutate=None, up=True):
        from feedcast import llm, report
        seed(self.conn, mutate)
        fn, calls = fake_digest(reply)
        llm.set_fake(fn)
        fx = Effects(up)
        out = report.generate(DAY, conn=self.conn, run=fx.run, server_up=fx.server_up, start_server=fx.start)
        return out, calls, fx

    def test_counts_and_links_come_from_db(self):
        reply = json.loads((FIX / "digest.json").read_text())
        reply["items"][0].update({"shared_by": 99, "links": [{"url": "https://evil.example/x"}], "stars": 1})
        reply["groups"].append({"name": "Invented Topic", "line": "Made up", "keys": ["urn:li:activity:999"]})
        out, calls, fx = self.make(reply)
        html = out.read_text()
        html = html[html.index("<body>"):]  # the page, not the stylesheet
        self.assertEqual(calls[0][0], "digest")
        self.assertEqual(calls[0][1], "claude-opus-5-5")
        self.assertIn('<span class="badge">shared by 3</span>', html)  # three of today's posts share agent-replay
        self.assertNotIn("99", html)
        self.assertNotIn("evil.example", html)
        self.assertNotIn("Invented Topic", html)
        self.assertIn("2,310 ★", html)
        self.assertIn('Mariana Oyelaran</span><small>and 2 others</small>', html)
        self.assertIn('<span class="pill strong">6 for you</span><span class="pill">11 posts read</span>'
                      '<span class="pill">2 skipped</span><span class="pill">1 ad dropped</span>', html)
        self.assertIn("$0.01 today", html)  # only today's calls
        self.assertIn("142 posts over 13 min", html)
        self.assertIn("new today", html)  # agent-failure-cases created today
        self.assertIn("The link is in a hidden comment", html)
        self.assertIn("Friday evening · 30 October", html)
        self.assertIn("<h1>Agent evals in CI, three days running now, and <b>three people shared the same harness repo</b>.", html)
        self.assertEqual(html.count("<li><b>"), 3)  # today's three reads in the top bar
        self.assertIn('href="http://127.0.0.1:8765/setup"', html)
        # history went to the model per topic: Agent Evals kept posts on the two days before
        self.assertEqual(calls[0][2]["history"]["topics"]["Agent Evals"]["days_building"], 3)
        self.assertEqual(calls[0][2]["reader"], "AI engineer: agents, MCP, evals; TypeScript, Python")
        # tabs from the model's broad groups in its order; 05 joins its topic's group, 08 (no group) lands in "Also"
        tabs = [html.index(f'class="tab" type="button" data-f="{t}"') for t in ("Agent Evals", "Tools &amp; MCP", "Also")]
        self.assertEqual(tabs, sorted(tabs))
        self.assertIn('data-f="Agent Evals">Agent Evals <span>3</span>', html)
        self.assertEqual(html.count('type="button" data-f='), 4)
        self.assertEqual(html.count('<article class="card hero" data-g="Agent Evals">'), 1)  # best item first, as the hero
        self.assertEqual(html.count('<article class="card'), 6)
        self.assertIn('<article class="card" data-g="Also">', html)
        self.assertLess(html.index('class="card hero"'), html.index('class="grid"'))
        # no permalink: a content search for the post's first words, and the author's page
        self.assertIn('href="https://www.linkedin.com/search/results/content/?keywords=212%20tools%209%20MCP%20servers%20one%20search%20step"'
                      ' target="_blank" rel="noopener noreferrer">Find post ↗</a>', html)
        self.assertIn('<a href="https://www.linkedin.com/in/deng-weiming-example/" target="_blank" rel="noopener noreferrer">Deng Weiming</a>', html)
        self.assertEqual(html.count(">Open post ↗</a>"), 5)
        self.assertIn('<b>2 posts skipped</b> as unrelated to you', html)
        self.assertIn("matched to: AI engineer: agents, MCP, evals; TypeScript, Python", html)
        for name in ("nova", "codegraph", "deckforge", "profile:"):
            self.assertNotIn(name, html.lower())
        self.assertNotIn("Sample data", html)
        row = self.conn.execute("SELECT passes, html_path FROM reports WHERE date='2026-10-30'").fetchone()
        self.assertEqual(row["passes"], 3)
        self.assertEqual(fx.cmds[0][0], "osascript")
        self.assertTrue(fx.cmds[0][-1].startswith("Agent evals in CI, three days running now"))
        self.assertNotIn("**", fx.cmds[0][-1])
        self.assertEqual(fx.cmds[1], ["open", "http://127.0.0.1:8765/reports/2026-10-30.html"])

    def test_escapes_post_text_and_model_output(self):
        evil = '<script>alert(1)</script>'

        def mutate(d):
            p = d["posts"][8]  # a skipped post
            p["author"], p["takeaway"] = evil, '"><img src=x onerror=alert(2)>'
            d["links"][3]["title"] = evil
            d["posts"][0]["url"] = "javascript:alert(3)"
            d["posts"][5]["text"] = 'Ship <script>x</script> & "agents" fast https://evil.example/?a=1&b=2 #ad'
            d["posts"][5]["author_url"] = 'javascript:alert(4)'  # not a stored shape; never linked anyway
        reply = json.loads((FIX / "digest.json").read_text())
        reply["summary"] = "**<b onclick=x>bold</b>** " + evil
        reply["items"][1]["why"] = evil
        reply["groups"][0]["line"] = evil
        out, _, _ = self.make(reply, mutate)
        html = out.read_text()
        self.assertNotIn(evil, html)
        self.assertNotIn("<img", html)
        self.assertNotIn("<b onclick", html)
        self.assertNotIn("javascript:", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertEqual(html.count("<script>"), 1)  # only the page's own
        self.assertIn('?keywords=Ship%20script%20x%20agents%20fast"', html)

    def test_unknown_and_missing_keys(self):
        reply = {"summary": "Short day.", "items": [
            {"key": "urn:li:activity:999", "takeaway": "ghost", "why": "ghost"},
            {"key": K + "06", "takeaway": "Tool search first.", "why": "For codegraph-mcp."},
        ], "open_three": ["urn:li:activity:999", K + "06"]}
        out, _, _ = self.make(reply)
        html = out.read_text()
        self.assertNotIn(">ghost<", html)
        self.assertLess(html.index("Tool search first."), html.index("Fit text up front instead of catching overflow."))
        self.assertIn("A harness that fails CI on agent eval regressions.", html)  # stored copy fallback
        stored = json.loads(self.conn.execute("SELECT json FROM reports").fetchone()["json"])
        self.assertEqual(stored["items"][0], K + "06")
        self.assertEqual(stored["open_three"][0], K + "06")
        self.assertEqual(len(stored["open_three"]), 3)
        self.assertEqual(len(stored["items"]), 6)  # merged sharers are one item
        # no groups from the model: the stored topics, in order of each topic's best item
        self.assertEqual([g["name"] for g in stored["groups"]], ["MCP", "Agent Evals", "Slide Layout"])

        # every item in exactly one group, at most four: the smallest groups merge into "Also"
        from feedcast.report import validate_digest
        items = [{"key": f"k{i}", "links": [], "post": {"topic": f"T{i}", "takeaway": "t", "why": "", "text": ""}} for i in range(7)]
        groups = [{"name": "G1", "line": "big", "keys": ["k0", "k1", "k0"]}, {"name": "G2", "keys": ["k2", "k1"]},
                  {"name": "G3", "keys": ["k3"]}, {"name": "G4", "keys": ["k4"]}, {"name": "G5", "keys": ["k5", "ghost"]},
                  {"name": "", "keys": ["k6"]}, {"name": "T6 group", "keys": []}]
        got = validate_digest({"groups": groups}, items, 8)["groups"]
        self.assertEqual([(g["name"], g["keys"]) for g in got],
                         [("G1", ["k0", "k1"]), ("G2", ["k2"]), ("G3", ["k3"]), ("Also", ["k4", "k5", "k6"])])
        self.assertEqual(got[0]["line"], "big")

    def test_zero_runs_notifies_once_and_writes_nothing(self):
        def mutate(d):  # today's runs happened another day
            for r in d["runs"][2:]:
                r["started_at"] = r["started_at"].replace("2026-10-30", "2026-11-02")
        out, calls, fx = self.make(mutate=mutate)
        self.assertIsNone(out)
        self.assertEqual(calls, [])
        self.assertEqual(len(fx.cmds), 1)
        self.assertEqual(fx.cmds[0][-1], "No reads today. Chrome was closed.")
        from feedcast import config
        self.assertFalse((config.reports_dir() / "2026-10-30.html").exists())

    def test_aborted_run_status_in_notification(self):
        def mutate(d):
            for r in d["runs"][2:]:
                r["status"] = "aborted_challenge"
        out, _, fx = self.make(mutate=mutate)
        self.assertIsNone(out)
        self.assertEqual(fx.cmds[0][-1], "No reads today. LinkedIn asked for a check.")

    def test_index_rebuilt_newest_first_and_server_started(self):
        from feedcast import config
        self.conn.execute("INSERT INTO reports (date, json) VALUES ('2026-10-28', ?)", (json.dumps({"summary": "Older day. More."}),))
        self.conn.commit()
        out, _, fx = self.make(up=False)
        idx = (config.reports_dir() / "index.html").read_text()
        self.assertLess(idx.index("2026-10-30.html"), idx.index("2026-10-28.html"))
        self.assertIn("Older day.", idx)
        self.assertNotIn("More.", idx)
        self.assertEqual(fx.started, [8765])

    def test_dark_mode_tokens_and_local_images_only(self):
        from feedcast import media
        post_img, avatar = "https://media.licdn.com/dms/image/p1", "https://media.licdn.com/dms/image/a6"
        media.media_dir().mkdir(parents=True)
        for u in (post_img, avatar):
            (media.media_dir() / f"{media._stem(u)}.jpg").write_bytes(b"JPEG")

        def mutate(d):
            d["posts"][0]["image"] = post_img
            d["posts"][5]["author_avatar"] = avatar
            d["posts"][3]["image"] = "https://media.licdn.com/dms/image/never-downloaded"
        out, _, _ = self.make(mutate=mutate)
        html = out.read_text()
        self.assertIn("@media (prefers-color-scheme:dark)", html)
        self.assertIn("--bg:#0c0d10", html)
        self.assertIn("Helvetica Neue", html)  # font fallbacks
        self.assertNotIn("<script src", html)
        self.assertIn(f'<div class="img"><img src="../media/{media._stem(post_img)}.jpg"', html)
        self.assertIn(f'<img class="av md" src="../media/{media._stem(avatar)}.jpg"', html)
        self.assertNotIn("media.licdn.com", html)  # remote images are never loaded by the page
        self.assertEqual(html.count('class="img quote"'), 5)  # no image: the quote card


class ServeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FEEDCAST_HOME"] = self.tmp.name
        from feedcast import config, db, feedback
        conn = db.connect()
        with conn:
            conn.execute("INSERT INTO posts (key, author, text) VALUES ('urn:li:activity:1', 'A', 'b')")
        conn.close()
        config.reports_dir().mkdir(parents=True)
        (config.reports_dir() / "2026-10-30.html").write_text("<p>page</p>")
        self.srv = feedback.make_server(0)
        self.port = self.srv.server_port
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def post(self, body, origin=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/feedback", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Origin": origin or f"http://127.0.0.1:{self.port}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def feedback_rows(self):
        from feedcast import db
        conn = db.connect()
        rows = conn.execute("SELECT post_key, verdict FROM feedback").fetchall()
        conn.close()
        return [tuple(r) for r in rows]

    def test_rejects_bad_origin_and_verdict_accepts_good(self):
        self.assertEqual(self.post({"key": "urn:li:activity:1", "verdict": "more_like_this"}, "https://evil.example"), 403)
        self.assertEqual(self.post({"key": "urn:li:activity:1", "verdict": "love_it"}), 400)
        self.assertEqual(self.post({"key": "urn:li:activity:404", "verdict": "not_relevant"}), 400)
        self.assertEqual(self.feedback_rows(), [])
        self.assertEqual(self.post({"key": "urn:li:activity:1", "verdict": "not_relevant"}), 200)
        self.assertEqual(self.feedback_rows(), [("urn:li:activity:1", "not_relevant")])

    def test_serves_reports_and_checks_host(self):
        from feedcast import feedback
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/reports/2026-10-30.html", timeout=5) as r:
            self.assertEqual(r.read(), b"<p>page</p>")
        self.assertTrue(feedback.server_up(self.port))
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/reports/2026-10-30.html", headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/reports/../feedcast.db", timeout=5)
        self.assertEqual(e.exception.code, 404)

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, None, b""

    def api(self, path, body, token=None, origin=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/{path}", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Origin": origin or f"http://127.0.0.1:{self.port}",
                     "X-Feedcast-Token": self.srv.token if token is None else token})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_setup_page_and_actions(self):
        import subprocess
        from datetime import datetime, timezone
        from feedcast import capture, config, profile, schedule, setup

        class Proc:
            code = None

            def poll(self):
                return self.code
        spawned, proc = [], Proc()
        setup.SPAWN = lambda name, shell: spawned.append((name, shell)) or proc
        setup._job.update(name=None, proc=None, last=None)
        capture.RUNNER = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")  # launchctl never runs
        os.environ["FEEDCAST_LAUNCHD_DIR"] = str(Path(self.tmp.name) / "LaunchAgents")
        self.addCleanup(lambda: (setattr(setup, "SPAWN", setup._default_spawn), setattr(capture, "RUNNER", capture._default_runner)))
        work = Path(self.tmp.name) / "work"
        work.mkdir()

        code, ctype, page = self.get("/setup")
        self.assertEqual((code, ctype), (200, "text/html; charset=utf-8"))
        self.assertIn(f'<meta name="feedcast-token" content="{self.srv.token}">', page.decode())
        self.assertIn("Daily reads off", page.decode())
        # same origin and this server's token, or nothing changes
        for kw in ({"token": ""}, {"token": "guess"}, {"origin": "https://evil.example"}):
            self.assertEqual(self.api("roots", {"add": str(work)}, **kw)[0], 403)
        self.assertEqual(config.load()["roots"], [])

        self.assertEqual(self.api("roots", {"add": "/does/not/exist"})[0], 400)
        self.assertEqual(self.api("roots", {"add": str(work)})[0], 200)
        self.api("roots", {"add": str(work)})
        self.assertEqual(config.load()["roots"], [str(work)])
        self.assertIn(f"{work}<button", self.get("/setup")[2].decode())
        self.api("roots", {"remove": str(work)})
        self.api("sessions", {"on": False})
        self.assertEqual((config.load()["roots"], config.load()["use_sessions"]), ([], False))

        profile.save({"role": "model role", "interests": [{"topic": "agent evals", "weight": 0.9, "auto": True}],
                      "not_interested": ["crypto"]})
        for body in ({"role": "AI engineer: voice"}, {"add_interest": "browser agents"}, {"remove_interest": "Agent  Evals"},
                     {"add_never": "hiring takes"}, {"remove_never": "crypto"}):
            self.assertEqual(self.api("profile", body)[0], 200, body)
        prof = profile.load()
        self.assertEqual(prof["interests"], [{"topic": "browser agents", "weight": 0.8}])  # hand-added: no auto
        self.assertEqual(prof["not_interested"], ["hiring takes"])
        later = profile.merge(prof, {"role": "a model's guess", "interests": [{"topic": "evals", "weight": 0.5}]}, {}, {},
                              datetime.now(timezone.utc))
        self.assertEqual(later["role"], "AI engineer: voice")  # typed here, so a refresh keeps it
        self.assertIn({"topic": "browser agents", "weight": 0.8}, later["interests"])
        self.assertIn("Role: AI engineer: voice", self.get("/setup")[2].decode())  # what Claude sees

        self.assertEqual(self.api("schedule", {"captures": ["25:00"]})[0], 400)
        self.assertEqual(self.api("schedule", {"captures": ["12:00", "08:15"], "digest": "20:00", "threshold": 3})[0], 200)
        cfg = config.load()
        self.assertEqual((cfg["schedule"]["captures"], cfg["schedule"]["digest"], cfg["relevance_threshold"]),
                         (["08:15", "12:00"], "20:00", 3))
        self.api("schedule/on", {})
        self.assertEqual(len(schedule.installed()), 4)
        self.assertIn("Daily reads on · next read", self.get("/setup")[2].decode())
        self.api("schedule/off", {})
        self.assertEqual(schedule.installed(), [])

        self.assertEqual(self.api("jobs", {"job": "rm -rf"})[0], 400)
        self.assertEqual(self.api("jobs", {"job": "read"})[1]["running"], "read")
        self.assertEqual(self.api("jobs", {"job": "profile"})[0], 409)  # one at a time
        self.assertRegex(spawned[0][1], r"capture --now && .* ingest && .* report --no-open$")
        self.assertIn("Reading…", self.get("/setup")[2].decode())
        proc.code = 0
        self.assertEqual(json.loads(self.get("/api/jobs")[2]), {"running": None, "last": {"name": "read", "code": 0}})

        config.paused_path().write_text("aborted_challenge at 09:00")
        self.assertIn('data-act="resume"', self.get("/setup")[2].decode())
        self.assertEqual(self.api("jobs", {"job": "read"})[0], 409)
        self.api("resume", {})
        self.assertFalse(config.paused_path().exists())
        self.assertEqual(len(spawned), 1)

    def test_serves_downloaded_media_only(self):
        from feedcast import media
        media.media_dir().mkdir(parents=True)
        name = "a" * 40 + ".jpg"
        (media.media_dir() / name).write_bytes(b"JPEG")
        (media.media_dir() / ("b" * 40 + ".svg")).write_bytes(b"<svg/>")
        self.assertEqual(self.get(f"/media/{name}"), (200, "image/jpeg", b"JPEG"))
        for bad in ("/media/" + "b" * 40 + ".svg", "/media/../feedcast.db", "/media/" + "c" * 40 + ".png"):
            self.assertEqual(self.get(bad)[0], 404, bad)


def write_sample() -> None:
    """Regenerate fixtures/report/sample-report.html from the invented fixture: `uv run python tests/test_report.py sample`."""
    from feedcast import llm, report
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["FEEDCAST_HOME"] = tmp
        from feedcast import db
        conn = db.connect()
        seed(conn)
        llm.set_fake(fake_digest()[0])
        out = report.generate(DAY, conn=conn, do_notify=False, do_open=False, sample=True)
        (FIX / "sample-report.html").write_text(out.read_text())
        conn.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["sample"]:
        sys.path.insert(0, str(ROOT))
        write_sample()
    else:
        unittest.main()
