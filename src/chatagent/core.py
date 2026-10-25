"""The pieces of the agent that don't depend on a loop: addressing, batching, prompts, the Claude run."""

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .chat import Chat, Msg
from .config import Agent

HERE = Path(__file__).parent
HEARTBEAT = "coordinator.alive"

NO_POSTING = """## Your output
The issue changes are your whole output: you have no way to write to the chat, and the team sees the
issues on GitHub. If a new message addresses you by name ({names}) or is marked `[to claude]`, record any
task it contains as usual and otherwise leave it."""

COORDINATOR_NOTE = (
    "- A coordinator Claude session is listening to this chat right now and answers anything addressed "
    "to you. In this run send **no reply**; only the one summary after changing issues, if any."
)


def mentions(text: str, names: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<![\w@])@?{re.escape(n)}(?![\w-])", text, re.IGNORECASE) for n in names if n)


def addressed(m: Msg, names: tuple[str, ...]) -> bool:
    """Named, or replying to the agent: either way it is talking to the agent."""
    return not m.own and (m.to_agent or mentions(m.text, names))


@dataclass
class Batch:
    first: int  # cursor of the oldest unprocessed message
    last: int
    first_seen: float  # monotonic time it was noticed
    last_seen: float


def ready(b: Batch, now: float, a: Agent) -> bool:
    return now - b.last_seen >= a.debounce or now - b.first_seen >= a.max_wait


def urgent(new: list[Msg], names: tuple[str, ...]) -> bool:
    """Someone addressed the agent: don't wait for the chat to go quiet."""
    return any(addressed(m, names) for m in new)


def track(batch: Batch | None, new: list[Msg], now: float) -> Batch:
    """Start a batch at the first new message, or extend it when more arrive."""
    if batch is None or new[0].cursor != batch.first:
        return Batch(new[0].cursor, new[-1].cursor, now, now)
    if new[-1].cursor != batch.last:
        batch.last, batch.last_seen = new[-1].cursor, now
    return batch


def coordinator_alive(data_dir: Path, max_age: float = 120) -> bool:
    """`follow` touches data/coordinator.alive while an interactive Claude session is listening to
    the chat. While it is fresh, the agent leaves replies to that session and only keeps the issues."""
    try:
        return time.time() - (data_dir / HEARTBEAT).stat().st_mtime < max_age
    except FileNotFoundError:
        return False


def prefix_rule(prefix: str) -> str:
    if not prefix:
        return "It appears under the bot's own name; add no prefix."
    return f"Always start the text with `{prefix} `; posts carrying it are skipped on the way back in."


def posting_rules(send: str) -> str:
    """The output section for a reader that can post: `send` is how Claude calls its send command,
    e.g. `discord-reader send "<text>"`. Placeholders are filled in by system_prompt()."""
    return (HERE / "posting.md").read_text().replace("{send}", send)


def system_prompt(a: Agent, chat: Chat, coordinator: bool = False) -> str:
    names = a.names + tuple(chat.names)
    fill = dict(
        platform=chat.platform, chat=chat.label, repo=a.repo, label=chat.issue_label, attachments=chat.attachments,
        github_users=", ".join(f"{k} -> {v}" for k, v in a.github_users.items()) or "(none)",
        who_is_me=chat.who_is_me, brief=a.brief.strip(), names=", ".join(n for n in names if n),
        prefix_rule=prefix_rule(a.prefix), example=f"{a.prefix} Opened".strip(),
        coordinator=COORDINATOR_NOTE if coordinator else "", chat_style=(HERE / "chat_style.md").read_text(),
    )
    rules = (chat.rules or NO_POSTING).format(**fill)
    return (HERE / "prompt.md").read_text().format(**fill, rules=rules)


def user_prompt(chat: Chat, context: str, new: str, issues: str, now: datetime) -> str:
    return (
        f"Chat: {chat.label} ({chat.platform}). Now: {now:%a %d %b %Y %H:%M}.\n\n"
        f"## Earlier messages (context only, already handled)\n{context or '(none)'}\n\n"
        f"## New messages (act on these)\n{new}\n\n"
        f"## Issues in the repo (gh issue list --state all)\n{issues or '(none)'}\n"
    )


def list_issues(a: Agent) -> str:
    cmd = ["gh", "issue", "list", "--repo", a.repo, "--state", "all", "--limit", "60",
           "--json", "number,title,state,labels,assignees,updatedAt"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        return f"(gh issue list failed: {out.stderr.strip()})"
    lines = []
    for i in json.loads(out.stdout or "[]"):
        who = ",".join(x["login"] for x in i.get("assignees", [])) or "-"
        labels = ",".join(x["name"] for x in i.get("labels", []))
        lines.append(f"#{i['number']} [{i['state']}] {i['title']} (assignee {who}; {labels}; updated {i['updatedAt'][:10]})")
    return "\n".join(lines)


def ensure_label(a: Agent, label: str) -> str | None:
    """Create the issue label if the repo lacks it. Returns gh's error, if any (--force: no-op if it exists)."""
    out = subprocess.run(["gh", "label", "create", label, "--repo", a.repo, "--force",
                          "--description", "Tracked from the team chat by chat-agent"],
                         capture_output=True, text=True)
    return out.stderr.strip() if out.returncode else None


def run_claude(a: Agent, system: str, prompt: str, tools: list[str] = ()) -> dict:
    """One headless Claude Code run in the repo clone. Returns the parsed JSON result."""
    cmd = [
        a.claude, "-p", "--model", a.model, "--output-format", "json",
        "--append-system-prompt", system,
        "--allowedTools", "Bash(gh issue:*)", "Read", "Grep", "Glob", *tools,
    ]
    if a.max_budget_usd:
        cmd += ["--max-budget-usd", str(a.max_budget_usd)]
    # Claude Code prefers an API key over the claude.ai login; hide any key (e.g. one a reader's digest
    # feature uses) so the run bills the subscription.
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    started = time.monotonic()
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=a.workdir, timeout=1800, env=env)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result = {"result": proc.stdout}
    result["exit"] = proc.returncode
    result["stderr"] = proc.stderr[-2000:]
    result["seconds"] = round(time.monotonic() - started, 1)
    return result
