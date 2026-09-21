"""Offline tests for the parts that are ours. No browser, no API keys, no network.

Both jev keys are read lazily inside choose()/field_text(), so a fake Agent that
never reaches them needs neither.
"""

from __future__ import annotations

import time

import pytest

from jev_browser_use_mcp import chrome, server, sessions
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


def test_envelope_never_leaks_internals(runner, monkeypatch):
    """SECRET is planted in every field the design says must be stripped."""
    page = {
        "url": "https://e.test",
        "title": "T",
        "text": "hello",
        "screenshot": "SECRET-shot",
        "guards": "SECRET-guards",
        "marker": "SECRET-marker",
    }
    agent = FakeAgent(
        page=page,
        decision={"request": "SECRET-req", "raw_answers": "SECRET-raw"},
        decisions=[{"request": "SECRET-req2", "raw_answers": "SECRET-raw2"}],
    )
    monkeypatch.setattr(runner, "build_session", lambda url, goal: runner.store.add(agent, goal))
    body = runner.run_task("https://e.test", "a", None, 50)

    assert "SECRET" not in repr(body), "the envelope is a whitelist; nothing internal may ride along"
    assert 99.0 not in body.values(), "started_at is a raw perf_counter float, meaningless as JSON"
    assert body["verified"] is False
    assert body["outcome"] != "done", "the bare word 'done' must never be an outcome value"
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


def test_blocked_stays_resumable_for_a_new_goal(runner, monkeypatch):
    """jev bricks a blocked Agent permanently, but retask() revives it for a NEW goal."""
    monkeypatch.setattr(runner, "build_session", lambda url, goal: runner.store.add(FakeAgent(end="blocked"), goal))
    body = runner.run_task("https://e.test", "a", None, 50)
    assert body["outcome"] == "blocked"
    assert body["resumable"] is True and runner.store.get(body["session_id"]) is not None


def test_unresumable_outcomes_drop_the_session(runner, monkeypatch):
    agent = FakeAgent(raises=RuntimeError("Dropdown execution was not confirmed; inspect before retrying."))
    monkeypatch.setattr(runner, "build_session", lambda url, goal: runner.store.add(agent, goal))
    body = runner.run_task("https://e.test", "a", None, 50)
    assert body["outcome"] == "ambiguous_mutation"
    assert body["resumable"] is False and body["session_id"] is None
    assert len(runner.store) == 0, "an ambiguous session must not be offered for reuse"
    assert "NOT retried" in body["warning"]


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


def test_model_failure_is_retried_and_can_succeed(runner, monkeypatch, capsys):
    agent = FakeAgent(ticks=2)
    calls = {"n": 0}
    original = agent.command

    def flaky(name):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Model provider returned HTTP 500; no action executed.")
        return original(name)

    agent.command = flaky
    # DEBUG is read at import, so patch the module attribute the call site reads.
    monkeypatch.setattr(server, "DEBUG", True)
    monkeypatch.setattr(runner, "build_session", lambda url, goal: runner.store.add(agent, goal))
    body = runner.run_task("https://e.test", "a", None, 50)
    assert body["outcome"] == "agent_claims_done" and calls["n"] == 3
    # A silent retry looks exactly like a slow run. This line is the only thing that
    # tells the two apart in the field, so it is worth a test of its own.
    err = capsys.readouterr().err
    assert "model failure 1/2" in err and "HTTP 500" in err


def test_unknown_browser_mode_is_rejected(monkeypatch):
    """Silently serving headless on a typo is worse than refusing to start."""
    for name in server.REQUIRED_ENV:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("JEV_MCP_BROWSER", "chrome")
    with pytest.raises(SystemExit, match="not a mode"):
        server.check_env()
    for mode in ("headless", "attached", "ATTACHED"):
        monkeypatch.setenv("JEV_MCP_BROWSER", mode)
        server.check_env()


# ---- MCP wiring ----------------------------------------------------------


@pytest.mark.parametrize(
    "mode, names",
    [
        ("headless", ["close_browser_session", "run_browser_task"]),
        ("attached", ["close_browser_session_as_me", "run_browser_task_as_me"]),
    ],
)
def test_build_server_registers_tools(monkeypatch, mode, names):
    """The one surface no other test touches: a wrong kwarg here is a crash on startup."""
    import asyncio
    import importlib

    monkeypatch.setenv("JEV_MCP_BROWSER", mode)
    module = importlib.reload(server)
    try:
        tools = asyncio.run(module.build_server().list_tools())
        assert sorted(t.name for t in tools) == names
        run_tool = next(t for t in tools if t.name.startswith("run_browser_task"))
        assert sorted(run_tool.input_schema["properties"]) == ["goal", "session_id", "timeout_s", "url"]
        assert ("YOUR REAL LOGGED-IN CHROME" in run_tool.description) is (mode == "attached")
        assert "not for fetching public content" in run_tool.description.lower()
    finally:
        monkeypatch.delenv("JEV_MCP_BROWSER", raising=False)
        importlib.reload(server)


