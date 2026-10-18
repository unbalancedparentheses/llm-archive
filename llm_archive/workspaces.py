"""Named directory/session selections; foreign-device definitions retain their identity."""
import json
import re
from pathlib import Path

from llm_archive import db
from llm_archive.journal import local_workspace
from llm_archive.paths import atomic_write, data_dir, device_id, machine_name
from llm_archive.projects import repository


def validate_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        raise ValueError("Workspace names must be 1–64 letters, digits, dots, underscores, or dashes, starting with a letter or digit.")
    return name


def save(name: str, sessions: list[dict], directories: set[str] | None = None) -> dict:
    validate_name(name)
    chosen = []
    seen = set()
    for s in sessions:
        if not local_workspace(s) or not s.get("cwd") or not Path(s["cwd"]).is_dir():
            continue
        cwd = str(Path(s["cwd"]).resolve())
        if cwd in seen or (directories is not None and cwd not in directories):
            continue
        seen.add(cwd)
        chosen.append({k: v for k, v in s.items() if k not in ("id", "message_count")})
    for cwd in sorted((directories or set()) - seen):
        if Path(cwd).is_dir():
            chosen.append({**repository(cwd), "source": "", "session_id": "", "source_path": "",
                           "origin_device": device_id(), "machine": machine_name(), "started_at": db.utc_now(),
                           "last_activity": db.utc_now(), "git_branch": ""})
    if not chosen:
        raise ValueError("No matching local directories to save. Increase --days, select another project, or use --from-tabs.")
    record = {"version": 1, "name": name, "origin_device": device_id(), "saved_at": db.utc_now(), "sessions": chosen}
    atomic_write(data_dir() / "workspaces" / f"{name}.json", json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return record


def list_workspaces() -> list[dict]:
    root = data_dir() / "workspaces"
    results = []
    for path in sorted(root.glob("**/*.json")):
        record = json.loads(path.read_text())
        selector = record["name"] if path.parent == root else f"{path.parent.name}:{record['name']}"
        results.append({**record, "selector": selector})
    return results


def load(selector: str) -> dict:
    if ":" in selector:
        device, name = selector.split(":", 1)
        validate_name(device)
        validate_name(name)
        matches = [r for r in list_workspaces() if r["name"] == name and r["origin_device"].startswith(device)]
    else:
        validate_name(selector)
        records = [r for r in list_workspaces() if r["name"] == selector]
        local = [r for r in records if r["origin_device"] == device_id()]
        matches = local or records
    if len(matches) != 1:
        raise ValueError("Workspace not found or name is ambiguous. Use llm-archive workspace list to select one.")
    record = matches[0]
    if record.get("version") != 1 or not isinstance(record.get("sessions"), list):
        raise ValueError("Unsupported workspace format")
    return record


def export_local() -> list[dict]:
    return [{k: v for k, v in r.items() if k != "selector"}
            for r in list_workspaces() if r["origin_device"] == device_id()]


def import_foreign(origin: str, records: list[dict]) -> None:
    validate_name(origin)
    for record in records:
        validate_name(record["name"])
        if record.get("origin_device") != origin or record.get("version") != 1:
            raise ValueError("Invalid foreign workspace identity or format")
        atomic_write(data_dir() / "workspaces" / origin / f"{record['name']}.json",
                     json.dumps(record, indent=2, ensure_ascii=False) + "\n")
