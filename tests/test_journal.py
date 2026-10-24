import json
import os
import plistlib
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, ExitStack
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from llm_archive import db, ingest, scheduler, sync
from llm_archive.cli import cli
from llm_archive.journal import checkpoint, handoff, recent_sessions, resume_command
from llm_archive.parsers import ParsedConversation, ParsedMessage
from llm_archive.paths import collector_lock, device_id
from llm_archive.projects import canonical_remote, claude_encoded, repository
from llm_archive.terminal import ghostty_script, reopen_plan
from llm_archive.overview import display_time, project_overview
from llm_archive import workspaces
from datetime import datetime, timezone


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          text=True, capture_output=True).stdout.strip()


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}))
        self.stack.enter_context(self.device("one"))
        self.stack.enter_context(patch("llm_archive.terminal.ghostty_directories", return_value=set()))

    @contextmanager
    def device(self, name):
        home = self.root / name
        home.mkdir(exist_ok=True)
        with patch.dict(os.environ, {"LLM_ARCHIVE_HOME": str(home / "data"),
                                     "CODEX_HOME": str(home / "codex"),
                                     "CLAUDE_CONFIG_DIR": str(home / "claude")}), \
             patch.object(db, "DB_DIR", home / "data"), \
             patch.object(db, "DB_PATH", home / "data/archive.db"):
            yield home

    def codex_file(self, name="session-one", cwd=None):
        path = Path(os.environ["CODEX_HOME"]) / "sessions/2026" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        cwd = cwd or self.root / "project with spaces"
        cwd.mkdir(exist_ok=True)
        self.append(path, {"type": "session_meta", "payload": {
            "id": name, "cwd": str(cwd), "timestamp": db.utc_now(), "git": {"branch": "feature"}}})
        self.message(path, "user", "Fix the failing checkout test")
        return path

    def append(self, path, record):
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def message(self, path, role, text):
        self.append(path, {"type": "response_item", "timestamp": db.utc_now(), "payload": {
            "type": "message", "role": role, "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}})

    def sessions(self):
        conn = db.get_connection()
        try:
            return recent_sessions(conn)
        finally:
            conn.close()

    def test_growing_session_is_updated_and_unchanged_files_are_not_reparsed(self):
        path = self.codex_file()
        self.assertEqual(ingest.run_ingest(), (1, 1))
        conn = db.get_connection()
        old_id = conn.execute("SELECT id FROM messages").fetchone()[0]
        conn.close()
        with patch.object(ingest, "parse_codex", side_effect=AssertionError("unchanged file parsed")):
            self.assertEqual(ingest.run_ingest(), (0, 0))
        self.message(path, "assistant", "Checkout regression fixed; run integration tests next.")
        self.assertEqual(ingest.run_ingest(), (1, 1))
        conn = db.get_connection()
        self.assertEqual(conn.execute("SELECT min(id) FROM messages").fetchone()[0], old_id)
        self.assertEqual(len(db.search(conn, "regression")), 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM conversations").fetchone()[0], 1)
        conn.close()
        self.assertEqual(ingest.run_ingest(force=True), (1, 0))

    def test_partial_line_and_invalid_records_do_not_lose_future_messages(self):
        path = self.codex_file()
        with path.open("a") as f:
            f.write('null\n{"type":"response_item",')
        self.assertEqual(ingest.run_ingest(), (1, 1))
        with path.open("a") as f:
            f.write('"timestamp":"' + db.utc_now() + '","payload":{"role":"assistant","content":[{"type":"output_text","text":"Recovered message"}]}}\n')
        self.assertEqual(ingest.run_ingest(), (1, 1))
        self.assertEqual(self.sessions()[0]["message_count"], 2)

    def test_tool_evidence_and_handoff_are_separate_from_search(self):
        path = self.codex_file()
        self.append(path, {"type": "response_item", "timestamp": db.utc_now(), "payload": {
            "type": "function_call", "call_id": "call-1", "name": "exec_command",
            "arguments": json.dumps({"cmd": "pytest checkout", "env": {"SECRET": "not-archived"}})}})
        self.append(path, {"type": "response_item", "timestamp": db.utc_now(), "payload": {
            "type": "function_call_output", "call_id": "call-1", "output": "Exit code: 1\ncheckout regression failed"}})
        self.message(path, "assistant", "The unit test still fails; inspect the fixture next.")
        checkpoint(sync=False)
        conn = db.get_connection()
        text = handoff(conn, recent_sessions(conn))
        self.assertIn("pytest checkout", text)
        self.assertIn("Exit code: 1", text)
        self.assertIn("inspect the fixture next", text)
        self.assertIn("codex resume session-one", text)
        self.assertNotIn("not-archived", text)
        self.assertFalse(db.search(conn, "pytest"))
        conn.close()
        self.assertEqual(len(list((db.DB_DIR / "journal").glob("*.md"))), 1)

    def test_handoffs_only_probe_each_working_directory_once(self):
        self.codex_file()
        self.codex_file(name="another-session")
        ingest.run_ingest()
        conn = db.get_connection()
        with patch("llm_archive.journal.git_output", return_value="") as probe:
            handoff(conn, recent_sessions(conn))
        self.assertEqual(probe.call_count, 3)
        conn.close()

    def test_unavailable_git_status_is_not_reported_as_clean(self):
        self.codex_file()
        ingest.run_ingest()
        conn = db.get_connection()
        with patch("llm_archive.journal.git_output", side_effect=["main", "abc123", None]):
            text = handoff(conn, recent_sessions(conn))
        self.assertIn("Working-tree status unavailable", text)
        self.assertNotIn("Working tree clean", text)
        conn.close()

    def test_claude_main_and_subagent_sessions_do_not_overwrite_each_other(self):
        project = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects/-Users-person-projects-widget"
        (project / "parent/subagents").mkdir(parents=True)
        for path, sidechain in [(project / "parent.jsonl", False), (project / "parent/subagents/agent-child.jsonl", True)]:
            self.append(path, {"type": "user", "sessionId": "parent", "isSidechain": sidechain,
                               "cwd": str(self.root), "timestamp": db.utc_now(), "gitBranch": "main",
                               "message": {"role": "user", "content": "Investigate this bug"}})
        self.assertEqual(ingest.run_ingest(), (2, 2))
        conn = db.get_connection()
        self.assertEqual(conn.execute("SELECT count(*) FROM conversations").fetchone()[0], 2)
        self.assertEqual(len(recent_sessions(conn)), 1)
        conn.close()

    def test_old_database_migrates_without_erasing_search_history(self):
        db.DB_DIR.mkdir()
        conn = sqlite3.connect(db.DB_PATH)
        conn.executescript("""CREATE TABLE conversations (id INTEGER PRIMARY KEY, session_id TEXT UNIQUE,
            source TEXT, project TEXT, git_branch TEXT, started_at TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER, role TEXT, content TEXT, timestamp TEXT);
            INSERT INTO conversations VALUES (1, 'legacy', 'codex', 'old', NULL, '2026-01-01');
            INSERT INTO messages VALUES (1, 1, 'user', 'legacy question', '2026-01-01');""")
        conn.close()
        conn = db.get_connection()
        self.assertEqual(conn.execute("SELECT content FROM messages").fetchone()[0], "legacy question")
        self.assertIn("origin_device", [r[1] for r in conn.execute("PRAGMA table_info(conversations)")])
        conn.close()

    def test_message_ids_are_not_reused_after_transcript_rewrite(self):
        path = self.codex_file()
        self.message(path, "assistant", "Old answer")
        ingest.run_ingest()
        conn = db.get_connection()
        old_max = conn.execute("SELECT max(id) FROM messages").fetchone()[0]
        conn.close()
        lines = path.read_text().splitlines(keepends=True)
        path.write_text("".join(lines[:-1]))
        ingest.run_ingest()
        self.message(path, "assistant", "New answer")
        ingest.run_ingest()
        conn = db.get_connection()
        self.assertGreater(conn.execute("SELECT max(id) FROM messages").fetchone()[0], old_max)
        self.assertFalse(db.search(conn, "Old"))
        self.assertEqual(len(db.search(conn, "New")), 1)
        versions = db.revisions(conn, self.sessions()[0]["project"])
        self.assertTrue(any("Old answer" in r["snapshot"] for r in versions))
        conn.close()

    def test_history_survives_truncation_and_can_be_exported(self):
        path = self.codex_file()
        self.message(path, "assistant", "Important original context")
        ingest.run_ingest()
        old_time = self.sessions()[0]["last_activity"]
        path.write_text("".join(path.read_text().splitlines(keepends=True)[:2]))
        ingest.run_ingest()
        sessions = self.sessions()
        self.assertEqual(sessions[0]["last_activity"], old_time)
        result = CliRunner().invoke(cli, ["history", sessions[0]["project"]])
        self.assertEqual(result.exit_code, 0, result.output)
        key = result.output.split()[0]
        result = CliRunner().invoke(cli, ["history", sessions[0]["project"], "--revision", key])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Important original context", result.output)

    def test_tool_result_rewrites_preserve_original_evidence(self):
        path = self.codex_file()
        event = {"type": "response_item", "timestamp": db.utc_now(), "payload": {
            "type": "function_call_output", "call_id": "one", "output": "original failure"}}
        self.append(path, event)
        ingest.run_ingest()
        text = path.read_text().replace("original failure", "corrected output")
        path.write_text(text)
        ingest.run_ingest()
        conn = db.get_connection()
        self.assertIn("original failure", db.revisions(conn, self.sessions()[0]["project"])[0]["snapshot"])
        conn.close()

    def test_compressed_codex_archives_are_streamed_and_deduplicated(self):
        import zstandard
        path = self.codex_file()
        raw = path.read_bytes()
        archive = Path(os.environ["CODEX_HOME"]) / "archived_sessions/archive.jsonl.zst"
        archive.parent.mkdir()
        half = len(raw) // 2
        compressor = zstandard.ZstdCompressor()
        archive.write_bytes(compressor.compress(raw[:half]) + compressor.compress(raw[half:]))
        ingest.run_ingest()
        self.assertEqual(len(self.sessions()), 1)
        self.assertEqual(self.sessions()[0]["message_count"], 1)
        path.unlink()
        self.assertEqual(ingest.run_ingest(), (0, 0))

    def test_corrupt_compressed_archive_does_not_erase_captured_sessions(self):
        self.codex_file()
        bad = Path(os.environ["CODEX_HOME"]) / "sessions/bad.jsonl.zst"
        bad.write_bytes(b"not a zstandard frame")
        with self.assertRaisesRegex(RuntimeError, "Checkpoint incomplete"):
            ingest.run_ingest()
        self.assertEqual(len(self.sessions()), 1)

    def test_status_groups_sessions_and_reports_recorded_next_step(self):
        one = self.codex_file()
        self.message(one, "assistant", "Test fixed. Next step: run integration checks.")
        two = self.codex_file(name="second")
        self.message(two, "assistant", "Build complete. Next step: review the diff.")
        ingest.run_ingest()
        runner = CliRunner()
        result = runner.invoke(cli, ["status", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        record = json.loads(result.output)
        self.assertEqual(len(record["projects"]), 1)
        self.assertEqual(record["projects"][0]["session_count"], 2)
        self.assertIn("review the diff", record["projects"][0]["next_step"])
        result = runner.invoke(cli, ["status", "--sessions"])
        self.assertIn("session-one", result.output)
        self.assertIn("second", result.output)

    def test_times_show_relative_age_and_invalid_timestamps_remain_readable(self):
        now = datetime(2026, 10, 18, 14, 30, tzinfo=timezone.utc)
        self.assertIn("3m ago", display_time("2026-10-18T14:27:00Z", now=now))
        self.assertEqual(display_time(None), "never")
        self.assertEqual(display_time("unknown"), "unknown")

    def test_generic_followup_does_not_replace_the_actual_task_in_status(self):
        path = self.codex_file()
        self.message(path, "user", "fix anything that is needed")
        self.message(path, "user", "[Earlier usage-limit notes no longer apply. Continue working normally.]")
        ingest.run_ingest()
        conn = db.get_connection()
        overview = project_overview(conn, recent_sessions(conn))
        conn.close()
        self.assertEqual(overview[0]["task"], "Fix the failing checkout test")

    def test_workspaces_select_and_restore_a_specific_session(self):
        self.codex_file()
        second = self.codex_file(name="selected-session")
        ingest.run_ingest()
        record = workspaces.save("daily", self.sessions())
        self.assertEqual(len(record["sessions"]), 1)
        result = CliRunner().invoke(cli, ["reopen", "--workspace", "daily", "--dry-run", "--resume"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("selected-session", result.output)
        result = CliRunner().invoke(cli, ["reopen", "--session", "session-one", "--dry-run", "--resume"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("codex resume session-one", result.output)
        self.assertNotIn("codex resume selected-session", result.output)

    def test_workspace_from_tabs_preserves_untracked_shell_directories(self):
        folder = self.root / "shell-only"
        folder.mkdir()
        record = workspaces.save("shells", [], directories={str(folder.resolve())})
        plan = reopen_plan(record["sessions"], resume=True)
        self.assertEqual(plan[0]["cwd"], str(folder.resolve()))
        self.assertEqual(plan[0]["command"], "")
        for name in ("../escape", "/tmp/escape", ".", "other:workspace"):
            with self.assertRaises(ValueError):
                workspaces.save(name, self.sessions())

    def test_ghostty_script_skips_existing_directories_unless_explicitly_requested(self):
        plan = [{"cwd": "/tmp/project", "project": "project", "command": ""}]
        self.assertIn("repeat with existingTerminal", ghostty_script(plan))
        self.assertIn("return openedCount", ghostty_script(plan))
        plan[0]["new_tab"] = True
        self.assertNotIn("repeat with existingTerminal", ghostty_script(plan))

    def test_repo_identity_groups_worktrees_and_removes_remote_credentials(self):
        root = self.root / "repository"
        root.mkdir()
        git(root, "init", "-b", "main")
        git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "Initial")
        git(root, "remote", "add", "origin", "https://token:secret@github.com/example/widget.git")
        worktree = self.root / "different-worktree-name"
        git(root, "worktree", "add", "-b", "feature", str(worktree))
        repository.cache_clear()
        self.assertEqual(repository(str(root))["project_key"], repository(str(worktree))["project_key"])
        self.assertEqual(repository(str(worktree))["project"], "widget")
        self.assertEqual(canonical_remote("git@github.com:example/widget.git"), "github.com/example/widget")
        self.assertNotIn("secret", repository(str(root))["repo_url"])

    def test_deleted_worktrees_keep_their_repository(self):
        root = self.root / "repository"
        root.mkdir()
        git(root, "init", "-b", "main")
        git(root, "remote", "add", "origin", "git@github.com:example/widget.git")
        repository.cache_clear()
        gone = str(root / ".claude/worktrees/agent-1")
        self.assertEqual(repository(gone)["project_key"], "github.com/example/widget")
        self.assertEqual(repository(gone)["cwd"], gone)
        recorded = repository(str(self.root / "review-510"), "https://github.com/example/widget.git")
        self.assertEqual((recorded["project_key"], recorded["project"]), ("github.com/example/widget", "widget"))
        scratchpad = f"/private/tmp/claude-501/{claude_encoded(str(root.resolve()))}/session/scratchpad/review"
        self.assertEqual(repository(scratchpad)["project_key"], "github.com/example/widget")
        self.assertTrue(repository(str(self.root / "unrelated"))["project_key"].startswith("directory:"))

    def test_deleting_a_checkout_does_not_forget_its_repository(self):
        root = self.root / "repository"
        root.mkdir()
        git(root, "init", "-b", "main")
        git(root, "remote", "add", "origin", "git@github.com:example/widget.git")
        worktree = root / "nested"
        path = self.codex_file(cwd=worktree)
        repository.cache_clear()
        ingest.run_ingest()
        worktree.rmdir()
        repository.cache_clear()
        self.message(path, "assistant", "Done")
        ingest.run_ingest()
        self.assertEqual(self.sessions()[0]["project_key"], "github.com/example/widget")

    def test_forced_ingest_reidentifies_sessions_whose_transcripts_are_gone(self):
        root = self.root / "repository"
        root.mkdir()
        git(root, "init", "-b", "main")
        git(root, "remote", "add", "origin", "git@github.com:example/widget.git")
        worktree = root / ".claude/worktrees/agent-1"
        worktree.mkdir(parents=True)
        path = self.codex_file(cwd=worktree)
        ingest.run_ingest()
        conn = db.get_connection()
        conn.execute("UPDATE conversations SET project = 'agent-1', project_key = ?", ("directory:" + str(worktree),))
        conn.commit()
        conn.close()
        path.unlink()
        worktree.rmdir()
        ingest.run_ingest(force=True)
        self.assertEqual(self.sessions()[0]["project_key"], "github.com/example/widget")

    def test_reopen_deduplicates_directories_and_does_not_replay_commands(self):
        path = self.codex_file()
        ingest.run_ingest()
        sessions = self.sessions()
        plan = reopen_plan(sessions * 2, resume=True)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["command"], "codex resume session-one")
        self.assertNotIn("Fix the failing", ghostty_script(plan))
        self.assertIn("new tab in targetWindow", ghostty_script(plan))
        path.unlink()
        missing = reopen_plan(sessions, resume=True)[0]
        self.assertEqual(missing["command"], "")
        self.assertTrue(missing["cwd"])

    def test_reopen_maps_other_computer_without_trying_to_resume_foreign_session(self):
        self.codex_file()
        ingest.run_ingest()
        sessions = self.sessions()
        sessions[0]["origin_device"] = "other-device"
        sessions[0]["cwd"] = "/missing/computer/project with spaces"
        plan = reopen_plan(sessions, resume=True, projects_root=self.root)
        self.assertTrue(plan[0]["cwd"])
        self.assertEqual(plan[0]["command"], "")
        self.assertIn("unavailable", plan[0]["note"])

    def test_resume_ids_cannot_inject_shell_commands_or_cli_options(self):
        for sid in ["--yolo", "abc;touch /tmp/should-not-exist", "abc\nexec evil"]:
            with self.assertRaises(ValueError):
                resume_command({"source": "codex", "session_id": sid})

    def test_reopen_dry_run_and_status_json_do_not_open_apps(self):
        self.codex_file()
        ingest.run_ingest()
        runner = CliRunner()
        result = runner.invoke(cli, ["status", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(json.loads(result.output)["sessions"]), 1)
        with patch("llm_archive.terminal.open_ghostty", side_effect=AssertionError("app opened")):
            result = runner.invoke(cli, ["reopen", "--resume", "--dry-run"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("codex resume session-one", result.output)

    def test_collector_lock_prevents_overlapping_checkpoints(self):
        with collector_lock():
            with self.assertRaisesRegex(RuntimeError, "already running"):
                ingest.run_ingest()

    def test_semantic_search_excludes_deleted_message_vectors(self):
        import numpy as np
        from llm_archive import embeddings
        self.codex_file()
        ingest.run_ingest()
        conn = db.get_connection()
        live_id = conn.execute("SELECT id FROM messages").fetchone()[0]
        ids = np.array([999000 + i for i in range(10)] + [live_id])
        vectors = np.array([[1.0, 0.0]] * 10 + [[0.8, 0.6]])
        with patch.object(embeddings, "_load_existing", return_value=(ids, vectors, {"provider": "ollama"})), \
             patch.object(embeddings, "_ollama_available", return_value=True), \
             patch.object(embeddings, "_embed", return_value=np.array([[1.0, 0.0]])):
            self.assertEqual(len(embeddings.semantic_search(conn, "checkout", limit=1)), 1)
        conn.close()

    def test_capture_failure_is_reported_without_dropping_good_sessions(self):
        self.codex_file()
        broken = self.codex_file(name="unreadable")
        real_parse = ingest.parse_codex
        with patch.object(ingest, "parse_codex", side_effect=lambda p: None if p == broken else real_parse(p)):
            with self.assertRaisesRegex(RuntimeError, "Checkpoint incomplete"):
                ingest.run_ingest()
        self.assertEqual(len(self.sessions()), 1)
        conn = db.get_connection()
        self.assertIn("unreadable", db.get_state(conn)["capture_error"])
        self.assertNotIn("last_success", db.get_state(conn))
        conn.close()
        ingest.run_ingest()
        self.assertEqual(len(self.sessions()), 2)

    def test_launchd_service_checks_every_two_minutes_and_preserves_custom_roots(self):
        agents = self.root / "LaunchAgents"
        with patch.object(scheduler, "LAUNCHD_DIR", agents), \
             patch.object(scheduler, "_find_binary", return_value="/some path/llm-archive"), \
             patch.object(scheduler.subprocess, "run"):
            scheduler._install_launchd()
        config = plistlib.loads((agents / "com.llm-archive.ingest.plist").read_bytes())
        self.assertEqual(config["StartInterval"], 120)
        self.assertEqual(config["ProgramArguments"], ["/some path/llm-archive", "checkpoint"])
        self.assertEqual(config["EnvironmentVariables"]["CODEX_HOME"], os.environ["CODEX_HOME"])
        self.assertTrue(config["RunAtLoad"])

    def test_two_computers_exchange_updates_without_duplicates(self):
        remote = self.root / "remote.git"
        git(self.root, "init", "--bare", "--initial-branch=main", str(remote))
        local_path = self.codex_file()
        ingest.run_ingest()
        workspaces.save("daily", self.sessions())
        one = sync.configure(str(remote))
        self.assertEqual(sync.run_sync(one), 0)
        with self.device("two"):
            two = sync.configure(str(remote))
            self.assertEqual(sync.run_sync(two), 1)
            self.assertEqual(workspaces.load("daily")["sessions"][0]["session_id"], "session-one")
            self.assertEqual(sync.run_sync(two), 0)
            self.assertEqual(len(self.sessions()), 1)
            self.codex_file(name="session-two")
            ingest.run_ingest()
            sync.run_sync(two)
        # Both machines now have new work relative to the original common commit.
        self.message(local_path, "assistant", "Local update while the other computer was working")
        ingest.run_ingest()
        self.assertEqual(sync.run_sync(one), 1)
        self.assertEqual(len(self.sessions()), 2)
        self.assertEqual(sync.run_sync(one), 0)
        self.assertFalse(any(p.name.endswith(".db") for p in one.rglob("*")))
        with self.device("two"):
            self.assertEqual(sync.run_sync(two), 1)
            self.assertEqual(next(s for s in self.sessions() if s["session_id"] == "session-one")["message_count"], 2)

    def test_preserved_versions_sync_without_duplicates_even_when_current_record_is_unchanged(self):
        remote = self.root / "remote.git"
        git(self.root, "init", "--bare", "--initial-branch=main", str(remote))
        path = self.codex_file()
        self.message(path, "assistant", "Recover this context")
        ingest.run_ingest()
        path.write_text("".join(path.read_text().splitlines(keepends=True)[:2]))
        ingest.run_ingest()
        root = sync.configure(str(remote))
        sync.run_sync(root)
        with self.device("two"):
            root2 = sync.configure(str(remote))
            sync.run_sync(root2)
            conn = db.get_connection()
            self.assertEqual(conn.execute("SELECT count(*) FROM conversation_revisions").fetchone()[0], 1)
            self.assertIn("Recover this context", conn.execute("SELECT snapshot FROM conversation_revisions").fetchone()[0])
            conn.close()
            sync.run_sync(root2)
            conn = db.get_connection()
            self.assertEqual(conn.execute("SELECT count(*) FROM conversation_revisions").fetchone()[0], 1)
            conn.close()

    def test_sync_writes_append_friendly_text_records(self):
        path = self.codex_file()
        self.codex_file(name="another-session")
        ingest.run_ingest()
        root = self.root / "snapshot"
        conn = db.get_connection()
        records = sync.export_device(conn, root)
        paths = sorted(records.glob("*.jsonl"))
        self.assertEqual(len(paths), 2)
        self.assertFalse(list(records.glob("*.json.gz")))
        before = {p: p.read_text() for p in paths}
        conn.close()
        self.message(path, "assistant", "Appended answer")
        ingest.run_ingest()
        conn = db.get_connection()
        sync.export_device(conn, root)
        conn.close()
        changed = [p for p in paths if p.read_text() != before[p]]
        self.assertEqual(len(changed), 1)
        old, new = before[changed[0]].splitlines(), changed[0].read_text().splitlines()
        # Only the session header and the new message differ, so Git stores a small delta.
        self.assertEqual(len(set(new) - set(old)), 2)
        self.assertEqual(json.loads((root / sync.MARKER).read_text())["version"], sync.FORMAT_VERSION)

    def test_version_two_compressed_records_are_still_imported(self):
        import gzip
        self.codex_file()
        ingest.run_ingest()
        conn = db.get_connection()
        record = db.conversation_record(conn, self.sessions()[0]["id"])
        conn.close()
        folder = self.root / "v2" / "devices" / "other" / "sessions"
        folder.mkdir(parents=True)
        (folder / "record.json.gz").write_bytes(gzip.compress(json.dumps(record).encode()))
        with self.device("two"):
            conn = db.get_connection()
            self.assertEqual(sync.import_devices(conn, self.root / "v2"), 1)
            conn.close()

    def test_changing_directory_does_not_preserve_a_copy_and_prefix_copies_are_pruned(self):
        path = self.codex_file()
        ingest.run_ingest()
        self.append(path, {"type": "session_meta", "payload": {
            "id": "session-one", "cwd": str(self.root), "timestamp": db.utc_now()}})
        self.message(path, "assistant", "Now elsewhere")
        ingest.run_ingest()
        conn = db.get_connection()
        self.assertEqual(conn.execute("SELECT count(*) FROM conversation_revisions").fetchone()[0], 0)
        cid = self.sessions()[0]["id"]
        prefix = db.conversation_record(conn, cid)
        prefix["messages"] = prefix["messages"][:1]
        db.store_revision(conn, cid, prefix)
        rewritten = dict(prefix, messages=[{"role": "user", "content": "Lost", "timestamp": "t"}])
        db.store_revision(conn, cid, rewritten)
        self.assertEqual(db.prune_redundant_revisions(conn), 1)
        self.assertIn("Lost", conn.execute("SELECT snapshot FROM conversation_revisions").fetchone()[0])
        conn.close()

    def test_compacted_history_is_adopted_by_other_computers(self):
        remote = self.root / "remote.git"
        git(self.root, "init", "--bare", "--initial-branch=main", str(remote))
        path = self.codex_file()
        ingest.run_ingest()
        one = sync.configure(str(remote))
        sync.run_sync(one)
        with self.device("two"):
            two = sync.configure(str(remote))
            self.codex_file(name="session-two")
            ingest.run_ingest()
            sync.run_sync(two)
        sync.run_sync(one)
        self.message(path, "assistant", "Before compaction")
        ingest.run_ingest()
        sync.run_sync(one)
        sync.compact_history(one)
        self.assertEqual(git(one, "rev-list", "--count", "HEAD"), "1")
        self.assertEqual(git(remote, "rev-list", "--count", "main"), "1")
        with self.device("two"):
            self.codex_file(name="session-three")
            ingest.run_ingest()
            self.assertEqual(sync.run_sync(two), 1)
            self.assertEqual(len(self.sessions()), 3)
            self.assertEqual(git(two, "rev-list", "--count", "HEAD"), "2")
        self.assertEqual(sync.run_sync(one), 1)
        self.assertEqual(len(self.sessions()), 3)

    def test_legacy_jsonl_snapshots_are_still_imported(self):
        self.codex_file()
        ingest.run_ingest()
        conn = db.get_connection()
        record = db.conversation_record(conn, self.sessions()[0]["id"])
        conn.close()
        root = self.root / "legacy"
        folder = root / "devices" / device_id()
        folder.mkdir(parents=True)
        (folder / "sessions.jsonl").write_text(json.dumps(record) + "\n")
        with self.device("two"):
            conn = db.get_connection()
            self.assertEqual(sync.import_devices(conn, root), 1)
            conn.commit()
            self.assertEqual(len(recent_sessions(conn)), 1)
            conn.close()

    def test_sync_removes_abandoned_transfer_files_but_not_recent_ones(self):
        remote = self.root / "remote.git"
        git(self.root, "init", "--bare", "--initial-branch=main", str(remote))
        root = sync.configure(str(remote))
        packs = root / ".git/objects/pack"
        packs.mkdir(parents=True, exist_ok=True)
        old, recent = packs / "tmp_pack_old", packs / "tmp_pack_recent"
        old.write_bytes(b"x")
        recent.write_bytes(b"x")
        os.utime(old, (0, 0))
        sync.run_sync(root)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_offline_sync_keeps_local_capture_and_reports_failure(self):
        remote = self.root / "remote.git"
        git(self.root, "init", "--bare", "--initial-branch=main", str(remote))
        root = sync.configure(str(remote))
        self.codex_file()
        ingest.run_ingest()
        sync.run_sync(root)
        git(root, "remote", "set-url", "origin", str(self.root / "unavailable.git"))
        with self.assertRaisesRegex(RuntimeError, "local capture is intact"):
            sync.run_sync(root)
        self.assertEqual(len(self.sessions()), 1)
        conn = db.get_connection()
        self.assertTrue(db.get_state(conn)["sync_error"])
        self.assertTrue(db.get_state(conn)["last_success"])
        conn.close()

class GitOutputLocksTest(unittest.TestCase):
    def test_read_only_git_never_takes_optional_locks(self):
        # A status killed on its timeout must not leave .git/index.lock behind (concrete2 bug 209).
        from llm_archive.projects import git_output
        with tempfile.TemporaryDirectory() as d:
            git(d, "init", "-q")
            Path(d, "f").write_text("x")
            git(d, "add", "f")
            git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
            os.utime(Path(d, "f"), (1, 1))  # stale stat data: a plain status would refresh the index
            before = Path(d, ".git", "index").stat().st_mtime_ns
            seen = {}
            real = subprocess.run
            def spy(*a, **k):
                seen.update(k.get("env") or {})
                return real(*a, **k)
            with patch("llm_archive.projects.subprocess.run", side_effect=spy):
                self.assertIsNotNone(git_output(d, "status", "--short"))
            self.assertEqual(seen.get("GIT_OPTIONAL_LOCKS"), "0")
            self.assertEqual(Path(d, ".git", "index").stat().st_mtime_ns, before,
                             "a read-only status rewrote the index")


if __name__ == "__main__":
    unittest.main()

