"""Observe live Ghostty workspaces and recover once per application launch.

No shell hooks, transcript commands, or user prompts are executed. Process
metadata is used only to identify interactive agents and their native sessions.
"""
import fcntl
import json
import os
import platform
import re
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path

from llm_archive import db
from llm_archive.journal import local_workspace
from llm_archive.paths import atomic_write, data_dir, device_id, machine_name, read_config
from llm_archive.terminal import ghostty_terminals, open_ghostty, reopen_plan

WORKSPACE = "automatic"
POLL_SECONDS = 10
SETTLE_SECONDS = 20
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)


def read_state() -> dict:
    path = data_dir() / "recovery-state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def write_state(state: dict) -> None:
    atomic_write(data_dir() / "recovery-state.json", json.dumps(state, indent=2) + "\n")


def saved_workspace() -> dict:
    path = data_dir() / "workspaces" / f"{WORKSPACE}.json"
    if not path.exists():
        return {"sessions": []}
    record = json.loads(path.read_text())
    if record.get("version") != 1 or record.get("origin_device") != device_id():
        raise ValueError("Invalid local automatic workspace")
    return record


def _run(args, timeout=30):
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout)


def processes() -> dict[int, dict]:
    # Claude's procStart registrations use UTC, independent of the user's TZ.
    result = subprocess.run(["ps", "-x", "-o", "pid=,ppid=,tty=,lstart=,comm="],
                            text=True, capture_output=True, timeout=30, env={**os.environ, "TZ": "UTC"})
    if result.returncode:
        raise RuntimeError("Cannot inspect interactive agent processes: " + result.stderr.strip())
    rows = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 8)
        if len(fields) == 9:
            pid, parent, tty, *rest = fields
            rows[int(pid)] = {"pid": int(pid), "parent": int(parent), "tty": tty,
                              "started": " ".join(rest[:5]), "command": rest[5]}
    return rows


def ghostty_process(rows: dict[int, dict]) -> str | None:
    for pid, row in rows.items():
        if row["command"].endswith("/Ghostty.app/Contents/MacOS/ghostty"):
            return f"{pid}:{row['started']}"
    return None


def _descendants(rows, roots):
    found = set(roots)
    while True:
        new = {pid for pid, r in rows.items() if r["parent"] in found} - found
        if not new:
            return found
        found.update(new)


def _local_sessions() -> dict[tuple[str, str], dict]:
    conn = db.get_connection()
    try:
        return {(r["source"], r["session_id"]): dict(r) for r in conn.execute(
            "SELECT * FROM conversations WHERE origin_device = ? AND coalesce(is_subagent, 0) = 0", (device_id(),))}
    finally:
        conn.close()


def _shell(cwd: str) -> dict:
    return {"cwd": cwd, "project": (Path(cwd).name or cwd) if cwd else "(terminal)", "source": "", "session_id": "",
            "source_path": "", "origin_device": device_id(), "machine": machine_name()}


