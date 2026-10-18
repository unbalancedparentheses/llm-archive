import os
import platform
import shutil
import subprocess
import sys
import plistlib
import shlex
from pathlib import Path


def _find_binary() -> str:
    """Find the llm-archive binary path."""
    local = Path(sys.executable).parent / "llm-archive"
    if local.is_file():
        return str(local)
    result = subprocess.run(["which", "llm-archive"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    raise RuntimeError("Install llm-archive first: python -m pip install -e .")


def _log_path() -> str:
    from llm_archive.paths import data_dir
    p = data_dir()
    p.mkdir(parents=True, exist_ok=True)
    return str(p / "ingest.log")


def install(interval: int = 120) -> str:
    if interval < 60:
        raise ValueError("The checkpoint interval must be at least 60 seconds.")
    system = platform.system()
    if system == "Darwin":
        return _install_launchd(interval)
    elif system == "Linux":
        if shutil.which("systemctl"):
            return _install_systemd(interval)
        else:
            return _install_crontab(interval)
    else:
        return _install_crontab(interval)


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


def _environment() -> dict:
    return {k: v for k, v in os.environ.items() if k in (
        "PATH", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "LLM_ARCHIVE_HOME",
    )}


def _install_launchd(interval: int = 120) -> str:
    binary = _find_binary()
    log = _log_path()
    plist_path = LAUNCHD_DIR / f"{LAUNCHD_LABEL}.plist"
    LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)

    plist_path.write_bytes(plistlib.dumps({
        "Label": LAUNCHD_LABEL, "ProgramArguments": [binary, "checkpoint"],
        "StartInterval": interval, "RunAtLoad": True,
        "EnvironmentVariables": _environment(), "StandardOutPath": log, "StandardErrorPath": log,
    }))

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
    return f"Installed launchd agent: {plist_path}\nChecks every {interval} seconds and on login.\nLog: {log}"


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


def _install_systemd(interval: int = 120) -> str:
    binary = _find_binary()
    log = _log_path()
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)

    service = f"""[Unit]
Description=llm-archive ingest

[Service]
Type=oneshot
ExecStart="{binary}" checkpoint
EnvironmentFile=-{SYSTEMD_DIR / 'llm-archive.env'}
StandardOutput=append:{log}
StandardError=append:{log}
"""

    timer = f"""[Unit]
Description=Periodic llm-archive checkpoint

[Timer]
OnStartupSec=15
OnUnitInactiveSec={interval}s

[Install]
WantedBy=timers.target
"""

    (SYSTEMD_DIR / SYSTEMD_SERVICE).write_text(service)
    (SYSTEMD_DIR / SYSTEMD_TIMER).write_text(timer)
    (SYSTEMD_DIR / "llm-archive.env").write_text("".join(
        f'{key}="{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"\n'
        for key, value in _environment().items()
    ))

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", SYSTEMD_TIMER], check=True, capture_output=True)
    subprocess.run(["systemctl", "--user", "restart", SYSTEMD_TIMER], check=True, capture_output=True)

    return f"Installed systemd timer: {SYSTEMD_DIR / SYSTEMD_TIMER}\nChecks every {interval} seconds.\nLog: {log}"


def _uninstall_systemd() -> str:
    timer_path = SYSTEMD_DIR / SYSTEMD_TIMER
    service_path = SYSTEMD_DIR / SYSTEMD_SERVICE
    if not timer_path.exists():
        return "No systemd timer found."
    subprocess.run(["systemctl", "--user", "disable", "--now", SYSTEMD_TIMER], capture_output=True)
    timer_path.unlink(missing_ok=True)
    service_path.unlink(missing_ok=True)
    (SYSTEMD_DIR / "llm-archive.env").unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return f"Removed systemd timer and service."


# --- Fallback: crontab ---

CRON_MARKER = "# llm-archive-ingest"


def _install_crontab(interval: int = 120) -> str:
    minutes = interval // 60
    if interval % 60 or minutes < 1 or minutes > 60 or 60 % minutes:
        raise ValueError("Crontab fallback needs an interval dividing one hour, such as 120 or 300 seconds.")
    binary = _find_binary()
    log = _log_path()
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in _environment().items())
    schedule = "0" if minutes == 60 else f"*/{minutes}"
    cron_line = f"{schedule} * * * * {env} {shlex.quote(binary)} checkpoint >> {shlex.quote(log)} 2>&1 {CRON_MARKER}"

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
    return f"Installed crontab entry.\nChecks every {interval} seconds.\nLog: {log}"


def _uninstall_crontab() -> str:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0 or CRON_MARKER not in result.stdout:
        return "No crontab entry found."
    lines = [l for l in result.stdout.splitlines() if CRON_MARKER not in l]
    new_crontab = "\n".join(lines) + "\n" if lines else ""
    subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
    return "Removed crontab entry."
