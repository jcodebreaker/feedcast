import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures" / "profile"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
SECRET = "SECRET-SOURCE-TOKEN-8f3a"


def _git(repo: Path, *args, when: datetime | None = None):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when.isoformat()
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def make_repo(path: Path, files: dict, commits: list[tuple[str, int]]):
    """Invented repo: `commits` is [(message, days_before_NOW)]."""
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    for name, body in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(body)
    for i, (msg, days) in enumerate(commits):
        (path / "log.txt").write_text(f"{i} {SECRET}\n")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", msg, when=NOW - timedelta(days=days))


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        os.environ["FEEDCAST_HOME"] = str(self.home)
        sessions = self.tmp / "sessions"
        shutil.copytree(FIX / "sessions", sessions)
        for p in sessions.glob("*/*.jsonl"):
            age = 20 if "old" in p.name else 1
            t = (NOW - timedelta(days=age)).timestamp()
            os.utime(p, (t, t))
        os.environ["FEEDCAST_CLAUDE_PROJECTS"] = str(sessions)

        code = self.tmp / "code"
        make_repo(code / "nova", {
            "README.md": "[![ci](x)](y)\n# Nova\n\nAn agent harness\nthat runs in Docker.\n\nMore text later.\n",
            "package.json": '{"dependencies": {"@anthropic-ai/sdk": "1"}, "devDependencies": {"vitest": "2"}}',
            "src/secret.ts": f'export const key = "{SECRET}";\n',
        }, [("Start nova", 6), (f"Add tool-call eval gate\n\nCOMMIT-BODY-NEVER-READ {SECRET}", 2)])
        make_repo(code / "group" / "deckforge", {
            "pyproject.toml": '[project]\nname = "deckforge"\ndependencies = ["python-pptx>=1.0", "pyyaml"]\n',
            "deckforge/main.py": f"KEY = '{SECRET}'\n",
        }, [("Fit titles before render", 5)])
        make_repo(code / "oldproj", {"go.mod": "module x\nrequire github.com/old/dep v1\n"}, [("Old work", 20)])
        make_repo(code / "node_modules" / "vendored", {}, [("Vendored", 1)])
        make_repo(code / ".hidden" / "secretrepo", {}, [("Hidden", 1)])
        make_repo(code / "a" / "b" / "c" / "toodeep", {}, [("Deep", 1)])
        self.code = code

        from feedcast import config
        config.save({"roots": [str(code)], "use_sessions": True, "pins": ["github.com/example/pinned"], "mutes": ["crypto"]})

    def tearDown(self):
        from feedcast import llm
        llm.set_fake(None)
        os.environ.pop("FEEDCAST_CLAUDE_PROJECTS", None)
        shutil.rmtree(self.tmp)

    def run_cli(self, *argv):
        from feedcast import cli
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("feedcast.profile._now", return_value=NOW), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["profile", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_signals_window_depth_and_no_source_read(self):
        from feedcast import config, profile
        signals = profile.gather(config.load(), NOW)
        self.assertEqual([r["name"] for r in signals["repos"]], ["nova", "deckforge"])
        nova = signals["repos"][0]
        self.assertEqual(nova["readme"], "Nova — An agent harness that runs in Docker.")
        self.assertEqual(nova["deps"], ["@anthropic-ai/sdk", "vitest"])
        self.assertEqual(nova["commits"], ["Add tool-call eval gate", "Start nova"])
        self.assertEqual(signals["repos"][1]["deps"], ["python-pptx", "pyyaml"])

        sessions = {s["title"] or s["prompt"]: s for s in signals["sessions"]}
        self.assertEqual(len(sessions), 3)
        evals = sessions["Tool-call accuracy evals"]
        self.assertEqual(evals["project"], "-Users-dev-code-nova")
        self.assertTrue(evals["prompt"].startswith("Add a regression eval"))
        self.assertLessEqual(len(evals["prompt"]), 200)
        self.assertEqual(sessions["Fit text boxes before pptx render"]["prompt"],
                         "Make the layout check fail on overflowing text in slide titles")

        text = profile.signals_text(signals)
        for never in (SECRET, "COMMIT-BODY", "TRANSCRIPT-BODY", "SECOND-PROMPT", "OLD-SESSION", "oldproj",
                      "vendored", "secretrepo", "toodeep", "/clear"):
            self.assertNotIn(never, text)

    def test_dry_run_prints_what_refresh_sends_without_calling(self):
        from feedcast import llm
        llm.set_fake(lambda *a: self.fail("dry run called the model"))
        code, dry, _ = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("## repo nova", dry)
        self.assertFalse((self.home / "profile.yaml").exists())

        sent = []
        llm.set_fake(lambda purpose, model, system, user: sent.append((purpose, model, user)) or (FIX / "reply.json").read_text())
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(sent, [("profile", "claude-haiku-5-5", dry.rstrip("\n"))])
        self.assertEqual(out.splitlines()[1:], [
            "Role: AI engineer: agents and evals; TypeScript, Python",
            "Interests: pptx layout, agent evals and regression tests, MY HAND TOPIC",
            "Read 2 repos and 3 sessions.",
            "Run feedcast profile --linkedin once to add your LinkedIn profile.",
        ])

    def test_hand_edits_survive_quiet_interests_decay_and_linkedin_seeds(self):
        from feedcast import config, llm, profile
        profile.save({
            "generated_at": "2026-10-27T12:00:00Z",
            "active_projects": [{"name": "Deckforge", "about": "v1 field, dropped"}],
            "stack": ["Go"],
            "interests": [
                {"topic": "my hand topic", "weight": 0.6},
                {"topic": "go modules", "weight": 0.5, "auto": True},
                {"topic": "old faded", "weight": 0.12, "auto": True},
                {"topic": "agent evals and regression tests", "weight": 0.4, "auto": True},
            ],
            "not_interested": ["X is dead posts"],
            "pins": ["hand pin"],
            "mutes": [],
        })
        li = {"headline": "Builds voice agents", "about": "I work on agents.", "experience": ["AI engineer at Example Co"],
              "skills": ["Python", "MCP"], "read_at": "2026-10-20T09:00:00Z"}
        config.linkedin_self_path().write_text(json.dumps(li))
        sent = []
        llm.set_fake(lambda p_, m, s, user: sent.append(user) or (FIX / "reply.json").read_text())
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertNotIn("--linkedin", out)
        self.assertIn("## Their LinkedIn profile\nHeadline: Builds voice agents", sent[0])
        self.assertIn("Skills: Python, MCP", sent[0])
        p = yaml.safe_load(config.profile_path().read_text())

        self.assertEqual(list(p), ["generated_at", "role", "stack", "interests", "not_interested", "pins", "mutes", "linkedin"])
        self.assertEqual(p["generated_at"], "2026-10-30T12:00:00Z")
        self.assertEqual(p["role"], "AI engineer: agents and evals; TypeScript, Python")
        self.assertEqual(p["interests"], [
            {"topic": "my hand topic", "weight": 0.6},
            {"topic": "pptx layout", "weight": 1.0, "auto": True},
            {"topic": "agent evals and regression tests", "weight": 0.9, "auto": True},
            # Dropped by the model three days after the last refresh: 0.5 * 0.9^3; "old faded" fell under 0.1.
            {"topic": "go modules", "weight": 0.36, "auto": True},
        ])
        self.assertEqual(p["stack"], ["Python", "TypeScript"])
        self.assertEqual(p["not_interested"], ["X is dead posts", "hiring takes"])
        self.assertEqual(p["pins"], ["github.com/example/pinned", "hand pin"])
        self.assertEqual(p["mutes"], ["crypto"])
        self.assertEqual(p["linkedin"], li)
        text = profile.compact_text(p)
        self.assertIn("Role: AI engineer: agents and evals", text)
        self.assertIn("LinkedIn headline: Builds voice agents", text)
        for name in ("nova", "deckforge", "Deckforge", "MODEL-PROJECT"):
            self.assertNotIn(name, text)

        # Older than 30 days: not used, and the hint comes back.
        li["read_at"] = "2026-09-01T09:00:00Z"
        config.linkedin_self_path().write_text(json.dumps(li))
        self.assertIsNone(profile.read_linkedin(NOW))

    def test_compact_text_is_capped_and_keeps_pins_and_mutes(self):
        from feedcast import profile
        big = {
            "role": "AI engineer",
            "stack": ["Python"] * 30,
            "interests": [{"topic": f"topic {i} " + "t" * 70, "weight": i / 100} for i in range(100)],
            "not_interested": ["n"],
            "pins": ["github.com/example/pinned"],
            "mutes": ["crypto"],
        }
        text = profile.compact_text(big)
        self.assertLessEqual(len(text), 2500)
        self.assertIn("Pins: github.com/example/pinned", text)
        self.assertIn("Mutes: crypto", text)
        self.assertTrue(text.startswith("Role: AI engineer\nStack: Python"))
        self.assertIn("topic 99", text)
        self.assertNotIn("topic 0 ", text)

        profile.save(big)
        code, out, _ = self.run_cli("--show")
        self.assertEqual((code, out.strip()), (0, text))

    def test_empty_roots(self):
        from feedcast import config, llm
        config.save({"roots": []})
        llm.set_fake(lambda *a: self.fail("called the model"))
        code, out, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "No work folders set. Add them to ~/.feedcast/config.yaml under roots, or run feedcast init.")
        self.assertEqual(self.run_cli("--dry-run")[0], 1)


if __name__ == "__main__":
    unittest.main()
