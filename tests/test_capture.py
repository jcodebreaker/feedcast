import getpass
import io
import json
import os
import plistlib
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from feedcast import capture, cli, config, schedule

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = json.loads((ROOT / "fixtures/capture-sample.json").read_text())


class FakeRunner:
    """Answers by program name; records every call. Never runs anything."""

    def __init__(self, chrome=True, batt="Now drawing from 'AC Power'\n -InternalBattery-0\t80%; charged;",
                 agent_writes=None, keychain=True):
        self.calls = []
        self.inputs = []
        self.chrome, self.batt, self.agent_writes, self.keychain = chrome, batt, agent_writes, keychain

    def __call__(self, cmd, input=None, timeout=None, cwd=None):
        self.calls.append(cmd)
        self.inputs.append(input)
        prog, ok = cmd[0], 0
        out = ""
        if prog == "pgrep":
            ok = 0 if self.chrome else 1
        elif prog == "pmset":
            out = self.batt
        elif prog == "claude" and "-p" in cmd:
            rule = next(t for t in cmd[cmd.index("--allowedTools") + 1].split(",") if t.startswith("Edit("))
            if self.agent_writes is not None:
                Path(rule[len("Edit(/"):-1]).write_text(json.dumps(self.agent_writes))
        elif prog == "claude":
            out = "  --chrome   Enable Claude in Chrome integration"
        elif prog == "security" and cmd[1] == "find-generic-password":
            ok = 0 if self.keychain else 44
        elif prog == "gh":
            ok, out = 0, "gho_FAKE_TOKEN_NEVER_PRINTED"
        return subprocess.CompletedProcess(cmd, ok, out, "")

    def ran(self, prog):
        return [c for c in self.calls if c[0] == prog]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        os.environ["FEEDCAST_HOME"] = str(self.home)
        os.environ["FEEDCAST_LAUNCHD_DIR"] = str(Path(self.tmp.name) / "LaunchAgents")
        self.runner = FakeRunner(agent_writes=SAMPLE)
        self.sleeps = []
        capture.RUNNER, capture.SLEEP = self.runner, self.sleeps.append

    def tearDown(self):
        capture.RUNNER, capture.SLEEP = capture._default_runner, time.sleep
        capture.INPUT, capture.GETPASS = input, getpass.getpass
        self.tmp.cleanup()

    def feedcast(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def runs(self):
        return sorted(config.runs_dir().glob("*/*.json"))


class CaptureTest(Base):
    def test_agent_command_is_locked_down(self):
        out = Path("/x/runs/.incoming/a.json")
        cmd = capture.agent_command(capture.render_prompt(50, out), out)
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertNotIn("bypassPermissions", cmd)
        self.assertEqual(cmd[:3], ["claude", "-p", "--chrome"])
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Write")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
        self.assertIn("Edit(//x/runs/.incoming/a.json)", allowed)
        self.assertTrue(all(t.startswith("mcp__claude-in-chrome__") or t.startswith("Edit(") for t in allowed))
        self.assertNotIn("mcp__claude-in-chrome__javascript_tool", allowed)
        self.assertNotIn("mcp__claude-in-chrome__form_input", allowed)
        prompt = cmd[-1]
        self.assertIn("/x/runs/.incoming/a.json", prompt)
        self.assertIn("read 50 posts", prompt)
        self.assertIn("/feed/update/urn:li:activity:", prompt)  # where permalinks come from
        self.assertIn("mcp__claude-in-chrome__read_page", allowed)
        self.assertNotIn("{max_posts}", prompt)

    def test_live_capture_writes_validated_run(self):
        code, out = self.feedcast("capture", "--now")
        self.assertEqual(code, 0)
        [run] = self.runs()
        data = json.loads(run.read_text())
        self.assertEqual((data["status"], len(data["posts"])), ("ok", 12))
        self.assertNotIn("_note", data)
        self.assertEqual(len(self.runner.ran("claude")), 1)
        self.assertEqual(self.sleeps, [])
        self.assertFalse(list(config.path("runs", ".incoming").iterdir()))

    def test_scheduled_waits_a_random_while(self):
        self.feedcast("capture", "--scheduled")
        self.assertEqual(len(self.sleeps), 1)
        self.assertTrue(0 <= self.sleeps[0] <= 480)

    def test_paused_refuses_without_touching_anything(self):
        config.paused_path().parent.mkdir(parents=True)
        config.paused_path().write_text("aborted_challenge at 09:00")
        code, out = self.feedcast("capture", "--now")
        self.assertEqual(code, 2)
        self.assertIn("aborted_challenge at 09:00", out)
        self.assertEqual(self.runner.calls, [])

    def test_no_chrome_is_noted_and_agent_not_run(self):
        self.runner.chrome = False
        code, _ = self.feedcast("capture", "--now")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(self.runs()[0].read_text())["status"], "aborted_no_chrome")
        self.assertEqual(self.runner.ran("claude"), [])

    def test_low_battery_skips(self):
        self.runner.batt = "Now drawing from 'Battery Power'\n -InternalBattery-0\t12%; discharging;"
        code, out = self.feedcast("capture", "--now")
        self.assertEqual((code, self.runs()), (0, []))
        self.assertEqual(self.runner.ran("claude"), [])

    def test_challenge_pauses_capture_and_own_profile_read(self):
        self.runner.agent_writes = json.loads((ROOT / "fixtures/capture/challenge.json").read_text())
        code, out = self.feedcast("capture", "--now")
        self.assertEqual(code, 1)
        self.assertIn("aborted_challenge", config.paused_path().read_text())
        [note] = self.runner.ran("osascript")
        self.assertIn(capture.PAUSED_NOTE, note[-1])
        # next run refuses; resume clears it
        self.assertEqual(self.feedcast("capture", "--now")[0], 2)
        code, out = self.feedcast("resume")
        self.assertIn("Removed PAUSED", out)
        self.assertFalse(config.paused_path().exists())

        # `profile --linkedin`: same agent, same lock-down and abort rule, one output file.
        self.assertEqual(self.feedcast("profile", "--linkedin")[0], 1)  # the challenge again
        self.assertIn("own profile", config.paused_path().read_text())
        self.assertEqual(self.feedcast("profile", "--linkedin")[0], 2)  # paused: agent not run
        self.feedcast("resume")
        self.runner.agent_writes = {"status": "ok", "headline": " Builds  voice agents ", "about": "x" * 3000,
                                    "experience": [{"title": "AI engineer", "company": "Example Co"}, "Intern at Lab"],
                                    "skills": ["Python", "Python", "MCP"]}
        code, out = self.feedcast("profile", "--linkedin")
        self.assertEqual(code, 0)
        self.assertIn("2 roles, 2 skills", out)
        cmd = self.runner.ran("claude")[-1]
        allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
        [rule] = [t for t in allowed if t.startswith("Edit(")]
        self.assertTrue(rule.startswith("Edit(//") and rule.endswith(".json)"))
        self.assertNotIn("mcp__claude-in-chrome__javascript_tool", allowed)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("https://www.linkedin.com/in/me/", cmd[-1])
        self.assertIn(rule[len("Edit(/"):-1], cmd[-1])
        me = json.loads(config.linkedin_self_path().read_text())
        self.assertEqual((me["headline"], len(me["about"])), ("Builds voice agents", 2000))
        self.assertEqual((me["experience"], me["skills"]), (["AI engineer at Example Co", "Intern at Lab"], ["Python", "MCP"]))
        self.assertTrue(me["read_at"].endswith("Z"))

    def test_agent_wrote_nothing_is_degraded(self):
        self.runner.agent_writes = None
        self.feedcast("capture", "--now")
        data = json.loads(self.runs()[0].read_text())
        self.assertEqual((data["status"], data["posts"]), ("degraded", []))

    def test_from_file_imports_and_cleans(self):
        code, out = self.feedcast("capture", "--from-file", str(ROOT / "fixtures/capture/messy.json"))
        self.assertEqual(code, 0)
        [run] = self.runs()
        self.assertEqual(run.relative_to(config.runs_dir()).as_posix(), "2026-10-30/1845.json")
        data = json.loads(run.read_text())
        self.assertEqual(data["status"], "degraded")  # 2 valid posts < 10
        self.assertEqual(data["posts"][0]["reactions"], 1400)
        self.assertEqual([l["url"] for l in data["posts"][0]["links"]], ["https://github.com/example-org/trace-viewer"])
        self.assertIsNone(data["posts"][1]["urn"])
        self.assertEqual(self.runner.calls, [])  # nothing live


