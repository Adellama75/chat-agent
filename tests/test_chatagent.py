"""chat-agent against a fake chat; Claude, gh and git are faked. Synthetic data only."""

import json
import subprocess
from pathlib import Path

import pytest

from chatagent import Agent, AgentLoop, Msg, core, follow, loop


class FakeChat:
    platform, label, issue_label = "Testchat", "team", "testchat"
    attachments, who_is_me, names, tools = "/data/att", "", ("teambot",), []
    rules = None

    def __init__(self, data_dir: Path):
        self.data_dir, self.msgs, self.events, self.polled = data_dir, [], [], 0

    def add(self, text, own=False, to_agent=False):
        self.msgs.append(Msg(len(self.msgs) + 1, text, own, to_agent))

    def poll(self):
        self.polled += 1

    def after(self, cursor):
        return [m for m in self.msgs if m.cursor > cursor]

    def before(self, cursor, n):
        return [m for m in self.msgs if m.cursor < cursor][-n:]

    def last(self):
        return len(self.msgs)

    def render(self, msgs, to_agent=None, ids=False):
        return "\n".join(f"[Sun 25 Oct 09:{m.cursor:02d}{f' #{m.cursor}' if ids else ''}] kim:"
                         f"{' [to claude]' if to_agent and to_agent(m) else ''} {m.text}" for m in msgs)

    def side_events(self):
        out, self.events = self.events, []
        return out


A = Agent(repo="org/repo", workdir=Path("."), debounce=10, max_wait=60, github_users={"kim": "kim-gh"}, brief="We build robots.")


@pytest.fixture
def chat(tmp_path):
    return FakeChat(tmp_path / "data")


@pytest.fixture
def runs(monkeypatch):
    """Fake gh/git (succeed) and record Claude runs."""
    seen = []
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "[]", ""))
    monkeypatch.setattr(loop.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(core, "run_claude", lambda a, s, p, tools=(): seen.append((s, p, tools)) or {"result": "ok", "exit": 0})
    return seen


def test_mentions_are_whole_words():
    names = ("claude", "bot")
    assert core.mentions("@claude what's open?", names) and core.mentions("hey bot", names)
    assert not core.mentions("robots are cool", names) and not core.mentions("claudette", names)


def test_addressed_by_name_or_reply_never_own():
    assert core.addressed(Msg(1, "claude?"), ("claude",))
    assert core.addressed(Msg(1, "yes please", to_agent=True), ("claude",))
    assert not core.addressed(Msg(1, "claude said so", own=True), ("claude",))


def test_ready_after_quiet_or_max_wait_and_tracking():
    b = core.track(None, [Msg(1, "a")], now=0)
    assert not core.ready(b, 5, A) and core.ready(b, 10, A)
    b = core.track(b, [Msg(1, "a"), Msg(2, "b")], now=55)
    assert b.last == 2 and not core.ready(b, 59, A) and core.ready(b, 60, A)  # max_wait from the first


def test_system_prompt_issues_only_by_default(chat):
    s = core.system_prompt(A, chat)
    assert "Testchat chat \"team\"" in s and "org/repo" in s and "kim -> kim-gh" in s and "We build robots." in s
    assert "Label `testchat`" in s and "no way to write to the chat" in s and "teambot" in s


def test_system_prompt_with_posting_rules(chat):
    chat.rules = core.posting_rules('testchat send "<text>"')
    s = core.system_prompt(Agent(repo="o/r", workdir=Path("."), prefix="[Claude]:"), chat, coordinator=True)
    assert 'testchat send "<text>"' in s and "Always start the text with `[Claude]: `" in s
    assert "send **no reply**" in s and "How to write to the chat" in s
    assert "add no prefix" in core.system_prompt(A, chat)


def test_run_claude_hides_the_api_key_and_restricts_tools(monkeypatch, tmp_path):
    seen = {}

    class Proc:
        stdout, stderr, returncode = '{"result": "ok"}', "", 0

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(core.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd, kw=kw) or Proc())
    out = core.run_claude(Agent(repo="o/r", workdir=tmp_path, max_budget_usd=1.5), "sys", "p", ["Bash(x send:*)"])
    assert out["result"] == "ok" and "ANTHROPIC_API_KEY" not in seen["kw"]["env"] and seen["kw"]["cwd"] == tmp_path
    cmd = seen["cmd"]
    allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--max-budget-usd")]
    assert set(allowed) == {"Bash(gh issue:*)", "Read", "Grep", "Glob", "Bash(x send:*)"}