def live_sessions(rows: dict[int, dict], process: str | None) -> list[dict]:
    """Identify native sessions; do not inspect environment variables or shell history."""
    if not process:
        return []
    family = _descendants(rows, {int(process.split(":", 1)[0])})
    interactive = {pid for pid in family if rows[pid]["tty"] not in ("??", "?")
                   and Path(rows[pid]["command"]).name in ("claude", "codex")}
    known = _local_sessions()
    result = {}
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()
    codex = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    for pid in interactive:
        if Path(rows[pid]["command"]).name != "claude":
            continue
        path = claude / "sessions" / f"{pid}.json"
        try:
            meta = json.loads(path.read_text())
        except FileNotFoundError:
            continue  # Older Claude versions have no trustworthy process/session binding.
        if meta.get("pid") != pid or not meta.get("sessionId") or not meta.get("cwd"):
            continue
        if meta.get("procStart") and " ".join(meta["procStart"].split()) != rows[pid]["started"]:
            continue  # Ignore stale registrations after PID reuse.
        sid = meta["sessionId"]
        key = ("claude", sid)
        session = known.get(key)
        if not session:
            paths = list((claude / "projects").glob(f"*/{sid}.jsonl")) if UUID.fullmatch(sid) else []
            session = {**_shell(meta["cwd"]), "source": "claude", "session_id": sid,
                       "source_path": str(paths[0]) if len(paths) == 1 else ""}
        # A transcript can mention a task's subdirectory while the interactive
        # agent and Ghostty surface were launched from its parent directory.
        result[key] = {**session, "cwd": meta["cwd"]}

    # New Codex versions share a background server. Its open transcript/writer
    # locks identify the sessions; requiring a terminal ancestor excludes desktop
    # app sessions and unrelated background agents.
    codex_pids = [pid for pid in _descendants(rows, interactive)
                  if Path(rows[pid]["command"]).name == "codex"]
    if codex_pids:
        files = _run(["lsof", "-nP", "-a", "-p", ",".join(map(str, sorted(codex_pids))), "-Fpn"])
        if files.returncode and not files.stdout:
            raise RuntimeError("Could not identify active Codex sessions; keeping the previous workspace.")
        paths = [Path(line[1:]) for line in files.stdout.splitlines() if line.startswith("n")]
        for path in paths:
            if not path.is_relative_to(codex):
                continue
            match = UUID.search(path.stem)
            if not match or not (path.suffix == ".jsonl" or path.parent == codex / "thread-writer-locks"):
                continue
            key = ("codex", match.group())
            if key in known:
                result[key] = known[key]
            elif path.suffix == ".jsonl":
                try:
                    with path.open() as f:
                        meta = json.loads(f.readline()).get("payload", {})
                    source = meta.get("source")
                    subagent = isinstance(source, dict) and bool(source.get("subagent"))
                    if meta.get("id") == key[1] and meta.get("cwd") and not subagent:
                        result[key] = {**_shell(meta["cwd"]), "source": "codex", "session_id": key[1], "source_path": str(path)}
                except (OSError, ValueError):
                    continue
    index = codex / "session_index.jsonl"
    if index.exists():
        for line in index.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            key = ("codex", row.get("id"))
            if key in result:
                result[key] = {**result[key], "agent_title": row.get("thread_name", "")}
    return list(result.values())


def _title(value: str) -> str:
    value = re.sub(r"^[^\w]+", "", value).split(" | ", 1)[0]
    return value.removesuffix("...").removesuffix("…").strip().casefold()


def workspace_sessions(terminals: list[dict], active: list[dict], previous: list[dict]) -> list[dict]:
    """Bind unambiguous live sessions to surfaces, never guess the newest conversation."""
    unknown = [t for t in terminals if not t.get("cwd") or not Path(t["cwd"]).is_dir()]
    terminals = [t for t in terminals if t not in unknown]
    by_cwd = defaultdict(list)
    for s in active:
        by_cwd[str(Path(s["cwd"]).resolve())].append(s)
    old = {s.get("terminal_id"): s for s in previous}
    chosen, used = {}, set()
    for t in terminals:
        cwd = str(Path(t["cwd"]).resolve())
        candidates = by_cwd[cwd]
        title = _title(t.get("title") or "")
        matches = [s for s in candidates if title and s.get("agent_title") and
                   (_title(s["agent_title"]) == title or len(title) >= 12 and _title(s["agent_title"]).startswith(title))]
        prev = old.get(t["id"], {})
        if len(matches) != 1:
            matches = [s for s in candidates if (s["source"], s["session_id"]) == (prev.get("source"), prev.get("session_id"))]
        if len(matches) == 1:
            s = matches[0]
            key = (s["source"], s["session_id"])
            if key not in used:
                chosen[t["id"]] = s
                used.add(key)
    # A single remaining surface and agent in a directory are unambiguous, even
    # if a tool changed its terminal title. Several unmatched surfaces stay shells.
    for cwd, candidates in by_cwd.items():
        remaining = [s for s in candidates if (s["source"], s["session_id"]) not in used]
        surfaces = [t for t in terminals if str(Path(t["cwd"]).resolve()) == cwd and t["id"] not in chosen]
        if len(remaining) == len(surfaces) == 1:
            chosen[surfaces[0]["id"]] = remaining[0]
    sessions = [{**{k: v for k, v in chosen.get(t["id"], _shell(str(Path(t["cwd"]).resolve()))).items()
                if k not in ("id", "agent_title", "message_count")},
             "terminal_id": t["id"], "terminal_title": re.sub(r"^[^\w]+", "", t.get("title") or "")} for t in terminals]
    for t in unknown:
        # Keep an existing surface's last known directory during a transient
        # integration gap. A new surface with no cwd cannot yet be restored.
        session = old.get(t["id"], {**_shell(""), "terminal_id": t["id"],
                                   "terminal_title": re.sub(r"^[^\w]+", "", t.get("title") or "")})
        if session.get("source") and not any((s["source"], s["session_id"]) ==
                                               (session["source"], session["session_id"]) for s in active):
            session = {**session, "source": "", "session_id": "", "source_path": ""}
        sessions.append(session)
    return sessions


