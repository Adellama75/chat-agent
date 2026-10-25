"""The service loop (`<reader> agent`) and the coordinator stream (`<reader> follow`).

Push or pull, the shape is the same: each poll asks the chat for messages past the cursor. A burst
is batched: the run starts `debounce` seconds after the last new message, or `max_wait` seconds
after the first, whichever comes first; a message that addresses the agent runs at once, unless a
coordinator session is reading `follow` (then that session answers and the run waits for the
batch). The agent's own posts are skipped on the way back in, so it can't talk to itself. Touch
data/agent.paused to stop runs without stopping the service.
"""

import json
import signal
import subprocess
import sys
import time
from datetime import datetime

from . import core
from .chat import Chat, Msg
from .config import Agent


class AgentLoop:
    def __init__(self, a: Agent, chat: Chat):
        self.a, self.chat = a, chat
        self.names = a.names + tuple(chat.names)
        d = chat.data_dir
        d.mkdir(parents=True, exist_ok=True)
        self.state_file, self.log_file, self.pause_file = d / "agent.json", d / "agent.log", d / "agent.paused"
        self.batch: core.Batch | None = None
        chat.poll()
        if self.state_file.exists():
            self.cursor = json.loads(self.state_file.read_text()).get("cursor", 0)
        else:
            self.cursor = chat.last()  # first start: only messages from now on
            self._save_cursor()
        if err := core.ensure_label(a, chat.issue_label):
            self._log(label_failed=err[-300:])

    def _save_cursor(self) -> None:
        self.state_file.write_text(json.dumps({"cursor": self.cursor, "saved": datetime.now().isoformat()}))

    def _log(self, **fields) -> None:
        with self.log_file.open("a") as f:
            f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **fields}, ensure_ascii=False) + "\n")

    def pending(self) -> list[Msg]:
        """New messages past the cursor, minus the agent's own posts."""
        return [m for m in self.chat.after(self.cursor) if not m.own]

    def poll(self, now: float) -> bool:
        """One poll: fetch, then maybe run. Returns True if a run happened."""
        try:
            self.chat.poll()
        except Exception as e:  # network trouble: log and try again next poll
            self._log(error=repr(e))
            return False
        new = self.chat.after(self.cursor)
        if not new:
            return False
        if all(m.own for m in new):
            self.cursor = new[-1].cursor  # only our own posts: skip them silently
            self._save_cursor()
            return False
        self.batch = core.track(self.batch, new, now)
        alive = core.coordinator_alive(self.chat.data_dir)
        if not core.ready(self.batch, now, self.a) and not (core.urgent(new, self.names) and not alive):
            return False
        if self.pause_file.exists():
            return False
        self.run(new)
        return True

    def build(self, new: list[Msg]) -> tuple[str, str]:
        acted = [m for m in new if not m.own]
        context = self.chat.before(new[0].cursor, self.a.context)
        system = core.system_prompt(self.a, self.chat, core.coordinator_alive(self.chat.data_dir))
        prompt = core.user_prompt(
            self.chat,
            self.chat.render(context),
            self.chat.render(acted, to_agent=lambda m: m.to_agent, ids=True),
            core.list_issues(self.a),
            datetime.now(),
        )
        return system, prompt

    def run(self, new: list[Msg], dry_run: bool = False) -> dict | None:
        pull = subprocess.run(["git", "-C", str(self.a.workdir), "pull", "-q", "--ff-only"],
                              capture_output=True, text=True)
        system, prompt = self.build(new)
        if pull.returncode != 0:
            # A blocked pull once left a clone 55 commits stale without anyone noticing.
            self._log(pull_failed=pull.stderr.strip()[-500:])
            prompt += ("\n\nWARNING: `git pull` in the repo clone failed, so it may be out of date. "
                       f"Say so before relying on repo contents.\ngit: {pull.stderr.strip()[-300:]}")
        if dry_run:
            print(system, "\n=====\n", prompt)
            return None
        result = core.run_claude(self.a, system, prompt, self.chat.tools)
        self.cursor, self.batch = new[-1].cursor, None
        self._save_cursor()
        self._log(cursors=[new[0].cursor, new[-1].cursor], n=len(new), exit=result.get("exit"),
                  seconds=result.get("seconds"), cost=result.get("total_cost_usd"),
                  result=(result.get("result") or "")[-1500:], stderr=result.get("stderr") or "")
        return result

    def once(self, dry_run: bool = False) -> str:
        """Process what is pending now (`<reader> agent --once / --dry-run`)."""
        new = self.chat.after(self.cursor)
        if not [m for m in new if not m.own]:
            return "nothing new since the cursor"
        result = self.run(new, dry_run=dry_run)
        return "" if result is None else result.get("result", "")

    def serve(self, interval: float, polls: int | None = None) -> None:
        n = 0
        while polls is None or n < polls:
            n += 1
            try:
                self.poll(time.monotonic())
            except Exception as e:  # keep the service alive; the next poll retries
                self._log(error=repr(e))
            time.sleep(interval)


def follow(chats: list[Chat], a: Agent | None, agent_chat: Chat | None = None, after: int | None = None,
           interval: float = 2, polls: int | None = None, fetch: bool = False, prefix_labels: bool = False) -> None:
    """Stream each burst of new messages to stdout for a coordinator session, with message ids.

    Same batching as the agent (no [agent]: print at once). The cursor is saved in data/follow.cursor,
    so a restarted follow resumes where the last one stopped instead of dropping what arrived in between.
    While `agent_chat` is among the chats it keeps data/coordinator.alive fresh, so the agent service
    leaves replies to whoever reads this. `fetch` polls the source too (when no service does).
    Stops cleanly on SIGHUP/SIGTERM (ssh dropped)."""
    a = a or Agent(repo="", workdir=".", debounce=0, max_wait=0)
    data_dir = chats[0].data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    beat = data_dir / core.HEARTBEAT if agent_chat is not None and agent_chat in chats else None
    cursor_file = data_dir / "follow.cursor"
    if after is not None:
        start = after
    elif cursor_file.exists() and cursor_file.read_text().strip().isdigit():
        start = int(cursor_file.read_text().strip())
    else:
        start = max(c.last() for c in chats)
    cursors = {id(c): start for c in chats}
    batches: dict[int, core.Batch] = {}
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(0))
    n = 0
    try:
        while polls is None or n < polls:
            n += 1
            if beat:
                beat.touch()
            now = time.monotonic()
            for c in chats:
                if fetch:
                    try:
                        c.poll()
                    except Exception as e:
                        print(f"(fetch failed: {e!r})", flush=True)
                label = f"[{c.label}] " if prefix_labels else ""
                for line in c.side_events():
                    print(label + line, flush=True)
                fresh = c.after(cursors[id(c)])
                new = [m for m in fresh if not m.own]
                if not new:
                    cursors[id(c)] = max(cursors[id(c)], c.last())  # only our own posts, or nothing
                    continue
                b = batches[id(c)] = core.track(batches.get(id(c)), new, now)
                if core.ready(b, now, a) or core.urgent(new, a.names + tuple(c.names)):
                    for line in c.render(new, to_agent=lambda m: m.to_agent, ids=True).splitlines():
                        print(label + line, flush=True)
                    cursors[id(c)] = fresh[-1].cursor  # past any own posts too
                    del batches[id(c)]
            cursor_file.write_text(str(min(cursors.values())))
            time.sleep(interval)
    finally:
        if beat:
            beat.unlink(missing_ok=True)
