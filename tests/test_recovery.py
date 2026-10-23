import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from llm_archive import db, recovery, scheduler, ghostty_config
from llm_archive.cli import cli
from llm_archive.paths import device_id, read_config


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {
            "LLM_ARCHIVE_HOME": str(self.root / "data"), "CODEX_HOME": str(self.root / "codex"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude")}))
        self.stack.enter_context(patch.object(db, "DB_DIR", self.root / "data"))
        self.stack.enter_context(patch.object(db, "DB_PATH", self.root / "data/archive.db"))
        self.cwd = self.root / "project with spaces"
        self.cwd.mkdir()
        self.transcript = self.root / "session.jsonl"
        self.transcript.write_text("{}\n")
        self.shell = recovery._shell(str(self.cwd))

    def session(self, sid="session-one", source="codex", title="First task"):
        return {**self.shell, "source": source, "session_id": sid,
                "source_path": str(self.transcript), "agent_title": title}

    def terminal(self, tid="surface-one", title="First task | project"):
        return {"id": tid, "title": title, "cwd": str(self.cwd)}

    def watcher(self, process="process-one"):
        recovery.write_state({"login": "login-one", "handled_process": process})
        return recovery.Watcher("login-one")

    def settle(self, watcher, process, terminals, active, start=0):
        watcher.tick(process, terminals, active, start)
        watcher.tick(process, terminals, active, start + 20)

    def test_multiple_agents_in_one_project_keep_their_exact_sessions(self):
        active = [self.session(), self.session("session-two", title="Second task"),
                  self.session("session-three", source="claude", title="")]
        terminals = [self.terminal(), self.terminal("surface-two", "⠇ Second task | project"),
                     self.terminal("surface-three", "✳ Claude task")]
        sessions = recovery.workspace_sessions(terminals, active, [])
        self.assertEqual([s["session_id"] for s in sessions], ["session-one", "session-two", "session-three"])
        self.assertEqual(len(recovery.restoration_plan(sessions, [], [])), 3)
        self.assertEqual(recovery.restoration_plan(sessions, terminals, active), [])
        self.assertEqual(sessions[1]["terminal_title"], "Second task | project")

    def test_ambiguous_tabs_remain_shells_instead_of_picking_newest_agent(self):
        active = [self.session(), self.session("session-two", title="Second task")]
        terminals = [self.terminal(title="zsh"), self.terminal("two", "zsh")]
        sessions = recovery.workspace_sessions(terminals, active, [])
        self.assertEqual([s["source"] for s in sessions], ["", ""])
        previous = [{**active[0], "terminal_id": "surface-one"}]
        sessions = recovery.workspace_sessions(terminals, active, previous)
        self.assertEqual([s["session_id"] for s in sessions], ["session-one", "session-two"])
        # Exiting an agent changes that surface back to a shell.
        self.assertEqual(recovery.workspace_sessions(terminals, [], sessions)[0]["source"], "")

    def test_resume_does_not_type_into_an_existing_shell_or_replay_transcript(self):
        saved = [self.session(), {**self.shell, "terminal_id": "shell"}]
        plan = recovery.restoration_plan(saved, [self.terminal(title="zsh")], [])
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["command"], "codex resume session-one")
        self.assertTrue(plan[0]["new_tab"])
        foreign = {**self.session(), "origin_device": "another-device"}
        self.assertEqual(recovery.restoration_plan([foreign], [], []), [])
        self.transcript.unlink()
        self.assertEqual(recovery.restoration_plan([self.session()], [], [])[0]["command"], "")

    def test_quit_preserves_workspace_but_deliberate_tab_closure_updates_it(self):
        w = self.watcher()
        terminals = [self.terminal(), self.terminal("two", "Second task")]
        active = [self.session(), self.session("two", title="Second task")]
        self.settle(w, "process-one", terminals, active)
        self.assertEqual(len(recovery.saved_workspace()["sessions"]), 2)
        w.tick(None, [], [], 30)
        self.assertEqual(len(recovery.saved_workspace()["sessions"]), 2)
        self.settle(w, "process-one", terminals[:1], active[:1], 40)
        self.assertEqual(len(recovery.saved_workspace()["sessions"]), 1)
        # Closing all tabs while leaving Ghostty running is intentional too.
        self.settle(w, "process-one", [], [], 70)
        self.assertEqual(recovery.saved_workspace()["sessions"], [])

    def test_relaunch_recovers_once_and_watcher_restart_does_not_duplicate_tabs(self):
        recovery.save_workspace([self.session()])
        w = self.watcher()
        with patch.object(recovery, "open_ghostty", return_value=1) as opened:
            self.settle(w, "process-two", [], [])
            opened.assert_called_once()
            w.tick("process-two", [], [], 30)
            resumed = self.session()
            w.tick("process-two", [self.terminal()], [resumed], 40)
            w.tick("process-two", [self.terminal()], [resumed], 60)
            restarted = recovery.Watcher("login-one")
            self.settle(restarted, "process-two", [self.terminal()], [resumed], 80)
            self.assertEqual(opened.call_count, 1)

    def test_login_opens_ghostty_once_but_quitting_does_not_force_it_open(self):
        recovery.save_workspace([self.session()])
        w = recovery.Watcher("new-login")
        with patch.object(recovery, "_run", return_value=subprocess.CompletedProcess([], 0)) as run:
            w.tick(None, [], [], 0)
            w.tick(None, [], [], 10)
            restarted = recovery.Watcher("new-login")
            restarted.tick(None, [], [], 20)
            run.assert_called_once_with(["open", "-a", "Ghostty"])
        # A later login, including a logout/login without reboot, is distinct.
        with patch.object(recovery, "_run", return_value=subprocess.CompletedProcess([], 0)) as run:
            recovery.Watcher("another-login").tick(None, [], [], 30)
            run.assert_called_once()

    def test_partial_failure_keeps_original_snapshot_and_does_not_loop(self):
        saved = [self.session(), self.session("session-two")]
        recovery.save_workspace(saved)
        w = self.watcher()
        with patch.object(recovery, "open_ghostty", side_effect=RuntimeError("Automation denied")) as opened:
            w.tick("process-two", [], [], 0)
            with self.assertRaisesRegex(RuntimeError, "Automation denied"):
                w.tick("process-two", [], [], 20)
            w.persist(error="Automation denied")
            w.tick("process-two", [self.terminal()], [self.session()], 30)
            w.tick("process-two", [self.terminal()], [self.session()], 60)
            opened.assert_called_once()
            self.assertEqual(recovery.saved_workspace()["sessions"], saved)
            self.assertEqual(recovery.read_state()["error"], "Automation denied")
        # Explicit retry only opens the still-missing session.
        self.assertEqual(len(recovery.restoration_plan(saved, [self.terminal()], [self.session()])), 1)

    def test_incomplete_shell_integration_cannot_erase_previous_workspace(self):
        saved = [{**self.session(), "terminal_id": "surface-one"}]
        recovery.save_workspace(saved)
        w = self.watcher()
        self.settle(w, "process-one", [{**self.terminal(), "cwd": ""}], [self.session()])
        self.assertEqual(recovery.saved_workspace()["sessions"], saved)
        self.settle(w, "process-one", [{**self.terminal(), "cwd": ""}], [], 40)
        self.assertEqual(recovery.saved_workspace()["sessions"][0]["source"], "")

    def test_unknown_shell_does_not_block_agent_recovery_or_known_tab_capture(self):
        recovery.save_workspace([self.session()])
        w = self.watcher()
        unknown = {"id": "unknown-shell", "title": "shell", "cwd": ""}
        with patch.object(recovery, "open_ghostty", return_value=1) as opened:
            self.settle(w, "process-two", [unknown], [])
            opened.assert_called_once()
        self.settle(w, "process-two", [unknown, self.terminal()], [self.session()], 40)
        saved = recovery.saved_workspace()["sessions"]
        self.assertEqual(len(saved), 2)
        self.assertEqual(next(s for s in saved if s["terminal_id"] == "unknown-shell")["cwd"], "")
        self.assertEqual(recovery.read_state()["unknown_directories"], 1)

    def test_native_window_restore_override_is_reversible_and_preserves_user_edits(self):
        config = self.root / "ghostty-config"
        config.write_text("window-save-state = always\nfont-size = 16")
        with patch.object(ghostty_config, "CONFIG", config):
            ghostty_config.manage_window_state(True)
            ghostty_config.manage_window_state(True)
            self.assertEqual(config.read_text().count(ghostty_config.BLOCK), 1)
            with config.open("a") as f:
                f.write("\nbackground-opacity = 0.9\n")
            ghostty_config.manage_window_state(False)
        self.assertEqual(config.read_text(), "window-save-state = always\nfont-size = 16\nbackground-opacity = 0.9\n")

    def test_native_override_does_not_overwrite_managed_symlinks_or_edited_blocks(self):
        target = self.root / "managed-config"
        target.write_text("font-size = 16\n")
        config = self.root / "ghostty-config"
        config.symlink_to(target)
        with patch.object(ghostty_config, "CONFIG", config), self.assertRaisesRegex(RuntimeError, "symlinked"):
            ghostty_config.manage_window_state(True)
        self.assertEqual(target.read_text(), "font-size = 16\n")
        config.unlink()
        config.write_text(ghostty_config.BLOCK.replace("never", "always"))
        with patch.object(ghostty_config, "CONFIG", config), self.assertRaisesRegex(RuntimeError, "edited"):
            ghostty_config.manage_window_state(False)

    def test_failed_first_install_rolls_back_native_restore_override(self):
        config = self.root / "ghostty-config"
        config.write_text("window-save-state = always\n")
        with patch.object(recovery.platform, "system", return_value="Darwin"), \
             patch.object(recovery, "processes", return_value={}), \
             patch.object(ghostty_config, "CONFIG", config), \
             patch.object(ghostty_config, "reload_config"), \
             patch.object(scheduler, "install", side_effect=RuntimeError("launchd unavailable")):
            with self.assertRaisesRegex(RuntimeError, "launchd unavailable"):
                recovery.enable()
        self.assertEqual(config.read_text(), "window-save-state = always\n")
        self.assertFalse(read_config()["automatic_recovery"])

    def test_watcher_crash_during_recovery_preserves_the_source(self):
        saved = [self.session()]
        recovery.save_workspace(saved)
        recovery.write_state({"login": "login-one", "handled_process": "process-one", "restore_in_progress": True})
        w = recovery.Watcher("login-one")
        with patch.object(recovery, "open_ghostty") as opened:
            self.settle(w, "process-one", [], [])
            opened.assert_not_called()
        self.assertEqual(recovery.saved_workspace()["sessions"], saved)
        self.assertTrue(recovery.read_state()["restore_failed"])
        self.assertIn("interrupted", recovery.read_state()["error"])

    def test_process_parser_uses_utc_and_detects_application_restarts(self):
        output = " 123 1 ?? Sun Oct 18 11:52:56 2026 /Applications/Ghostty.app/Contents/MacOS/ghostty\n"
        output += " 234 123 ttys001 Sun Oct 18 12:00:00 2026 claude\n"
        with patch.object(recovery.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output, "")) as run:
            rows = recovery.processes()
            self.assertEqual(run.call_args.kwargs["env"]["TZ"], "UTC")
        self.assertEqual(recovery.ghostty_process(rows), "123:Sun Oct 18 11:52:56 2026")
        self.assertEqual(rows[234]["started"], "Sun Oct 18 12:00:00 2026")

    def test_live_native_metadata_rejects_stale_pids_and_headless_sessions(self):
        sid = "01a14f34-29b6-74d3-81c8-13058eaa15c2"
        claude_sid = "0e531940-caf1-4cb7-ba72-33704184227d"
        root = Path(os.environ["CLAUDE_CONFIG_DIR"])
        (root / "sessions").mkdir(parents=True)
        path = root / "projects/project" / f"{claude_sid}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("{}\n")
        registration = {"pid": 2, "sessionId": claude_sid, "cwd": str(self.cwd), "procStart": "Sun Oct 18 12:00:00 2026"}
        (root / "sessions/2.json").write_text(json.dumps(registration))
        codex = Path(os.environ["CODEX_HOME"])
        transcript = codex / "sessions" / f"rollout-{sid}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": str(self.cwd), "source": "cli"}}) + "\n")
        (codex / "session_index.jsonl").write_text(json.dumps({"id": sid, "thread_name": "Current task"}) + "\n")
        rows = {
            1: {"parent": 0, "command": "/Applications/Ghostty.app/Contents/MacOS/ghostty", "tty": "??", "started": "today"},
            2: {"parent": 1, "command": "claude", "tty": "ttys000", "started": registration["procStart"]},
            3: {"parent": 1, "command": "codex", "tty": "ttys001", "started": "today"},
            4: {"parent": 3, "command": "codex", "tty": "??", "started": "today"},
            5: {"parent": 0, "command": "codex", "tty": "??", "started": "today"},
        }
        files = subprocess.CompletedProcess([], 0, f"p4\nn{transcript}\n", "")
        # A collector may group Claude's task under a subdirectory. Recovery
        # still matches the original interactive launch directory.
        known = {("claude", claude_sid): {**self.session(claude_sid, "claude"), "cwd": str(self.cwd / "subdirectory")}}
        with patch.object(recovery, "_run", return_value=files) as run, \
             patch.object(recovery, "_local_sessions", return_value=known):
            live = recovery.live_sessions(rows, "1:today")
            self.assertEqual({s["session_id"] for s in live}, {sid, claude_sid})
            self.assertEqual(run.call_args.args[0][4], "3,4")
            self.assertEqual(next(s for s in live if s["source"] == "codex")["agent_title"], "Current task")
            self.assertEqual(next(s for s in live if s["source"] == "claude")["cwd"], str(self.cwd))
            rows[2]["started"] = "Sun Oct 18 12:00:01 2026"
            self.assertEqual(len(recovery.live_sessions(rows, "1:today")), 1)

    def test_login_identity_does_not_depend_on_formatted_boot_timezone(self):
        for formatted in ("Sun Oct 18 12:00:00 2026", "Sun Oct 18 15:00:00 2026"):
            replies = [subprocess.CompletedProcess([], 0, "security context = { asid = 100024 }", ""),
                       subprocess.CompletedProcess([], 0, "{ sec = 1791903138, usec = 676187 } " + formatted, "")]
            with patch.object(recovery, "_run", side_effect=replies):
                self.assertEqual(recovery.login_identity(), "1791903138:100024")

    def test_launchd_observer_is_separate_from_periodic_network_sync(self):
        with patch.object(scheduler, "LAUNCHD_DIR", self.root / "LaunchAgents"), \
             patch.object(scheduler, "_find_binary", return_value="/path with spaces/llm-archive"), \
             patch.object(scheduler.platform, "system", return_value="Darwin"), \
             patch.object(scheduler.subprocess, "run"):
            scheduler.install_recovery()
        path = self.root / "LaunchAgents/com.llm-archive.recovery.plist"
        config = plistlib.loads(path.read_bytes())
        self.assertEqual(config["ProgramArguments"], ["/path with spaces/llm-archive", "recovery-watch"])
        self.assertEqual(config["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(config["LimitLoadToSessionType"], "Aqua")
        self.assertNotIn("StartInterval", config)

    def test_status_is_read_only_and_disable_preserves_sync_configuration(self):
        root = self.root / "data"
        root.mkdir(exist_ok=True)
        (root / "config.json").write_text(json.dumps({"sync_directory": "/private-journal", "automatic_recovery": True}))
        with patch.object(recovery, "processes", side_effect=AssertionError("read-only status")):
            result = CliRunner().invoke(cli, ["automation", "status"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(json.loads(result.output)["enabled"])
        with patch.object(scheduler, "uninstall_recovery", return_value="disabled"), \
             patch.object(recovery.platform, "system", return_value="Darwin"), \
             patch.object(ghostty_config, "manage_window_state"), patch.object(ghostty_config, "reload_config"):
            recovery.disable()
        self.assertFalse(read_config()["automatic_recovery"])
        self.assertEqual(read_config()["sync_directory"], "/private-journal")


if __name__ == "__main__":
    unittest.main()
