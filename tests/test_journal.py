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
from llm_archive.projects import canonical_remote, repository
from llm_archive.terminal import ghostty_script, reopen_plan


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
        conn.close()

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
        one = sync.configure(str(remote))
        self.assertEqual(sync.run_sync(one), 0)
        with self.device("two"):
            two = sync.configure(str(remote))
            self.assertEqual(sync.run_sync(two), 1)
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


if __name__ == "__main__":
    unittest.main()
