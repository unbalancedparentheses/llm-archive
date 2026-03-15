from dataclasses import dataclass, field


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