def test_importing_the_server_does_not_bind_a_daemon_name():
    """browser_harness freezes NAME/SOCK at import, so importing it early pins "default".

    main() must set BU_NAME before anything pulls browser_harness in; importing this
    package must not pull it in at all.
    """
    import subprocess
    import sys as _sys

    code = "import sys, jev_browser_use_mcp.server; print('browser_harness' in sys.modules)"
    out = subprocess.run([_sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False", "importing the server must not freeze browser_harness's daemon name"


# ---- orphan tab sweeping -------------------------------------------------


def refuse(*_a, **_k):
    raise ConnectionRefusedError(61, "daemon gone")


@pytest.fixture
def orphan(tmp_path, monkeypatch):
    """An attached-mode leftover: a tab record, and no chrome.pid because we own no browser."""
    for module in (sessions, chrome):
        monkeypatch.setattr(module, "CACHE", tmp_path)
        monkeypatch.setattr(module, "pid_alive", lambda pid: False)
    dead = tmp_path / "99999"
    dead.mkdir()
    (dead / "targets.json").write_text('["T1", "T2"]')
    return dead / "targets.json"


def test_sweep_closes_orphan_tabs_and_clears_the_record(orphan):
    assert sessions.sweep_orphan_tabs(lambda *a, **k: {}) == 2
    assert not orphan.exists()
    chrome.sweep_orphans()
    assert not orphan.parent.exists(), "an empty record dir is reclaimable"


def test_sweep_keeps_the_record_across_the_whole_startup_path(orphan):
    """main() runs sweep_orphan_tabs() then sweep_orphans(); the record must survive both.

    Testing sweep_orphan_tabs() alone hides the bug, because the deletion lives in the
    order main() calls two functions, not inside either one.
    """
    assert sessions.sweep_orphan_tabs(refuse) == 0
    assert orphan.exists()
    assert chrome.sweep_orphans() == 0, "a dir holding an unreaped tab record is not an orphan"
    assert orphan.exists(), "rmtree would strand those tabs in the user's Chrome forever"
    assert orphan.exists(), "deleting the record after a failed sweep strands tabs in the user's Chrome"
    assert sessions.sweep_orphan_tabs(lambda *a, **k: {}) == 2, "a later run must be able to retry"


def test_headless_leftovers_are_still_fully_reclaimed(orphan):
    """The gate keys on chrome.pid, not on mode -- a headless dir must not leak a profile."""
    headless = orphan.parent.parent / "88888"
    headless.mkdir()
    (headless / "chrome.pid").write_text("77777")  # pid_alive is stubbed False
    corrupt = orphan.parent.parent / "77766"
    corrupt.mkdir()
    (corrupt / "chrome.pid").write_text("not-a-pid")
    (corrupt / "targets.json").write_text("[]")
    (headless / "targets.json").write_text('["T9"]')
    (headless / "Default").mkdir()

    assert sessions.sweep_orphan_tabs(refuse) == 0
    assert chrome.sweep_orphans() == 2, "their Chrome is dead, so their tabs are too"
    assert not headless.exists() and not corrupt.exists(), "a corrupt chrome.pid is still headless garbage"
    assert orphan.exists(), "the attached record is untouched"


def test_headless_registry_writes_nothing(tmp_path, monkeypatch):
    """Writing it in headless recreates CACHE/<pid>/ after close() rmtree'd the profile.

    That leaves a dir with a tab record and no chrome.pid -- the exact shape
    sweep_orphans() refuses to reclaim, so it accumulates forever.
    """
    monkeypatch.setattr(sessions, "CACHE", tmp_path)
    registry = sessions.TabRegistry()
    registry.path = tmp_path / str(9999) / "targets.json"
    registry.enabled = False
    registry.add("T1")
    assert not registry.path.exists() and not registry.path.parent.exists()

    registry.enabled = True
    registry.add("T2")
    assert registry.path.exists(), "attached mode must still record"


@pytest.mark.parametrize(
    "name, stopped",
    [("jev-h60281", True), ("jev-chrome", False), ("default", False), ("", False)],
)
def test_only_our_own_headless_daemon_is_stopped(monkeypatch, name, stopped):
    """jev-chrome is shared with the user's other browser-harness consumers."""
    calls = []
    monkeypatch.setenv("BU_NAME", name)
    monkeypatch.setitem(
        __import__("sys").modules,
        "browser_harness.admin",
        type("M", (), {"restart_daemon": staticmethod(lambda n: calls.append(n))}),
    )
    server.stop_own_daemon()
    assert bool(calls) is stopped