def save_workspace(sessions: list[dict]) -> None:
    record = {"version": 1, "name": WORKSPACE, "origin_device": device_id(),
              "saved_at": db.utc_now(), "sessions": sessions, "automatic": True}
    atomic_write(data_dir() / "workspaces" / f"{WORKSPACE}.json", json.dumps(record, indent=2, ensure_ascii=False) + "\n")


def restoration_plan(saved: list[dict], terminals: list[dict], active: list[dict]) -> list[dict]:
    running = {(s["source"], s["session_id"]) for s in active}
    shells = Counter(s["cwd"] for s in workspace_sessions(terminals, active, []) if not s["source"])
    plan = []
    for s in saved:
        if not local_workspace(s) or (s.get("source"), s.get("session_id")) in running:
            continue
        p = reopen_plan([s], resume=bool(s.get("source")))[0]
        if not p["cwd"]:
            continue
        if not p["command"] and shells[p["cwd"]]:
            shells[p["cwd"]] -= 1
            continue
        # Session identity, not directory, is the duplicate key for recovery.
        plan.append({**p, "new_tab": True})
    return plan


def login_identity() -> str:
    result = _run(["launchctl", "print", f"gui/{os.getuid()}"])
    match = re.search(r"\basid = (\d+)", result.stdout)
    boot = _run(["sysctl", "-n", "kern.boottime"])
    boot_seconds = re.search(r"\bsec = (\d+)", boot.stdout)
    if result.returncode or not match or boot.returncode or not boot_seconds:
        raise RuntimeError("Cannot identify the macOS login session")
    return boot_seconds.group(1) + ":" + match.group(1)


class Watcher:
    def __init__(self, login: str):
        self.state = read_state()
        if self.state.get("restore_in_progress"):
            self.state.update(restore_failed=True,
                              error="Workspace recovery was interrupted. Use llm-archive automation recover to retry.")
        self.login = login
        self.observed = None
        self.signature = None
        self.stable_since = 0.0
        self.saved_signature = None

    def persist(self, **updates):
        self.state.update(updates)
        write_state(self.state)

    def tick(self, process: str | None, terminals: list[dict], active: list[dict], now: float):
        self.persist(last_check=db.utc_now(), **({} if self.state.get("restore_failed") else {"error": ""}))
        if self.state.get("login") != self.login:
            self.persist(login=self.login)  # Claim before opening; watcher restarts must not relaunch a deliberately quit app.
            if not process and saved_workspace()["sessions"]:
                result = _run(["open", "-a", "Ghostty"])
                if result.returncode:
                    raise RuntimeError("Could not start Ghostty: " + result.stderr.strip())
                return
        if not process:
            self.observed = None
            self.signature = None
            return  # Keep the last snapshot when the app quits or crashes.
        # Titles include busy spinners. Only structure/cwd/session identity needs to settle.
        structure = json.dumps([process, [(t["id"], t.get("cwd")) for t in terminals],
                                sorted((s["source"], s["session_id"]) for s in active)])
        if self.observed != process or self.signature != structure:
            self.observed, self.signature, self.stable_since = process, structure, now
            return
        # Launch recovery needs a longer settling period than ordinary tab edits.
        delay = SETTLE_SECONDS if self.state.get("handled_process") != process else POLL_SECONDS
        if now - self.stable_since < delay:
            return
        if self.state.get("handled_process") != process:
            saved = saved_workspace()["sessions"]
            self.persist(handled_process=process, last_restore_attempt=db.utc_now(), restore_failed=False,
                         restore_in_progress=True, error="")
            try:
                plan = restoration_plan(saved, terminals, active)
                count = open_ghostty(plan) if plan else 0
            except Exception:
                self.persist(restore_failed=True, restore_in_progress=False)
                raise
            self.persist(last_restore=db.utc_now(), restored_tabs=count, restore_in_progress=False)
            self.signature = None  # Allow resumed agents to initialize before replacing the snapshot.
            return
        if self.state.get("restore_failed"):
            return  # Preserve the recovery source and avoid an endless partial-restore loop.
        saved = saved_workspace()["sessions"]
        sessions = workspace_sessions(terminals, active, saved)
        content = json.dumps(sessions, sort_keys=True)
        if content != self.saved_signature:
            if sessions != saved:
                save_workspace(sessions)
            self.saved_signature = content
            self.persist(last_capture=db.utc_now(), saved_tabs=len(sessions),
                         resumable_tabs=sum(bool(s["source"] and s.get("source_path")) for s in sessions),
                         unknown_directories=sum(not bool(s.get("cwd")) for s in sessions))


