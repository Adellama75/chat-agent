# chat-agent

Team chat → headless Claude Code → GitHub issues. Each burst of new messages in one chat becomes one
`claude -p` run that opens, comments on and closes issues for the tasks, blockers and decisions the team
states, and (if the chat's reader can post) posts one short update or answers when addressed.

chat-agent doesn't talk to any chat service itself. A reader archives the chat and hands chat-agent a small
adapter (`chatagent.Chat`, see [src/chatagent/chat.py](src/chatagent/chat.py)):

- [whatsapp-reader](https://github.com/Adellama75/whatsapp-reader): `whatsapp-reader agent` / `follow`
  (issues only: the bridge never posts)
- [discord-reader](https://github.com/Adellama75/discord-reader): `discord-reader agent` / `follow`
  (a bot account, so it can post)

What it does, for every reader alike:
- batching: a run starts `debounce` s after the last new message, or `max_wait` s after the first; a message
  that names the agent or replies to one of its posts runs at once;
- one shared prompt ([prompt.md](src/chatagent/prompt.md)), posting rules ([posting.md](src/chatagent/posting.md))
  and chat style ([chat_style.md](src/chatagent/chat_style.md));
- `follow`: streams bursts (with message ids) to an interactive Claude "coordinator" session, resumes from
  `data/follow.cursor`, and keeps `data/coordinator.alive` fresh so the service leaves replies to that session;
- `git pull` of the repo clone before each run, with a warning in the prompt if it fails;
- the issue label is created if missing; runs bill the `claude` login (API keys are hidden from `claude -p`);
- `data/agent.log` records every run; `touch data/agent.paused` holds runs.

## Using it from a reader

Readers find chat-agent as a sibling checkout: clone it next to the reader (`~/dev/chat-agent` beside
`~/dev/whatsapp-reader`), and `uv sync` in the reader picks it up (editable). The `[agent]` table of the
reader's `config.toml` is `chatagent.Agent`.

## Tests

`uv run pytest` runs the loop against a fake chat; Claude, `gh` and `git` are faked.

## License

AGPL-3.0-or-later.
