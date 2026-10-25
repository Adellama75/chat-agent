"""chat-agent: new messages in a team chat -> a headless Claude Code run that keeps a GitHub repo's
issues in step with what the team says. Transport-agnostic: a reader (whatsapp-reader,
discord-reader) supplies a `Chat` adapter (see chat.py) and this package does the rest."""

from .chat import Chat, Msg
from .config import Agent
from .loop import AgentLoop, follow

__all__ = ["Agent", "AgentLoop", "Chat", "Msg", "follow"]
