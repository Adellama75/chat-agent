from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass(frozen=True)
class Agent:
    """The [agent] table of a reader's config.toml: what a Claude Code run may do with each burst."""

    repo: str  # GitHub owner/name whose issues it keeps
    workdir: Path  # local clone of that repo; Claude runs there (read-only) for context
    group: str = ""  # which chat, for readers that archive several (label or id)
    model: str = "claude-sonnet-5-5"
    debounce: int = 30  # seconds of quiet after the last new message before a run
    max_wait: int = 300  # run anyway once the oldest unprocessed message is this old
    context: int = 40  # earlier messages shown for context (never acted on)
    prefix: str = ""  # messages starting with this (from the agent's account) are its own and skipped
    max_sends_per_hour: int = 8  # cap for the reader's send command, if it has one
    names: tuple[str, ...] = ("claude", "bot")  # it answers when addressed by one of these
    github_users: dict[str, str] = field(default_factory=dict)  # chat display name -> GitHub login
    claude: str = "claude"  # the Claude Code binary
    max_budget_usd: float = 0  # per-run cap passed to claude -p (0 = none)
    brief: str = ""  # who the team is and where to look in the repo; goes into the system prompt

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Agent | None":
        """Build from a parsed [agent] table, ignoring keys this version doesn't know."""
        if not raw:
            return None
        known = {f.name for f in fields(cls)}
        raw = {k: v for k, v in raw.items() if k in known}
        raw["workdir"] = Path(raw["workdir"]).expanduser()
        if "names" in raw:
            raw["names"] = tuple(raw["names"])
        return cls(**raw)