class ScheduleTest(Base):
    def test_install_writes_plists_and_bootstraps(self):
        self.feedcast("schedule", "install")
        d = Path(os.environ["FEEDCAST_LAUNCHD_DIR"])
        names = sorted(p.stem for p in d.iterdir())
        self.assertEqual(names, ["com.feedcast.capture-0900", "com.feedcast.capture-1330", "com.feedcast.capture-1845",
                                 "com.feedcast.profile", "com.feedcast.report"])
        pl = plistlib.loads((d / "com.feedcast.capture-1330.plist").read_bytes())
        self.assertEqual(pl["StartCalendarInterval"], {"Hour": 13, "Minute": 30})
        shell = pl["ProgramArguments"][-1]
        self.assertRegex(shell, r"capture --scheduled && .*ingest$")
        self.assertEqual(pl["StandardOutPath"], str(self.home / "logs" / "capture-1330.log"))
        self.assertEqual(pl["EnvironmentVariables"]["FEEDCAST_HOME"], str(self.home))
        self.assertEqual(len([c for c in self.runner.ran("launchctl") if c[1] == "bootstrap"]), 5)

    def test_changed_times_drop_old_jobs_and_remove_clears_all(self):
        self.feedcast("schedule", "install")
        cfg = config.load()
        cfg["schedule"]["captures"] = ["10:15"]
        config.save(cfg)
        self.feedcast("schedule", "install")
        self.assertEqual(len(schedule.installed()), 3)
        code, out = self.feedcast("schedule", "remove")
        self.assertEqual(schedule.installed(), [])
        self.assertIn("Removed com.feedcast.report", out)


