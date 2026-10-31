import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FEEDCAST_HOME"] = self.tmp.name

    def tearDown(self):
        from feedcast import llm
        llm.set_fake(None)
        self.tmp.cleanup()

    def test_sample_capture_validates(self):
        from feedcast.models import validate_capture
        clean, errors = validate_capture(json.loads((ROOT / "fixtures/capture-sample.json").read_text()))
        self.assertEqual(clean["status"], "ok")
        self.assertEqual(len(clean["posts"]), 12)
        self.assertEqual(errors, [])

    def test_bad_posts_dropped_ads_flagged_and_run_degraded(self):
        from feedcast.models import validate_capture
        clean, errors = validate_capture({"status": "ok", "posts": [{"author": "x"}, {"author": "a", "text": "b", "reactions": "1.2K"}]})
        self.assertEqual(clean["status"], "degraded")
        self.assertEqual(clean["posts"][0]["reactions"], 1200)
        self.assertTrue(errors)

        # Ads the capture didn't flag, kept conservative; a urn recovered from the permalink.
        base = {"author": "A", "text": "Shipped a thing.", "posted_age": "3h"}
        posts = [
            {**base, "author_headline": "Promoted"},                                   # 0 ad: headline
            {**base, "text": "Sponsored\nScale your agents."},                         # 1 ad: own line
            {**base, "posted_age": "", "links": [{"url": "https://x.example/w", "title": "Download now"}]},  # 2 ad: CTA, no age
            {**base, "links": [{"url": "https://x.example/w", "title": "Download now"}]},  # 3 organic: has an age
            {**base, "text": "I got promoted to staff engineer. Sponsored by nobody but coffee."},  # 4 organic
            {**base, "url": "https://www.linkedin.com/feed/update/urn:li:activity:7251000000000000099/"},  # 5
            {**base, "links": [{"url": "https://x.example/g", "title": "Startup guide to AI agents. Download now."}]},  # 6 ad: CTA ends the title
            {**base, "links": [{"url": "https://x.example/r", "title": "We open-sourced our eval harness. Learn more about how it works."}]},  # 7 organic
        ]
        clean, _ = validate_capture({"status": "degraded", "posts": posts})
        self.assertEqual([p["is_promoted"] for p in clean["posts"]], [True, True, True, False, False, False, True, False])
        self.assertEqual(clean["posts"][5]["urn"], "urn:li:activity:7251000000000000099")

        # The author's page: profile or company only, query dropped.
        from feedcast.models import author_url
        self.assertEqual(author_url("https://www.linkedin.com/in/jane-doe-example?trk=feed"), "https://www.linkedin.com/in/jane-doe-example/")
        self.assertEqual(author_url("/company/acme-example/posts/"), "https://www.linkedin.com/company/acme-example/")
        for bad in ("https://evil.example/in/x", "https://www.linkedin.com/feed/", "javascript:alert(1)", None, 3):
            self.assertIsNone(author_url(bad))

    def test_db_and_fake_llm(self):
        from feedcast import config, db, llm
        conn = db.connect()
        self.assertTrue(str(config.db_path()).startswith(self.tmp.name))
        self.assertEqual(db.post_key(None, "a", "b")[:2], "h:")
        # Live reads cut the text at different places past the fold: same first 120 characters, same key.
        head = "We rebuilt our eval harness around recorded sessions and it caught three regressions before they shipped to anyone in production"
        self.assertEqual(db.post_key(None, "Ana", head + " …more"),
                         db.post_key(None, "Ana", "  " + head.upper().replace(" ", "\n ", 3) + " and here is the full story, see more"))
        self.assertEqual(db.post_key(None, "Ana", "Short post…"), db.post_key(None, "Ana", "short post ...see more"))
        self.assertNotEqual(db.post_key(None, "Ana", head), db.post_key(None, "Bo", head))
        self.assertEqual(db.post_key("urn:li:activity:1", "Ana", head), "urn:li:activity:1")
        llm.set_fake(lambda *a: 'Sure: {"ok": true}')
        self.assertEqual(llm.complete_json("relevance", "m", "s", "u"), {"ok": True})
        self.assertEqual(conn.execute("select count(*) from llm_calls").fetchone()[0], 0)
        conn.close()

        # A DB from before topics gains the column once.
        config.db_path().unlink()
        old = sqlite3.connect(config.db_path())
        old.execute("CREATE TABLE posts (key TEXT PRIMARY KEY, project TEXT)")
        old.execute("INSERT INTO posts VALUES ('k', 'nova')")
        old.commit()
        old.close()
        for _ in range(2):
            conn = db.connect()
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(posts)")]
            conn.close()
        self.assertEqual(cols, ["key", "project", "topic", "author_url", "image", "author_avatar"])

        # A post stored under the pre-fold key keeps it: the next read of it is a sighting, not a new post.
        from feedcast import ingest
        conn = db.connect()
        post = {"urn": None, "author": "Ana", "text": "Old post text"}
        conn.execute("INSERT INTO posts (key) VALUES (?)", (db.legacy_key("Ana", "Old post text"),))
        self.assertEqual(ingest._key(conn, post), db.legacy_key("Ana", "Old post text"))
        self.assertEqual(ingest._key(conn, {**post, "text": "Another post"}), db.post_key(None, "Ana", "Another post"))
        conn.close()

    def test_config_defaults_have_no_roots(self):
        from feedcast import config
        self.assertEqual(config.load()["roots"], [])


if __name__ == "__main__":
    unittest.main()
