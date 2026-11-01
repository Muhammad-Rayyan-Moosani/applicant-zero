# Applicant Zero — 8-Agent Autonomous Job-Application Engine

A multi-agent system that discovers, ranks, tailors documents for, and fills out job applications
end-to-end — the agents collaborate over a shared blackboard and self-heal at every decision point.

Cloud brain (Claude), local body (Playwright), orchestrated as a **LangGraph** state machine, with
remote control (Telegram over Tailscale) and a live dark dashboard (FastAPI). **No local LLM is ever
started** — cognition is offloaded to Claude via a hybrid backend: the `claude` CLI (flat
subscription cost, great for a 24/7 private server) or the Anthropic API (per-token), switched with
`LLM_BACKEND` in `.env`.

## The 8 agents
| # | Agent | Does |
|---|-------|------|
| 8 | Executive Manager | Orchestrates, holds state, freezes on risk, gates login + submit through you on Telegram |
| 1 | Search Scout | Crawls a source URL → postings (title, company, location, apply link) |
| 2 | Relevancy Ranker | Scores each job 1–5 vs your knowledge base; drops below `MIN_TIER` |
| 3 | Document Tailor | Tailored résumé bullets (Google XYZ) + a genuine cover letter |
| 4 | Account Access | **Manual-login handoff** — you sign in, it never stores a plaintext password |
| 5 | Form Filler | Maps DOM → knowledge base; unknown fields are asked on Telegram |
| 6 | ATS Officer | Keyword scores the résumé; loops back to Tailor or issues GREEN LIGHT |
| 7 | Submitter | Only on GREEN LIGHT **and** your Telegram greenlight → submits + screenshots to `/receipts` |

## Setup
```bash
cd applicant-zero
bash setup.sh                 # venv + deps + Chromium + .env
# edit .env  → LLM_BACKEND (cli|api), ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
# seed your profile (these are gitignored — your real data never leaves your machine):
cp data/profile.example.json      data/profile.json
cp data/knowledge_base.example.md data/knowledge_base.md
# then put resume.pdf (and optional transcript.pdf) in ./data/
source .venv/bin/activate
python main.py                # or: python main.py https://some-job-url
```
Dashboard: `http://<your-tailscale-ip>:8000` (binds `0.0.0.0`, no public ports).

## Control
- **Telegram:** send a job URL or `/apply <url>` to start. Approve gates with the inline buttons
  (✅ Greenlight / ❌ Reject); answer unknown fields by replying with text.
- **Dashboard:** live Role / Company / Status / Tier / ATS, and an amber banner when it's waiting on you.

## Deliberate safety deviations from the original spec
These are intentional and will not be changed back:
- **No automated account creation and no `credentials.log` of passwords.** Agent 4 hands sign-in to
  you; `logs/credentials.log` records only *site + email + status*. Real secrets stay in your
  keychain/browser.
- **No blind submission.** Agent 7 requires both an ATS green light and your explicit Telegram
  greenlight before it clicks submit.
- **No CAPTCHA/bot-detection bypass.**

## Note on CrewAI
The 8-agent separation, `config/agents.yaml`, and `config/tasks.yaml` are all here, but the
orchestration is implemented directly in `asyncio` rather than via the CrewAI runtime. CrewAI's
turn-based execution model fights the three hard requirements here — concurrent HITL freezes, live
browser control, and a streaming dashboard — and pulls in a heavy dependency tree that's rough on a
2019 Air. The direct implementation does exactly what the spec describes and actually runs.

## Files
```
main.py                     runtime: dashboard + telegram + pipeline (asyncio)
config/agents.yaml          roles / goals / backstories
config/tasks.yaml           task descriptions, inputs, outputs, human-gates
data/knowledge_base.md      your master profile (generated from your real data)
data/profile.json           structured fields for deterministic form mapping
src/graph.py                LangGraph swarm — the state machine + self-healing routers
src/agents.py               the 8 agents
src/tools/browser.py        Playwright browser tool
src/dashboard.py            FastAPI dark cockpit
src/telegram_gate.py        Telegram remote gateway + human gates
src/state.py                thread-safe state + HITL event loop
src/llm.py                  Claude client (cloud brain) — hybrid CLI / API backend
receipts/                   submission confirmation screenshots
logs/credentials.log        site + email + status only (never passwords)
```
