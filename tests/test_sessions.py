"""Offline tests for the parts that are ours. No browser, no API keys, no network.

Both jev keys are read lazily inside choose()/field_text(), so a fake Agent that
never reaches them needs neither.
"""

from __future__ import annotations

import time

import pytest

from jev_browser_use_mcp import server, sessions
from jev_browser_use_mcp.sessions import STATE_KEYS, ContractDrift, SessionStore, assert_state_contract, retask


class FakeBrowser:
    def __init__(self, target="T1"):
        self.target = target


def fresh_state(**over):
    state = {
        "browser": FakeBrowser(),
        "goal": "old goal",
        "page": {"url": "https://e.test", "title": "T", "text": "hello", "screenshot": "SECRET"},
        "decision": {"request": "SECRET"},
        "history": [{"kind": "click", "label": "Go", "page_changed": True}],
        "status": "done",
        "plan": ["old goal"],
        "plan_index": 1,
        "decisions": [{"request": "SECRET"}],
        "text_calls": [{"value": "x"}],
        "elapsed_ms": 1234,
        "started_at": 99.0,
        "record": False,
    }
    state.update(over)
    return state


class FakeAgent:
    """Terminal after `ticks` calls; `raises` is thrown on the first tick instead."""

    def __init__(self, ticks=1, end="done", raises=None, **over):
        self.state = fresh_state(**over)
        self.state["status"] = "ready"
        self.pending_text = ("ctx", "text", {})
        self.browser = self.state["browser"]
        self.ticks, self.end, self.raises = ticks, end, raises
        self.closed = False

    def command(self, name):
        assert name == "tick"
        if self.raises:
            raise self.raises
        self.ticks -= 1
        self.state["history"] = self.state["history"] + [{"kind": "click", "label": "x", "page_changed": True}]
        if self.ticks <= 0:
            self.state["status"] = self.end
        return self.state

    def close(self):
        self.closed = True


# ---- the contract that protects retask() --------------------------------


def test_contract_matches_pinned_agent():
    assert_state_contract(fresh_state())
    assert len(STATE_KEYS) == 13


def test_added_key_is_caught():
    """The dangerous direction: a new counter beside `decisions` would gate task two."""
    with pytest.raises(ContractDrift, match="added=..new_counter"):
        assert_state_contract(fresh_state(new_counter=0))


def test_removed_key_is_caught():
    state = fresh_state()
    del state["history"]
    with pytest.raises(ContractDrift, match="removed=..history"):
        assert_state_contract(state)


def test_retask_clears_everything_that_poisons_the_next_goal():
    agent = FakeAgent()
    agent.state["status"] = "done"
    retask(agent, "new goal")
    assert agent.state["goal"] == "new goal"
    assert agent.state["plan"] == ["new goal"] and agent.state["plan_index"] == 0
    assert agent.state["status"] == "ready", "a terminal status makes predict refuse forever"
    assert agent.state["history"] == [], "stale history reads as satisfied steps of the new goal"
    assert agent.state["decisions"] == [], "decisions is the budget counter"
    assert agent.state["decision"] is None and agent.state["started_at"] is None
    assert agent.pending_text is None, "an instance attribute no key-set assertion can cover"
    assert agent.state["browser"] is not None and agent.state["page"], "the live tab must survive"


def test_retask_refuses_on_drift():
    agent = FakeAgent()
    agent.state["surprise"] = 1
    with pytest.raises(ContractDrift):
        retask(agent, "new goal")


# ---- session store -------------------------------------------------------


def test_cap_evicts_oldest_idle_and_closes_it():
    store = SessionStore()
    first = store.add(FakeAgent(), "a")
    time.sleep(0.01)
    for goal in "bcd":
        store.add(FakeAgent(), goal)
    assert len(store) == sessions.MAX_SESSIONS
    assert store.get(first.id) is None and first.agent.closed


def test_reaper_drops_idle_sessions():
    store = SessionStore()
    session = store.add(FakeAgent(), "a")
    session.last_used -= sessions.IDLE_TTL_S + 1
    assert store.reap_idle() == 1
    assert store.get(session.id) is None and session.agent.closed


def test_close_all_is_idempotent():
    store = SessionStore()
    store.add(FakeAgent(), "a")
    store.close_all()
    store.close_all()
    assert len(store) == 0


# ---- argument validation -------------------------------------------------


@pytest.fixture
def runner(monkeypatch):
    run = server.Runner()
    monkeypatch.setattr(run, "ensure_browser", lambda: None)
    monkeypatch.setattr(run, "daemon_alive", lambda: True)
    monkeypatch.setattr(run, "build_session", lambda url, goal: run.store.add(FakeAgent(), goal))
    return run


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"url": "https://e.test", "session_id": "x"}, "exactly one"),
        ({}, "exactly one"),
        ({"url": "https://e.test"}, "`goal` is required"),
    ],
)
def test_rejects_bad_call_shapes(runner, kwargs, message):
    with pytest.raises(ValueError, match=message):
        runner.run_task(kwargs.get("url"), kwargs.get("goal"), kwargs.get("session_id"), 50)


def test_rejects_file_scheme(runner):
    with pytest.raises(ValueError, match="scheme file is not allowed"):
        runner.run_task("file:///etc/passwd", "read it", None, 50)


def test_allows_loopback(runner):
    body = runner.run_task("http://localhost:3000", "check it", None, 50)
    assert body["outcome"] == "agent_claims_done"


