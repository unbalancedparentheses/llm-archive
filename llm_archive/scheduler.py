import os
import platform
import shutil
import subprocess
from pathlib import Path


def _find_binary() -> str:
    """Find the llm-archive binary path."""
    result = subprocess.run(["which", "llm-archive"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    return "llm-archive"


def _log_path() -> str:
    p = Path.home() / ".local" / "share" / "llm-archive"
    p.mkdir(parents=True, exist_ok=True)
    return str(p / "ingest.log")


def install() -> str:
    system = platform.system()
    if system == "Darwin":
        return _install_launchd()
    elif system == "Linux":
        if shutil.which("systemctl"):
            return _install_systemd()
        else:
            return _install_crontab()
    else:
        return _install_crontab()


def uninstall() -> str:
    system = platform.system()
    if system == "Darwin":
        return _uninstall_launchd()
    elif system == "Linux":
        if shutil.which("systemctl"):
            return _uninstall_systemd()
        else:
            return _uninstall_crontab()
    else:
        return _uninstall_crontab()


# --- macOS launchd ---

LAUNCHD_LABEL = "com.llm-archive.ingest"
LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"


def _install_launchd() -> str:
    binary = _find_binary()
    log = _log_path()
    plist_path = LAUNCHD_DIR / f"{LAUNCHD_LABEL}.plist"
    LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{binary}</string>
        <string>ingest</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>6</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{log}</string>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
"""
    plist_path.write_text(plist)

    # Unload first if already loaded
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True,
    )
    subprocess.run(
        ["launchctl", "load", str(plist_path)],
        check=True,
        capture_output=True,
    )
    return f"Installed launchd agent: {plist_path}\nRuns daily at 06:00 and on login.\nLog: {log}"


def _uninstall_launchd() -> str:
    plist_path = LAUNCHD_DIR / f"{LAUNCHD_LABEL}.plist"
    if not plist_path.exists():
        return "No launchd agent found."
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    plist_path.unlink()
    return f"Removed launchd agent: {plist_path}"


# --- Linux systemd ---

SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
SYSTEMD_SERVICE = "llm-archive-ingest.service"
SYSTEMD_TIMER = "llm-archive-ingest.timer"


def _install_systemd() -> str:
    binary = _find_binary()
    log = _log_path()
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)

    service = f"""[Unit]
Description=llm-archive ingest

[Service]
Type=oneshot
ExecStart={binary} ingest
StandardOutput=append:{log}
StandardError=append:{log}
"""

    timer = """[Unit]
Description=Daily llm-archive ingest

[Timer]
OnCalendar=*-*-* 06:00:00
Persistent=true

[Install]
WantedBy=timers.target
"""

    (SYSTEMD_DIR / SYSTEMD_SERVICE).write_text(service)
    (SYSTEMD_DIR / SYSTEMD_TIMER).write_text(timer)

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", SYSTEMD_TIMER], check=True, capture_output=True)

    return f"Installed systemd timer: {SYSTEMD_DIR / SYSTEMD_TIMER}\nRuns daily at 06:00.\nLog: {log}"


def _uninstall_systemd() -> str:
    timer_path = SYSTEMD_DIR / SYSTEMD_TIMER
    service_path = SYSTEMD_DIR / SYSTEMD_SERVICE
    if not timer_path.exists():
        return "No systemd timer found."
    subprocess.run(["systemctl", "--user", "disable", "--now", SYSTEMD_TIMER], capture_output=True)
    timer_path.unlink(missing_ok=True)
    service_path.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return f"Removed systemd timer and service."


# --- Fallback: crontab ---

CRON_MARKER = "# llm-archive-ingest"


def _install_crontab() -> str:
    binary = _find_binary()
    log = _log_path()
    cron_line = f"0 6 * * * {binary} ingest >> {log} 2>&1 {CRON_MARKER}"

    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = result.stdout if result.returncode == 0 else ""

    if CRON_MARKER in existing:
        # Replace existing entry
        lines = [l for l in existing.splitlines() if CRON_MARKER not in l]
        lines.append(cron_line)
    else:
        lines = existing.splitlines() + [cron_line]

    new_crontab = "\n".join(lines) + "\n"
    subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
    return f"Installed crontab entry.\nRuns daily at 06:00.\nLog: {log}"


def _uninstall_crontab() -> str:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0 or CRON_MARKER not in result.stdout:
        return "No crontab entry found."
    lines = [l for l in result.stdout.splitlines() if CRON_MARKER not in l]
    new_crontab = "\n".join(lines) + "\n" if lines else ""
    subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
    return "Removed crontab entry."
