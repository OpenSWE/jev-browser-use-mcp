"""Sessions, the retask contract, and cleanup.

jev's Agent has no retask path -- `command()` accepts only tick/predict/act --
so a follow-up goal means writing its internal state dict directly. That is the
one place this package reaches into undocumented internals, and it is guarded by
a contract assertion that fails loud the moment upstream changes shape.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .chrome import CACHE, pid_alive

MAX_SESSIONS = 3
IDLE_TTL_S = 300.0

# Every key of Agent.state as built at jev_ultrafast/agent.py:27-41, pinned to
# sha 1231850a. The assertion below compares the FULL set, not just the nine
# keys retask() writes: the drift that silently breaks retasking is an ADDED
# key -- a new counter beside `decisions` would gate task two at agent.py:75
# while a nine-key check saw nothing wrong.
STATE_KEYS = frozenset(
    {
        "browser",
        "goal",
        "page",
        "decision",
        "history",
        "status",
        "plan",
        "plan_index",
        "decisions",
        "text_calls",
        "elapsed_ms",
        "started_at",
        "record",
    }
)


class ContractDrift(RuntimeError):
    """jev_ultrafast.Agent.state no longer matches what retask() was written against."""


def assert_state_contract(state: dict) -> None:
    """Fail loud when Agent.state gains or loses a key. Extras are the dangerous direction."""
    actual = set(state)
    added, removed = actual - STATE_KEYS, STATE_KEYS - actual
    if added or removed:
        raise ContractDrift(
            "jev_ultrafast.Agent.state changed shape; retask() is no longer safe. "
            f"added={sorted(added)} removed={sorted(removed)}. "
            "Re-read jev_ultrafast/agent.py:27-41 and update STATE_KEYS and retask() together."
        )


def retask(agent, goal: str) -> None:
    """Point a live Agent at a new goal on its current page, without re-navigating.

    Clearing `history` is mandatory, not hygiene: its last 10 entries ship to the
    model on every predict under rules that say "Do not repeat satisfied steps",
    so leftovers from the previous goal are a direct path to a premature DONE.
    Clearing `decisions` also resets jev's two budgets, giving each goal a full one.
    """
    assert_state_contract(agent.state)
    agent.state.update(
        goal=goal,
        plan=[goal],
        plan_index=0,
        status="ready",
        history=[],
        decisions=[],
        decision=None,
        started_at=None,
        text_calls=[],
    )
    # An instance attribute, outside state -- a dict key-set assertion can never cover it.
    agent.pending_text = None


@dataclass
class Session:
    id: str
    agent: object
    goal: str
    created_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)
    browser_context_id: str | None = None

    def touch(self) -> None:
        self.last_used = time.monotonic()

    @property
    def idle_s(self) -> float:
        return time.monotonic() - self.last_used


def close_quietly(agent) -> None:
    """Agent.close() raises BEFORE clearing self.target, so an unguarded close masks the real error."""
    try:
        agent.close()
    except Exception:
        pass


class TabRegistry:
    """Records the tabs we create so a hard-killed server's tabs can still be reaped.

    Only meaningful in attached mode: there the browser outlives us and our
    background tabs would otherwise accumulate invisibly in the user's Chrome
    forever. Headless mode kills the whole browser, which takes its tabs with it.
    """

    def __init__(self) -> None:
        self.path = CACHE / str(os.getpid()) / "targets.json"
        self._ids: set[str] = set()
        self._lock = threading.Lock()

    def _flush(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(sorted(self._ids)))
        except OSError:
            pass

    def add(self, target_id: str | None) -> None:
        if not target_id:
            return
        with self._lock:
            self._ids.add(target_id)
            self._flush()

    def discard(self, target_id: str | None) -> None:
        if not target_id:
            return
        with self._lock:
            self._ids.discard(target_id)
            self._flush()

    def clear(self) -> None:
        with self._lock:
            self._ids.clear()
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass


def sweep_orphan_tabs(cdp) -> int:
    """Close tabs recorded by servers that are gone. Call BEFORE chrome.sweep_orphans().

    That ordering matters: sweep_orphans() removes the whole directory, this file
    included, so it must read them first.
    """
    closed = 0
    if not CACHE.is_dir():
        return 0
    for entry in CACHE.iterdir():
        if not entry.is_dir() or not entry.name.isdigit() or pid_alive(int(entry.name)):
            continue
        targets = entry / "targets.json"
        try:
            ids = json.loads(targets.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        failed = False
        for target_id in ids:
            try:
                cdp("Target.closeTarget", targetId=target_id)
                closed += 1
            except Exception:
                failed = True  # Tab already gone, or the daemon is unreachable. Cannot tell which.
        if failed:
            # Keep the record: this file is the only thing that can ever close those
            # tabs, and deleting it after a failed sweep strands them in the user's
            # Chrome forever. A later run retries; closing a dead tab is harmless.
            continue
        try:
            targets.unlink(missing_ok=True)
        except OSError:
            pass
    return closed


class SessionStore:
    """At most MAX_SESSIONS live Agents, reaped when idle, built one at a time."""

    def __init__(self, tabs: TabRegistry | None = None) -> None:
        self._sessions: dict[str, Session] = {}
        self._guard = threading.Lock()
        # Agent construction is serialized even though runs are parallel:
        # ensure_daemon() skips its own spawn lock exactly when BU_CDP_URL is set,
        # which is precisely our headless mode. Concurrent cold starts would race
        # several daemons onto one socket and orphan the losers' CDP connections.
        self.build_lock = threading.Lock()
        self.tabs = tabs or TabRegistry()

    def get(self, session_id: str) -> Session | None:
        with self._guard:
            session = self._sessions.get(session_id)
            if session:
                session.touch()
            return session

    def add(self, agent, goal: str) -> Session:
        session = Session(id=uuid.uuid4().hex[:12], agent=agent, goal=goal)
        self.tabs.add(getattr(getattr(agent, "browser", None), "target", None))
        with self._guard:
            while len(self._sessions) >= MAX_SESSIONS:
                oldest = max(self._sessions.values(), key=lambda s: s.idle_s)
                self._evict(oldest.id)
            self._sessions[session.id] = session
        return session

    def _evict(self, session_id: str) -> None:
        """Caller holds self._guard."""
        session = self._sessions.pop(session_id, None)
        if session:
            self.tabs.discard(getattr(getattr(session.agent, "browser", None), "target", None))
            close_quietly(session.agent)

    def drop(self, session_id: str) -> None:
        with self._guard:
            self._evict(session_id)

    def reap_idle(self) -> int:
        with self._guard:
            stale = [s.id for s in self._sessions.values() if s.idle_s > IDLE_TTL_S and not s.lock.locked()]
            for session_id in stale:
                self._evict(session_id)
            return len(stale)

    def close_all(self) -> None:
        with self._guard:
            for session_id in list(self._sessions):
                self._evict(session_id)
        self.tabs.clear()

    def __len__(self) -> int:
        with self._guard:
            return len(self._sessions)


def start_reaper(store: SessionStore, interval_s: float = 30.0) -> threading.Thread:
    def loop() -> None:
        while True:
            time.sleep(interval_s)
            try:
                store.reap_idle()
            except Exception:
                pass

    thread = threading.Thread(target=loop, name="jev-session-reaper", daemon=True)
    thread.start()
    return thread


def cache_dir_for_pid(pid: int | None = None) -> Path:
    return CACHE / str(pid if pid is not None else os.getpid())