def test_first_start_pins_cursor_then_batches(chat, runs):
    chat.add("old chatter")
    agent = AgentLoop(A, chat)
    assert agent.pending() == []
    chat.add("I'll fix the login timeout by Wednesday")
    assert agent.poll(now=0) is False and agent.poll(now=20) is True
    system, prompt, _ = runs[0]
    new = prompt.split("## New messages")[1]
    assert "I'll fix the login timeout" in new and "#2]" in new and "old chatter" in prompt.split("## New messages")[0]
    assert json.loads((chat.data_dir / "agent.json").read_text())["cursor"] == 2
    assert agent.pending() == []


def test_own_posts_are_skipped_silently(chat, runs):
    agent = AgentLoop(A, chat)
    chat.add("Opened #1", own=True)
    assert agent.poll(now=0) is False and agent.cursor == 1 and runs == []


def test_mentions_and_replies_run_at_once_unless_a_coordinator_listens(chat, runs):
    agent = AgentLoop(A, chat)
    chat.add("ok", to_agent=True)
    assert agent.poll(now=0) is True
    assert "[to claude] ok" in runs[0][1]
    (chat.data_dir / core.HEARTBEAT).touch()
    chat.add("@claude and now?")
    assert agent.poll(now=1) is False  # the coordinator answers; the run waits for the batch
    assert agent.poll(now=12) is True and "send **no reply**" not in runs[1][0]  # default rules have no posting
    chat.rules = core.posting_rules("x send")
    chat.add("@claude again")
    agent.poll(now=13)
    agent.poll(now=30)
    assert "send **no reply**" in runs[2][0]


def test_pause_file_holds_runs(chat, runs):
    agent = AgentLoop(A, chat)
    (chat.data_dir / "agent.paused").touch()
    chat.add("@claude hello")
    assert agent.poll(now=0) is False and agent.poll(now=100) is False
    (chat.data_dir / "agent.paused").unlink()
    assert agent.poll(now=101) is True


def test_failed_git_pull_is_logged_and_flagged(chat, runs, monkeypatch):
    agent = AgentLoop(A, chat)
    monkeypatch.setattr(loop.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "fatal: not a git repository"))
    chat.add("@claude what's open?")
    agent.poll(now=0)
    assert "WARNING: `git pull` in the repo clone failed" in runs[0][1]
    assert any("pull_failed" in json.loads(l) for l in (chat.data_dir / "agent.log").read_text().splitlines())


def test_label_is_created_once_at_start(chat, monkeypatch, runs):
    calls = []
    monkeypatch.setattr(core.subprocess, "run", lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    AgentLoop(A, chat)
    assert calls[0][:4] == ["gh", "label", "create", "testchat"] and "--force" in calls[0]


def test_dry_run_prints_and_keeps_cursor(chat, runs, capsys):
    agent = AgentLoop(A, chat)
    chat.add("can run the tests tonight")
    assert agent.once(dry_run=True) == ""
    assert "can run the tests tonight" in capsys.readouterr().out and agent.cursor == 0 and runs == []


def test_follow_resumes_beats_and_streams_side_events(chat, monkeypatch, capsys):
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    beats = []
    real_touch = Path.touch
    monkeypatch.setattr(Path, "touch", lambda p, *a, **k: beats.append(p.name) or real_touch(p, *a, **k))
    chat.add("seed")
    follow([chat], A, agent_chat=chat, polls=1)  # starts at "now": nothing printed
    assert "coordinator.alive" in beats and not (chat.data_dir / core.HEARTBEAT).exists()
    chat.add("arrived while nobody followed")
    chat.add("own post", own=True)
    chat.events.append("[Sun 25 Oct 09:05] lee reacted 👍 to kim: seed")
    follow([chat], A, polls=1)  # no agent_chat: no heartbeat; a mention-free burst waits for debounce...
    out = capsys.readouterr().out
    assert "lee reacted" in out and "arrived" not in out
    follow([chat], None, polls=1)  # ...and without [agent] prints at once, from the saved cursor
    out = capsys.readouterr().out
    assert "#2] kim: arrived while nobody followed" in out and "own post" not in out
    assert int((chat.data_dir / "follow.cursor").read_text()) == 3


def test_agent_config_ignores_unknown_keys(tmp_path):
    a = Agent.from_dict({"repo": "o/r", "workdir": "~/x", "names": ["claude"], "save_media": True})
    assert a.workdir == Path.home() / "x" and a.names == ("claude",)
    assert Agent.from_dict(None) is None
