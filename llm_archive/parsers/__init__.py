import re
from dataclasses import dataclass, field


_HOME_ALIASES = {"unbalancedparen", "projects", "(home)", ""}


def normalize_project(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[._]+", "-", name)
    name = re.sub(r"-+", "-", name)
    name = name.strip("-")
    if name in _HOME_ALIASES:
        return "(home)"
    return name or "unknown"


@dataclass
class ParsedMessage:
    role: str
    content: str
    timestamp: str


@dataclass
class ParsedConversation:
    session_id: str
    source: str
    project: str
    git_branch: str | None
    started_at: str | None
    messages: list[ParsedMessage] = field(default_factory=list)
