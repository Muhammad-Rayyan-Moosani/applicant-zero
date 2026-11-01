"""Thread-safe shared state + the Human-in-the-Loop (HITL) mechanism.

The dashboard reads `STATE.snapshot()`. The pipeline updates job rows. When an agent needs the
human, it calls `await STATE.ask(...)`, which registers a pending question (surfaced on Telegram
and the dashboard) and blocks on an asyncio.Event until Telegram resolves it with `STATE.answer()`.
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class JobRow:
    id: str
    role: str = ""
    company: str = ""
    status: str = "queued"          # queued | scouting | ranking | tailoring | login | filling |
    #                                 ats | awaiting_human | ready | submitting | submitted | skipped | error
    tier: Optional[int] = None
    ats_score: Optional[int] = None
    note: str = ""
    updated: float = field(default_factory=time.time)


@dataclass
class Pending:
    id: str
    kind: str                       # "greenlight" | "login" | "question" | "choice"
    company: str
    role: str
    prompt: str
    options: list[str]
    screenshot: Optional[str] = None   # path to a login-wall screenshot, surfaced on the dashboard/Telegram
    created: float = field(default_factory=time.time)


class AppState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.jobs: dict[str, JobRow] = {}
        self.order: list[str] = []
        self._pending: dict[str, Pending] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._answers: dict[str, Any] = {}
        self.log_lines: list[str] = []
        self.loop: Optional[asyncio.AbstractEventLoop] = None   # set by main

    # ---- jobs ----
    def add_job(self, role: str, company: str) -> str:
        jid = uuid.uuid4().hex[:8]
        with self._lock:
            self.jobs[jid] = JobRow(id=jid, role=role, company=company)
            self.order.append(jid)
        self.log(f"queued: {role} @ {company}")
        return jid

    def update(self, jid: str, **kw) -> None:
        with self._lock:
            r = self.jobs.get(jid)
            if not r:
                return
            for k, v in kw.items():
                setattr(r, k, v)
            r.updated = time.time()

    def log(self, line: str) -> None:
        with self._lock:
            self.log_lines.append(f"{time.strftime('%H:%M:%S')}  {line}")
            self.log_lines = self.log_lines[-400:]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "jobs": [asdict(self.jobs[i]) for i in self.order],
                "pending": [asdict(p) for p in self._pending.values()],
                "log": self.log_lines[-120:],
                "ts": time.time(),
            }

    # ---- HITL ----
    async def ask(self, kind: str | None = None, company: str = "", role: str = "",
                  prompt: str = "", options: list[str] | None = None, timeout: int = 1800,
                  action_type: str | None = None, title: str | None = None,
                  message: str | None = None, screenshot: str | None = None) -> Any:
        """Register a pending question, surface it, and block until answered or timed out.

        Accepts two naming styles: the positional (kind, company, role, prompt) and the keyword
        aliases the agents use (action_type→kind, title→role, message→prompt, plus screenshot)."""
        kind = kind or action_type or "question"
        role = role or title or ""
        prompt = prompt or message or ""
        pid = uuid.uuid4().hex[:8]
        p = Pending(id=pid, kind=kind, company=company, role=role, prompt=prompt,
                    options=options or [], screenshot=screenshot)
        ev = asyncio.Event()
        with self._lock:
            self._pending[pid] = p
            self._events[pid] = ev
        self.log(f"⏸ awaiting human [{kind}] — {company}: {prompt[:60]}")
        if _notifier:                     # push to Telegram
            try:
                _notifier(p)
            except Exception:
                pass
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.log(f"⌛ human timeout [{kind}] — {company}")
        with self._lock:
            self._pending.pop(pid, None)
            self._events.pop(pid, None)
            return self._answers.pop(pid, None)

    def answer(self, pid: str, value: Any) -> bool:
        """Called by the Telegram layer when the human responds. Thread-safe → loop-safe."""
        with self._lock:
            ev = self._events.get(pid)
            if not ev:
                return False
            self._answers[pid] = value
        if self.loop:
            self.loop.call_soon_threadsafe(ev.set)
        else:
            ev.set()
        return True

    def latest_pending_id(self) -> Optional[str]:
        with self._lock:
            return next(reversed(self._pending), None) if self._pending else None

    def pending(self, pid: str) -> Optional[Pending]:
        with self._lock:
            return self._pending.get(pid)


# a callback the Telegram layer registers to get pushed pending questions
_notifier = None


def set_notifier(fn) -> None:
    global _notifier
    _notifier = fn


STATE = AppState()
