"""The adapter a reader implements so chat-agent can drive it.

A reader keeps its own archive (SQLite) of one chat and knows how to render it; chat-agent only
needs cursors, a few facts per message, and the rendered text. Cursors are the archive's
monotonically increasing row ids.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


@dataclass
class Msg:
    cursor: int  # archive row id: strictly increasing in arrival order
    text: str
    own: bool = False  # posted by the agent itself (skipped on the way back in)
    to_agent: bool = False  # a reply to one of the agent's own posts
    raw: Any = field(default=None, repr=False)  # the reader's own message object, handed back to render()


class Chat(Protocol):
    platform: str  # "WhatsApp", "Discord": used in prompts
    label: str  # the chat's name
    issue_label: str  # GitHub label for issues opened from this chat ("whatsapp", "discord")
    data_dir: Path  # agent.json, agent.log, agent.paused, coordinator.alive, follow.cursor live here
    attachments: str  # where saved attachments are, for the prompt
    who_is_me: str  # one sentence on who "Me" (or the account owner) is in transcripts; may be ""
    names: tuple[str, ...]  # extra names the agent answers to (e.g. a bot's @username)
    tools: list[str]  # extra --allowedTools (e.g. the reader's send command)
    rules: str | None  # the prompt's output section (how to post); None = issues only

    def poll(self) -> None:
        """Fetch what is new into the archive (no-op for readers fed by a daemon)."""

    def after(self, cursor: int) -> list[Msg]:
        """Messages stored after `cursor`, oldest first."""

    def before(self, cursor: int, n: int) -> list[Msg]:
        """The n messages before `cursor`, oldest first (context)."""

    def last(self) -> int:
        """The newest cursor in the archive (0 if empty)."""

    def render(self, msgs: list[Msg], to_agent: Callable[[Msg], bool] | None = None, ids: bool = False) -> str:
        """Transcript text: one `[Day dd Mon HH:MM]` head per message, continuations indented,
        `[to claude]` on messages `to_agent` picks, `#id` in the head with `ids`."""

    def side_events(self) -> list[str]:
        """Extra lines for `follow` since the last call (e.g. reactions); [] if none."""