class DoctorTest(Base):
    def test_all_green(self):
        root = Path(self.tmp.name) / "work"
        root.mkdir()
        config.save({**config.load(), "roots": [str(root)]})
        config.profile_path().write_text(f'generated_at: "{datetime.now(timezone.utc).isoformat()}"\n')
        self.feedcast("capture", "--now")
        self.feedcast("schedule", "install")
        code, out = self.feedcast("doctor")
        self.assertEqual(code, 0, out)
        self.assertNotIn("✗", out)
        self.assertNotIn("gho_FAKE", out)

    def test_problems_shown(self):
        self.runner.chrome = False
        self.runner.keychain = False
        os.environ.pop("ANTHROPIC_API_KEY", None)
        config.paused_path().parent.mkdir(parents=True)
        config.paused_path().write_text("aborted_not_signed_in")
        code, out = self.feedcast("doctor")
        self.assertEqual(code, 1)
        for line in ["✗ work folders", "✗ profile", "✗ API key", "✗ Chrome", "✗ captures: paused", "✗ last run: none yet",
                     "✗ schedule", "✓ claude", "✓ gh token"]:
            self.assertIn(line, out)


class InitTest(Base):
    def test_init_flow(self):
        root = Path(self.tmp.name) / "work"
        root.mkdir()
        answers = iter(["/does/not/exist", f"{root}", "n", "y", "n"])
        capture.INPUT = lambda prompt="": next(answers)
        capture.GETPASS = lambda prompt="": "sk-ant-FAKE_test-key"
        code, out = self.feedcast("init", "--terminal")
        self.assertEqual(code, 0)
        self.assertIn("Not found: /does/not/exist", out)
        cfg = config.load()
        self.assertEqual((cfg["roots"], cfg["use_sessions"]), ([str(root)], False))
        [sec] = [c for c in self.runner.ran("security") if c[1] == "-i"]
        self.assertTrue(all("FAKE" not in a for a in sec))  # key stays out of argv
        self.assertIn("-s feedcast -a anthropic -w", self.runner.inputs[self.runner.calls.index(sec)])
        self.assertEqual(len(schedule.installed()), 5)
        self.assertIn("✓ work folders", out)
        self.assertNotIn("FAKE", out)
        self.assertEqual(self.runner.ran("claude")[1:], [])  # only doctor's --help, no first read

    def test_init_runs_supervised_first_read_on_yes(self):
        root = Path(self.tmp.name) / "work"
        root.mkdir()
        answers = iter([str(root), "", "n", "y"])
        capture.INPUT = lambda prompt="": next(answers)
        capture.GETPASS = lambda prompt="": ""
        code, out = self.feedcast("init", "--terminal")
        self.assertEqual(code, 0)
        self.assertEqual(self.runner.ran("security")[0][1], "find-generic-password")  # no key stored
        self.assertEqual(len([c for c in self.runner.ran("claude") if "-p" in c]), 1)
        self.assertEqual(len(self.runs()), 1)

    def test_init_and_setup_open_the_setup_page(self):
        from unittest import mock
        from feedcast import feedback
        with mock.patch.object(feedback, "ensure_server", return_value=True) as up:
            for cmd in ("init", "setup"):
                code, out = self.feedcast(cmd)
                self.assertEqual((code, out.strip()), (0, "http://127.0.0.1:8765/setup"))
        self.assertEqual(up.call_args.args, (8765,))
        self.assertEqual(self.runner.ran("open"), [["open", "http://127.0.0.1:8765/setup"]] * 2)
        self.assertEqual(self.runner.ran("claude"), [])  # nothing read, nothing asked


if __name__ == "__main__":
    unittest.main()
