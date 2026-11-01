"""The Swarm — a LangGraph state machine that turns the 8 agents into a collaborative group chat.

Instead of a rigid for-loop, the pipeline is a GRAPH of decisions over one shared state (the
"blackboard"). Every node appends to `messages` (the live group chat the dashboard shows), and the
Manager (a Claude router via LangChain's ChatAnthropic) reads that chat to decide what happens next
at the self-healing junctions:

    scout → rank → next_job ─▶ END
                      └▶ tailor → ats ──(revise)──▶ tailor         (ATS self-heals the résumé)
                                     └─(proceed)──▶ access ──(skip)──▶ next_job
                                                      └─▶ fill → submit ──(retry)──▶ fill
                                                                         └─(next)──▶ next_job

Self-healing (the "swarm talks it out" behaviour):
  • ATS below threshold  → loop back to the Tailor with the missing keywords (up to 2x).
  • Submit shows a validation error → the Manager consults the swarm, posts the DOM errors to the
    blackboard, and routes back to the Filler to fix them, then retries submit (up to 2x).
Guardrails kept: essay answers are drafted ONLY from the real knowledge base (no fabrication), the
human is asked for anything that can't be grounded, and the final submit always needs a human
greenlight (handled inside the agents via the Telegram gate).
"""
from __future__ import annotations

import json
import operator
import re
from typing import Annotated, Optional, TypedDict

from langgraph.graph import END, StateGraph

from . import agents, config, llm
from .state import STATE
from .tools import Browser


class SwarmState(TypedDict, total=False):
    source: str
    messages: Annotated[list, operator.add]   # the shared blackboard (group chat)
    jobs: list
    idx: int
    job: Optional[dict]
    jid: Optional[str]                         # dashboard row id
    tailored: Optional[dict]
    verdict: Optional[dict]
    revise_count: int
    submit_attempts: int
    result: Optional[dict]