def enable() -> list[str]:
    if platform.system() != "Darwin":
        raise RuntimeError("Automatic Ghostty recovery currently requires macOS.")
    from llm_archive.scheduler import install, install_recovery
    from llm_archive.ghostty_config import manage_window_state, reload_config
    rows = processes()
    process = ghostty_process(rows)
    terminals = ghostty_terminals() if process else []
    active = live_sessions(rows, process)
    if (not read_config().get("automatic_recovery") and process and terminals
            and all(t.get("cwd") and Path(t["cwd"]).is_dir() for t in terminals)):
        sessions = workspace_sessions(terminals, active, saved_workspace()["sessions"])
        save_workspace(sessions)
        state = read_state()
        state.update(handled_process=process, login=login_identity(), last_capture=db.utc_now(),
                     saved_tabs=len(sessions), resumable_tabs=sum(bool(s["source"] and s.get("source_path")) for s in sessions),
                     error="", restore_failed=False, restore_in_progress=False)
        write_state(state)
    config = read_config()
    was_enabled = bool(config.get("automatic_recovery"))
    try:
        manage_window_state(True)
        reload_config()
        config["automatic_recovery"] = True
        atomic_write(data_dir() / "config.json", json.dumps(config, indent=2) + "\n")
        return [install(), install_recovery(), "Ghostty native window restoration is disabled while automatic recovery is enabled."]
    except (OSError, RuntimeError, subprocess.SubprocessError):
        if not was_enabled:
            manage_window_state(False)
            reload_config()
            config["automatic_recovery"] = False
            atomic_write(data_dir() / "config.json", json.dumps(config, indent=2) + "\n")
        raise


def recover(dry_run=False) -> list[dict]:
    """Explicit retry after a reported partial recovery; running agents are skipped."""
    if platform.system() != "Darwin":
        raise RuntimeError("Automatic Ghostty recovery currently requires macOS.")
    restart = not dry_run and read_config().get("automatic_recovery")
    if restart:
        from llm_archive.scheduler import uninstall_recovery
        uninstall_recovery()  # Do not let the observer overwrite a partially restored workspace.
    try:
        rows = processes()
        process = ghostty_process(rows)
        terminals = ghostty_terminals() if process else []
        plan = restoration_plan(saved_workspace()["sessions"], terminals, live_sessions(rows, process))
        if not dry_run:
            state = read_state()
            state.update(restore_in_progress=True)
            write_state(state)
            count = open_ghostty(plan) if plan else 0
            # Re-read the process after opening Ghostty if it was previously stopped.
            state.update(handled_process=ghostty_process(processes()), last_restore=db.utc_now(),
                         restored_tabs=count, restore_failed=False, restore_in_progress=False, error="")
            write_state(state)
        return plan
    finally:
        if restart:
            from llm_archive.scheduler import install_recovery
            install_recovery()


def disable() -> str:
    if platform.system() != "Darwin":
        raise RuntimeError("Automatic Ghostty recovery currently requires macOS.")
    from llm_archive.scheduler import uninstall_recovery
    from llm_archive.ghostty_config import manage_window_state, reload_config
    message = uninstall_recovery()
    config = read_config()
    config["automatic_recovery"] = False
    atomic_write(data_dir() / "config.json", json.dumps(config, indent=2) + "\n")
    manage_window_state(False)
    reload_config()
    return message


def run():
    if platform.system() != "Darwin":
        raise RuntimeError("Automatic Ghostty recovery currently requires macOS.")
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "recovery.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another workspace recovery watcher is already running.") from None
        watcher = Watcher(login_identity())
        while read_config().get("automatic_recovery"):
            try:
                rows = processes()
                process = ghostty_process(rows)
                terminals = ghostty_terminals() if process else []
                watcher.tick(process, terminals, live_sessions(rows, process), time.monotonic())
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                watcher.persist(error=str(exc), last_check=db.utc_now())
                print(f"{db.utc_now()} {exc}", flush=True)
            time.sleep(POLL_SECONDS)
