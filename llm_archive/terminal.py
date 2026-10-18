"""Restore recent project directories through Ghostty's native AppleScript API."""
import platform
import shutil
import subprocess
from pathlib import Path

from llm_archive.journal import local_session, local_workspace, resume_command


def reopen_plan(sessions: list[dict], resume=False, projects_root: Path | None = None) -> list[dict]:
    plan = []
    seen = set()
    for s in sessions:
        cwd = Path(s["cwd"]) if s.get("cwd") else None
        mapped = False
        if (cwd is None or not cwd.is_dir() or not local_workspace(s)) and projects_root:
            # A worktree may be a sibling directory. Prefer its recorded directory name.
            candidate = projects_root / (cwd.name if cwd else s["project"])
            if not candidate.is_dir():
                candidate = projects_root / s["project"]
            if candidate.is_dir():
                cwd = candidate
                mapped = True
        path = str(cwd.resolve()) if cwd and cwd.is_dir() else None
        key = path or (s.get("machine"), s.get("cwd"), s["project"])
        if key in seen:
            continue
        seen.add(key)
        reason = ""
        command = ""
        if not path:
            reason = "Directory missing; use --projects-root to locate your local checkouts."
        elif not local_workspace(s) and not mapped:
            reason = "Recorded on another computer; use --projects-root to select local checkouts."
            path = None
        elif resume and not local_session(s):
            reason = "Opening directory only; original session is unavailable on this computer."
        elif resume:
            command = resume_command(s)
        plan.append({"project": s["project"], "cwd": path, "command": command, "note": reason})
    return plan


def _apple_string(text: str) -> str:
    # Paths and names are data, never AppleScript source or terminal commands.
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'


def ghostty_script(plan: list[dict]) -> str:
    lines = ['tell application "Ghostty"', 'activate', 'set targetWindow to missing value',
             'set openedCount to 0', 'if (count of windows) > 0 then set targetWindow to front window']
    for p in plan:
        if not p["cwd"]:
            continue
        lines += ['set alreadyOpen to false']
        if not p.get("new_tab"):
            lines += ['repeat with existingTerminal in terminals', 'try',
                      f'if working directory of existingTerminal is {_apple_string(p["cwd"])} then set alreadyOpen to true',
                      'end try', 'end repeat']
        lines += ['if not alreadyOpen then', 'set cfg to new surface configuration',
                  f'set initial working directory of cfg to {_apple_string(p["cwd"])}']
        if p["command"]:
            # Only our fixed resume command is ever sent, not recorded shell commands.
            lines += [f'set initial input of cfg to {_apple_string(p["command"] + chr(10))}']
        lines += ['if targetWindow is missing value then',
                  'set targetWindow to new window with configuration cfg',
                  'set targetTab to selected tab of targetWindow', 'else',
                  'set targetTab to new tab in targetWindow with configuration cfg', 'end if',
                  f'perform action {_apple_string("set_tab_title:" + p["project"])} on focused terminal of targetTab',
                  'set openedCount to openedCount + 1', 'end if']
    return "\n".join([*lines, 'end tell', 'return openedCount', ''])


def ghostty_directories() -> set[str]:
    """Read live directories without activating Ghostty or creating surfaces."""
    if platform.system() != "Darwin":
        raise RuntimeError("Reading Ghostty tabs requires macOS.")
    script = '''if application "Ghostty" is not running then return ""
tell application "Ghostty"
    set paths to {}
    repeat with term in terminals
        set p to working directory of term
        if p is not missing value and p is not "" then set end of paths to p
    end repeat
    set AppleScript's text item delimiters to character id 30
    return paths as text
end tell'''
    result = subprocess.run(["osascript", "-"], input=script, text=True, capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError("Could not read Ghostty directories: " + result.stderr.strip())
    return {str(Path(p).resolve()) for p in result.stdout.rstrip("\n").split("\x1e") if p}


def open_ghostty(plan: list[dict]) -> int:
    if platform.system() != "Darwin" or not shutil.which("osascript"):
        raise RuntimeError("Ghostty tab restoration requires macOS and Ghostty 1.3+. Use --dry-run to inspect the plan.")
    count = sum(bool(p["cwd"]) for p in plan)
    if not count:
        return 0
    result = subprocess.run(["osascript", "-"], input=ghostty_script(plan), text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError("Could not restore Ghostty tabs. Requires Ghostty 1.3+ and macOS Automation permission. " + result.stderr.strip())
    try:
        return int(result.stdout.strip())
    except ValueError:
        raise RuntimeError("Ghostty returned an unexpected restoration result") from None