class Swarm:
    def __init__(self) -> None:
        self.scout = agents.SearchScout()
        self.ranker = agents.RelevancyRanker()
        self.tailor = agents.DocumentTailor()
        self.access = agents.AccountCreator()
        self.filler = agents.FormFiller()
        self.ats = agents.AtsOfficer()
        self.submitter = agents.Submitter()
        self.browser: Optional[Browser] = None
        # The Manager runs on whatever backend is configured (CLI subscription or API).
        self._manager = config.LLM_BACKEND == "cli" or bool(config.ANTHROPIC_API_KEY)
        self.app = self._build()

    # ---- blackboard helpers ----
    @staticmethod
    def _msg(sender: str, text: str) -> dict:
        STATE.log(f"💬 {sender}: {text[:140]}")
        return {"sender": sender, "message": text}

    def consult(self, history: list, dilemma: str) -> dict:
        """Manager reads the group chat and decides the next move. Returns {next, decision, payload}."""
        if not self._manager:
            return {"next": "human", "decision": "no manager llm", "payload": ""}
        ctx = "\n".join(f"{m['sender']}: {m['message']}" for m in history[-12:])
        prompt = (
            "You are the Executive Manager of a collaborative job-application agent swarm. Read the "
            "group chat, then decide the next move to get UNSTUCK without fabricating anything.\n\n"
            f"GROUP CHAT:\n{ctx}\n\nCURRENT DILEMMA:\n{dilemma}\n\n"
            "Choose next from: tailor (synthesize an answer from REAL facts), filler (re-scan/fix the "
            "form), ats (re-check), human (ask the operator), skip (abandon this job). "
            'Reply JSON only: {"next":"...","decision":"why, <=25 words","payload":"any text to use"}')
        d = llm.generate_json(prompt, max_tokens=400)         # runs on the configured backend
        return d or {"next": "human", "decision": "manager could not decide", "payload": ""}

    # ---- nodes ----
    async def n_scout(self, s: SwarmState) -> dict:
        jobs = await self.scout.scout(s["source"], self.browser)
        return {"jobs": jobs, "idx": 0,
                "messages": [self._msg("Agent 1 (Scout)", f"Unrolled {len(jobs)} posting(s).")]}

    async def n_rank(self, s: SwarmState) -> dict:
        kept = await self.ranker.rank(s["jobs"], self.browser)
        return {"jobs": kept,
                "messages": [self._msg("Agent 2 (Ranker)", f"{len(kept)} job(s) cleared the tier bar.")]}

    async def n_next_job(self, s: SwarmState) -> dict:
        idx = s.get("idx", 0)
        jobs = s.get("jobs", [])
        if idx >= len(jobs):
            return {"job": None, "messages": [self._msg("Agent 8 (Manager)", "All jobs processed. Standing down.")]}
        job = jobs[idx]
        jid = STATE.add_job(job.get("title", ""), job.get("company", ""))
        STATE.update(jid, status="tailoring", tier=job.get("tier"))
        return {"job": job, "jid": jid, "idx": idx + 1, "revise_count": 0, "submit_attempts": 0,
                "tailored": None, "verdict": None,
                "messages": [self._msg("Agent 8 (Manager)",
                             f"Picking up {job.get('title')} @ {job.get('company')} (tier {job.get('tier')}).")]}

    async def n_tailor(self, s: SwarmState) -> dict:
        job, v = s["job"], s.get("verdict")
        fb = ("Missing keywords: " + ", ".join(v.get("missing", []))) if v and not v.get("passed") else ""
        tailored = self.tailor.tailor(job, feedback=fb)
        who = "Agent 3 (Tailor)"
        note = "Revised the résumé to cover the ATS gaps." if fb else "Drafted tailored résumé + cover."
        return {"tailored": tailored, "messages": [self._msg(who, note)]}

    async def n_ats(self, s: SwarmState) -> dict:
        v = await self.ats.check(s["tailored"], s["job"])
        if s.get("jid"):
            STATE.update(s["jid"], ats_score=v["score"])
        verdict = "GREEN LIGHT." if v["passed"] else f"{v['score']}% — below bar, needs a revision."
        return {"verdict": v, "messages": [self._msg("Agent 6 (ATS Officer)", f"Scored {v['score']}%. {verdict}")]}

    async def n_access(self, s: SwarmState) -> dict:
        job = s["job"]
        if job.get("apply_url"):
            await self.browser.goto(job["apply_url"])
        if s.get("jid"):
            STATE.update(s["jid"], status="login")
        status = await self.access.access(job, self.browser)
        return {"result": {"access": status},
                "messages": [self._msg("Agent 4 (Access)", f"Sign-in status: {status}.")]}

    async def n_fill(self, s: SwarmState) -> dict:
        if s.get("jid"):
            STATE.update(s["jid"], status="filling")
        report = await self.filler.fill(s["job"], s["tailored"], self.browser)
        unresolved = report.get("needs_input", [])
        msg = f"Filled {len(report['filled'])}, asked you {len(report['asked'])}."
        if unresolved:
            msg += f" Still blank: {', '.join(unresolved[:3])}."
        return {"result": {**(s.get("result") or {}), "fill": report},
                "messages": [self._msg("Agent 5 (Filler)", msg)]}

    async def n_submit(self, s: SwarmState) -> dict:
        job, v = s["job"], s.get("verdict") or {}
        token = v.get("token") or "GREENLIGHT-manual"
        if s.get("jid"):
            STATE.update(s["jid"], status="ready")
        res = await self.submitter.submit(job, s["tailored"], token, self.browser)
        attempts = s.get("submit_attempts", 0) + 1
        post = ""
        if res.get("submitted"):
            post = await self.browser.post_submit_state()
        msgs = [self._msg("Agent 7 (Submitter)",
                          f"Submit → {res.get('note') or ('confirmed' if post=='confirmed' else post or 'clicked')}.")]
        if res.get("submitted") and post == "error":
            errs = await self.browser.errors_on_page()
            d = self.consult(s["messages"], f"I clicked Submit but the page shows validation errors: "
                             f"{errs}. How do we fix and retry?")
            msgs.append(self._msg("Agent 8 (Manager)", f"Self-heal: {d.get('decision','retry via Filler')}"))
        return {"result": {**(s.get("result") or {}), "submit": res, "post": post}, "submit_attempts": attempts,
                "messages": msgs}

    async def n_done_job(self, s: SwarmState) -> dict:
        res = (s.get("result") or {}).get("submit", {})
        jid = s.get("jid")
        if jid:
            if res.get("submitted") and (s.get("result") or {}).get("post") != "error":
                STATE.update(jid, status="submitted", note="receipt saved")
            elif (s.get("result") or {}).get("access") == "skip":
                STATE.update(jid, status="skipped", note="login skipped")
            else:
                STATE.update(jid, status="skipped", note=res.get("note", "not submitted"))
        return {}

    # ---- routers (read state, pick the next node) ----
    def route_next_job(self, s: SwarmState) -> str:
        return "tailor" if s.get("job") else "end"

    def route_ats(self, s: SwarmState) -> str:
        v = s.get("verdict") or {}
        if v.get("passed") or s.get("revise_count", 0) >= 2:
            return "proceed"
        s["revise_count"] = s.get("revise_count", 0) + 1       # loop back to the Tailor
        return "revise"

    def route_access(self, s: SwarmState) -> str:
        return "skip" if (s.get("result") or {}).get("access") == "skip" else "fill"

    def route_submit(self, s: SwarmState) -> str:
        r = s.get("result") or {}
        if r.get("submit", {}).get("submitted") and r.get("post") != "error":
            return "next"
        if r.get("post") == "error" and s.get("submit_attempts", 0) < 2:
            return "retry"                                       # self-heal: fix the form, submit again
        return "next"

    # ---- build + run ----
    def _build(self):
        g = StateGraph(SwarmState)
        g.add_node("scout", self.n_scout)
        g.add_node("rank", self.n_rank)
        g.add_node("next_job", self.n_next_job)
        g.add_node("tailor", self.n_tailor)
        g.add_node("ats", self.n_ats)
        g.add_node("access", self.n_access)
        g.add_node("fill", self.n_fill)
        g.add_node("submit", self.n_submit)
        g.add_node("done_job", self.n_done_job)
        g.set_entry_point("scout")
        g.add_edge("scout", "rank")
        g.add_edge("rank", "next_job")
        g.add_conditional_edges("next_job", self.route_next_job, {"tailor": "tailor", "end": END})
        g.add_edge("tailor", "ats")
        g.add_conditional_edges("ats", self.route_ats, {"revise": "tailor", "proceed": "access"})
        g.add_conditional_edges("access", self.route_access, {"fill": "fill", "skip": "done_job"})
        g.add_edge("fill", "submit")
        g.add_conditional_edges("submit", self.route_submit, {"retry": "fill", "next": "done_job"})
        g.add_edge("done_job", "next_job")
        return g.compile()

    async def run(self, source: str) -> None:
        self.browser = Browser()
        await self.browser.start()
        STATE.log("🐝 Swarm online — LangGraph decision loop engaged.")
        try:
            await self.app.ainvoke(
                {"source": source, "messages": [self._msg("Agent 8 (Manager)", f"New brief: {source}")],
                 "idx": 0, "revise_count": 0, "submit_attempts": 0},
                config={"recursion_limit": 300})
            STATE.log("🐝 Swarm run complete.")
        finally:
            await self.browser.close()