def test_unknown_session_is_not_an_error(runner):
    assert runner.run_task(None, None, "nope", 50)["outcome"] == "session_expired"


def test_same_session_concurrent_call_is_busy(runner):
    body = runner.run_task("https://e.test", "a", None, 50)
    session = runner.store.get(body["session_id"])
    session.lock.acquire()
    try:
        with pytest.raises(ValueError, match="already running"):
            runner.run_task(None, None, session.id, 50)
    finally:
        session.lock.release()


# ---- envelope ------------------------------------------------------------


def test_envelope_never_leaks_internals(runner):
    body = runner.run_task("https://e.test", "a", None, 50)
    blob = repr(body)
    assert "SECRET" not in blob, "decision.request, raw_answers and screenshots must be stripped"
    assert body["verified"] is False
    assert "done" not in {body["outcome"]}, "the bare word 'done' must never be an outcome value"
    assert body["outcome"] == "agent_claims_done"
    assert "page_text_untrusted" in body and "text" not in body


def test_text_is_capped(runner, monkeypatch):
    monkeypatch.setattr(
        runner,
        "build_session",
        lambda url, goal: runner.store.add(FakeAgent(page={"url": "u", "title": "t", "text": "x" * 9000}), goal),
    )
    body = runner.run_task("https://e.test", "a", None, 50)
    assert len(body["page_text_untrusted"]) == server.TEXT_CAP


def test_deadline_returns_evidence_and_stays_resumable(runner, monkeypatch):
    monkeypatch.setattr(runner, "build_session", lambda url, goal: runner.store.add(FakeAgent(ticks=10**6), goal))
    body = runner.run_task("https://e.test", "a", None, 1)
    assert body["outcome"] == "deadline_exceeded"
    assert body["resumable"] is True and body["session_id"]
    assert runner.store.get(body["session_id"]) is not None


def test_blocked_is_not_resumable_and_is_dropped(runner, monkeypatch):
    monkeypatch.setattr(runner, "build_session", lambda url, goal: runner.store.add(FakeAgent(end="blocked"), goal))
    body = runner.run_task("https://e.test", "a", None, 50)
    assert body["outcome"] == "blocked"
    # jev bricks a blocked Agent permanently, but retask() revives it for a NEW goal.
    assert body["resumable"] is True


def test_setup_failure_passes_the_message_through(runner, monkeypatch):
    def boom():
        raise RuntimeError("chrome-not-running: start Chrome, then retry")

    monkeypatch.setattr(runner, "ensure_browser", boom)
    body = runner.run_task("https://e.test", "a", None, 50)
    assert body["outcome"] == "setup_failed"
    assert "chrome-not-running" in body["error"], "these are user instructions, not stack noise"


# ---- failure classification ----------------------------------------------


def outcome_for(runner, error, monkeypatch, daemon=True, **agent_kw):
    monkeypatch.setattr(runner, "daemon_alive", lambda: daemon)
    monkeypatch.setattr(
        runner, "build_session", lambda url, goal: runner.store.add(FakeAgent(raises=error, **agent_kw), goal)
    )
    return runner.run_task("https://e.test", "a", None, 50)["outcome"]


def test_dead_daemon_means_ambiguous(runner, monkeypatch):
    assert outcome_for(runner, ConnectionRefusedError(61, "refused"), monkeypatch, daemon=False) == "ambiguous_mutation"


def test_dropdown_marker_means_ambiguous(runner, monkeypatch):
    error = RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
    assert outcome_for(runner, error, monkeypatch) == "ambiguous_mutation"


def test_half_written_history_means_ambiguous(runner, monkeypatch):
    """Window B: act() executed and appended, then the post-action observe threw.

    jev writes the history entry before observing, so the row is left with
    page_changed=None -- the action landed, its effect is unknown.
    """
    agent = FakeAgent()

    def append_then_die(name):
        agent.state["history"].append({"kind": "click", "label": "x", "page_changed": None})
        raise RuntimeError("{'code': -32001, 'message': 'Session with given id not found.'}")

    agent.command = append_then_die
    monkeypatch.setattr(runner, "daemon_alive", lambda: True)
    monkeypatch.setattr(runner, "build_session", lambda url, goal: runner.store.add(agent, goal))
    assert runner.run_task("https://e.test", "a", None, 50)["outcome"] == "ambiguous_mutation"


def test_live_daemon_with_dead_tab_is_clean(runner, monkeypatch):
    error = RuntimeError("{'code': -32001, 'message': 'Session with given id not found.'}")
    assert outcome_for(runner, error, monkeypatch) == "browser_lost"


def test_budget_is_its_own_outcome(runner, monkeypatch):
    assert outcome_for(runner, ValueError("Stopped at the 60-action demo budget"), monkeypatch) == "budget_exhausted"


def test_model_failure_retries_then_gives_up(runner, monkeypatch):
    error = RuntimeError("Model connection failed; no action executed.")
    assert outcome_for(runner, error, monkeypatch) == "error"


def test_model_failure_is_retried_and_can_succeed(runner, monkeypatch):
    agent = FakeAgent(ticks=2)
    calls = {"n": 0}
    original = agent.command

    def flaky(name):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Model provider returned HTTP 500; no action executed.")
        return original(name)

    agent.command = flaky
    monkeypatch.setattr(runner, "build_session", lambda url, goal: runner.store.add(agent, goal))
    body = runner.run_task("https://e.test", "a", None, 50)
    assert body["outcome"] == "agent_claims_done" and calls["n"] == 3
