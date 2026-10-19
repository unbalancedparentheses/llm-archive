"""Coordinate native window restoration without editing XDG/Nix configuration."""
import subprocess
from pathlib import Path

from llm_archive.paths import atomic_write

CONFIG = Path.home() / "Library/Application Support/com.mitchellh.ghostty/config"
BLOCK = "\n# BEGIN llm-archive automatic recovery\nwindow-save-state = never\n# END llm-archive automatic recovery\n"


def manage_window_state(enabled: bool) -> None:
    # The macOS-specific file loads after XDG config. Only our marked block is
    # added/removed; existing settings and subsequent user edits are preserved.
    if CONFIG.is_symlink():
        raise RuntimeError(f"Cannot manage Ghostty recovery through a symlinked config: {CONFIG}")
    text = CONFIG.read_text() if CONFIG.exists() else ""
    if "# BEGIN llm-archive automatic recovery" in text and BLOCK not in text:
        raise RuntimeError(f"The llm-archive block was edited in {CONFIG}; restore or remove that block before changing recovery.")
    updated = text.replace(BLOCK, "") + (BLOCK if enabled else "")
    if updated == text:
        return
    if updated:
        atomic_write(CONFIG, updated)
    else:
        CONFIG.unlink(missing_ok=True)


def reload_config() -> None:
    script = '''if application "Ghostty" is not running then return
tell application "Ghostty"
    if (count of terminals) > 0 then perform action "reload_config" on first terminal
end tell'''
    result = subprocess.run(["osascript", "-"], input=script, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Could not reload Ghostty configuration: " + result.stderr.strip())
