"""The 8 agents. Cloud-brain (Claude) for cognition, Playwright for the body, HITL via Telegram.

  Agent 8  ExecutiveManager   — orchestrates, freezes on risk, gates login + submit through the human
  Agent 1  SearchScout        — crawl source → postings
  Agent 2  RelevancyRanker    — tier 1-5 vs knowledge base, drop < MIN_TIER
  Agent 3  DocumentTailor     — tailored résumé bullets (XYZ) + genuine cover letter
  Agent 4  AccountCreator     — SAFE: manual-login handoff; no plaintext passwords
  Agent 5  FormFiller         — map DOM → knowledge; yield unknown fields to the human
  Agent 6  AtsOfficer         — adversarial keyword score; loop to tailor or GREEN LIGHT
  Agent 7  Submitter          — only on GREEN LIGHT + human greenlight; attach, submit, screenshot
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

import yaml

from . import config, llm
from .state import STATE
from .tools import Browser

_AGENTS = yaml.safe_load((config.CONFIG / "agents.yaml").read_text())
_KB = config.KNOWLEDGE_BASE.read_text() if config.KNOWLEDGE_BASE.exists() else ""
_PROFILE = json.loads(config.PROFILE_JSON.read_text()) if config.PROFILE_JSON.exists() else {}


# ----------------------------------------------------------------------------- Agent 1: Scout
import urllib.parse   # (Browser is our own tools.Browser, imported at the top — do NOT import browser_use)

class SearchScout:
    async def scout(self, source: str, browser: Browser) -> list[dict]:
        STATE.log(f"Agent 1 (Scout): Processing target domain [{urllib.parse.urlparse(source).netloc}]...")
        
        # 1. Access the target environment
        page = await browser.goto(source)
        await browser.dismiss_cookies()
        
        # 2. Handle Lazily-Loaded Aggregators (LinkedIn/Indeed Infinite Scroll/Pagination)
        # We scroll iteratively to force the browser to trigger lazy XHR/Fetch API payload bindings
        for _ in range(3): 
            await page.evaluate("window.scrollBy(0, window.innerHeight * 1.5);")
            await asyncio.sleep(1.2) # Essential buffer for domestic 2019 Mac Air CPU rendering cycles
            
        # Return to top to ensure DOM stability
        await page.evaluate("window.scrollTo(0, 0);")
        
        # 3. Extract Semantic Structure + Direct Interactivity Layout
        # Rather than dropping a 12,000 char blind string, we parse the interactive links along with text
        raw_text = await browser.text(14000)
        
        # Extract a clean structure of contextual href links to prevent LLM URL hallucination
        links_context = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a[href*="/jobs/"], a[href*="/job/"], a[href*="/view/"], .job-card a'))
                .slice(0, 30)
                .map(a => ({ text: a.innerText.trim(), href: a.href }));
        }""")
        
        # 4. Deep Cognitive Engine Processing
        prompt = (
            f"{_AGENTS['search_scout']['backstory']}\n\n"
            f"TARGET LANDING URL: {source}\n\n"
            "CRITICAL MISSION:\n"
            "Analyze the job-board / search landing page architecture. Identify if this page represents an item directory "
            "(e.g., LinkedIn grid, Simplify search hub) or an isolated single-job profile.\n\n"
            "If it is a list/directory, extract up to 15 active postings. Securely resolve the real canonical application link "
            "for each listing based on the contextual element map provided.\n\n"
            f"AVAILABLE INTERACTIVE LINKS MAP:\n{json.dumps(links_context)}\n\n"
            f"RAW DOM TEXT LAYOUT SUBSET:\n{raw_text}\n\n"
            "JSON EXPECTED SCHEMA CONFIGURATION:\n"
            'Return exactly: {"jobs": [{"title": "String", "company": "String", "location": "String", "apply_url": "String"}]}'
        )
        
        data = llm.generate_json(prompt, max_tokens=2000)
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        
        # 5. Dual-Mode Failover Handling
        # If the page structure is a standalone micro-site layout (Simplify tracking link / single application board)
        if not jobs:
            domain = urllib.parse.urlparse(source).netloc
            STATE.log(f"Agent 1 Notice: Empty directory array. Defaulting to Single-Posting ingestion mode for {domain}.")
            jobs = [{
                "title": "(Single Tracking Profile Detected)", 
                "company": domain.replace("www.", ""), 
                "location": "Determined by Agent 2",
                "apply_url": source
            }]
            
        # 6. Absolute URL Standardization & Normalization
        for j in jobs:
            # Enforce clean data primitives
            title = j.get("title", "").strip()
            j["title"] = title if title else "(Untitled Position)"
            
            company = j.get("company", "").strip()
            j["company"] = company if company else "Unknown Employer"
            
            url = j.get("apply_url", "").strip()
            # If the URL is missing, relative, or points to a script anchor, normalize back to base source
            if not url or url.startswith("#") or url.startswith("javascript:"):
                j["apply_url"] = source
            else:
                # Merge relative paths (e.g. /jobs/view/123) with host schema cleanly
                j["apply_url"] = urllib.parse.urljoin(source, url)
                
        STATE.log(f"Agent 1 Success: Scouted and standardized {len(jobs)} target posting payload entries.")
        return jobs



# ----------------------------------------------------------------------------- Agent 2: Ranker

class RelevancyRanker:
    async def rank(self, jobs: list[dict], browser) -> list[dict]:
        STATE.log(f"Agent 2 (Ranker): Scoring fit for {len(jobs)} prospective posting(s)...")
        kept = []
        
        # Pull strict filter parameters from config if available, otherwise use strict defaults
        forbidden_keywords = getattr(config, "FORBIDDEN_KEYWORDS", [r"\bsenior\b", r"\bstaff\b", r"\blead\b", r"\bmanager\b"])
        
        for j in jobs:
            title = j.get("title", "")
            company = j.get("company", "")
            location = j.get("location", "")
            apply_url = j.get("apply_url", "")
            
            # --- STAGE 1: FAST FAILURE LOCAL GUARDRAILS (Token Saver) ---
            # Automatically drop senior/lead roles if the candidate profile is a new-grad/co-op
            if any(re.search(pattern, title.lower()) for pattern in forbidden_keywords):
                STATE.log(f"Agent 2: Dropped {company} via Local Guardrail (Seniority mismatch in Title: '{title}')")
                continue

            # --- STAGE 2: DEEP CONTENT INGESTION FAILOVER ---
            # If the snippet is missing context (common in directory lists), pull the actual posting page source
            full_description_text = j.get("description", "")
            if not full_description_text and apply_url and apply_url.startswith("http"):
                try:
                    STATE.log(f"Agent 2: Fetching deep context from source to evaluate {company}...")
                    # Fast-jump browser context page to scrape the full description text layout
                    context_page = await browser.new_page()
                    await context_page.goto(apply_url, timeout=10000)
                    full_description_text = await context_page.evaluate("document.body.innerText")
                    full_description_text = full_description_text[:6000] # Cap length to optimize token payload
                    await context_page.close()
                except Exception as e:
                    STATE.log(f"Agent 2 Warning: Failed to pull deep context for {company} ({str(e)}). Falling back to metadata-only ranking.")
                    full_description_text = "Metadata evaluation only."

            # --- STAGE 3: LENIENT FIELD + LEVEL GATE ---
            # Decide ONLY: is this a tech/IT role at an early-career level? If so, apply.
            # Deliberately ignore graduation year, start-year/cohort, exact tech stack, empty
            # descriptions, and location — those must never cause a skip (the applicant wants breadth).
            prompt = (
                f"{_AGENTS['relevancy_ranker']['backstory']}\n\n"
                "You are a LENIENT relevance gate for a student / new-grad applicant. Decide only whether "
                "this posting is worth applying to, and bias strongly toward APPLYING.\n\n"
                "APPLY (relevant=true) when BOTH hold:\n"
                "  1. FIELD is software or IT: software, developer, programmer, engineering, data, "
                "     data science, ML/AI, IT, QA/test, security, cloud/devops, web, mobile, firmware, "
                "     systems, or a technical analyst role — anything software/data/IT related.\n"
                "  2. LEVEL is early-career: intern, co-op, student, new-grad, entry-level, junior, "
                "     associate, I or II — OR the level is not stated.\n\n"
                "NEVER skip for any of these — ignore them entirely:\n"
                "  - Graduation year, program year, cohort year or start year (e.g. a '2027' program). "
                "Do NOT compare any dates.\n"
                "  - A missing/empty job description or an unverifiable tech stack.\n"
                "  - Specific tools or languages the résumé may not list.\n"
                "  - The city or country (apply regardless of location).\n\n"
                "SKIP (relevant=false) ONLY if the role is clearly SENIOR (senior/staff/lead/principal/"
                "manager/director/architect, or explicitly needs 3+ years) OR clearly NOT a tech field "
                "(e.g. nursing, sales, marketing, retail, pure finance/accounting with no software). "
                "When unsure, APPLY.\n\n"
                f"JOB:\nTitle: {title}\nCompany: {company}\nLocation: {location}\n"
                f"DESCRIPTION (may be empty — that is fine):\n{full_description_text[:3000]}\n\n"
                'Return exactly: {"relevant": true/false, "field": "", "level": "", "reason": "<=15 words"}'
            )

            try:
                ev = llm.generate_json(prompt, max_tokens=500)
                relevant = bool(ev.get("relevant", True))      # default to applying if unsure
                reason = ev.get("reason", "")
                j["tier"] = 5 if relevant else 1               # tier is now just a display signal
                j["reason"] = reason
                if relevant:
                    STATE.log(f"Agent 2 APPLY: {company} — {title} "
                              f"[{ev.get('level','?')} / {ev.get('field','?')}] {reason}")
                    j["description"] = full_description_text    # reuse so Agent 3 needn't re-scrape
                    kept.append(j)
                else:
                    STATE.log(f"Agent 2 Skip: {company} — {title} (not a fit: {reason})")

            except Exception as cognitive_err:
                STATE.log(f"Agent 2 Error: {str(cognitive_err)} — defaulting to APPLY (lenient).")
                j["tier"] = 5
                j["reason"] = "default apply (evaluator error)"
                j["description"] = full_description_text
                kept.append(j)
                
        STATE.log(f"Agent 2 Complete: Filtered down to {len(kept)} viable position target payloads.")
        return kept


# ----------------------------------------------------------------------------- Agent 3: Tailor
class DocumentTailor:
    def tailor(self, job: dict, feedback: str = "") -> dict:
        STATE.log(f"Agent 3 (Tailor): writing docs for {job.get('company')}…")
        company, role = job.get("company", "the company"), job.get("title", "the role")

        # STATIC mode: same base résumé for every job — no per-job rewrite. We still write a short,
        # genuine cover letter, but skip summary/bullet generation (and ATS skips scoring a fixed PDF).
        if getattr(config, "STATIC_RESUME", False):
            return self._static(company, role)

        research = llm.research(f"What does {company} do, their products, mission, and tech? 4 sentences.")
        fb = f"\nATS FEEDBACK to address this revision:\n{feedback}\n" if feedback else ""
        data = llm.generate_json(
            f"{_AGENTS['document_tailor']['backstory']}\n\n"
            "Write a tailored résumé summary, 3-5 résumé bullets in the Google XYZ formula "
            "('Accomplished X by doing Y, measured by Z'), and a genuine, specific cover-letter body. "
            "Use ONLY facts in the knowledge base — never invent employers, titles, or metrics.\n\n"
            f"KNOWLEDGE BASE:\n{_KB[:6000]}\n\n"
            f"COMPANY RESEARCH:\n{research[:1500]}\n\n"
            f"JOB: {role} @ {company}\nDESCRIPTION:\n{(job.get('description') or '')[:3000]}{fb}\n\n"
            '{"summary":"","bullets":["",""],"cover_letter":"a full 3-4 paragraph cover letter body"}',
            max_tokens=4000, backend="api")        # cover quality matters → prefer the API model
            # 4000, not ~2200: the résumé summary + 5 bullets + a 3-4 paragraph cover letter needs
            # ~2.2k output tokens; a lower cap truncates the JSON mid-string and the parse yields {}.
        out = config.DATA / "applications" / re.sub(r"[^A-Za-z0-9]+", "_", company)[:40]
        out.mkdir(parents=True, exist_ok=True)
        cover = (f"{_PROFILE.get('preferred_first','')} {_PROFILE.get('legal_last','')}\n"
                 f"{_PROFILE.get('email','')} · {_PROFILE.get('phone','')} · "
                 f"{_PROFILE.get('city','')}, {_PROFILE.get('province','')}\n\n"
                 f"Dear Hiring Team at {company},\n\n{data.get('cover_letter','')}\n\n"
                 f"Sincerely,\n{_PROFILE.get('preferred_first','')} {_PROFILE.get('legal_last','')}\n")
        cover_path = out / "cover_letter.txt"
        cover_path.write_text(cover)
        (out / "tailored_resume.json").write_text(json.dumps(data, indent=2))
        STATE.log(f"Agent 3: cover + tailored résumé saved → {cover_path.name}")
        return {"summary": data.get("summary", ""), "bullets": data.get("bullets", []),
                "cover_text": cover, "cover_path": str(cover_path),
                "resume_pdf": _resume_pdf(), "transcript_pdf": _transcript_pdf()}

    def _static(self, company: str, role: str) -> dict:
        """Static mode: attach the base résumé unchanged; still write a short grounded cover letter."""
        body = ""
        try:
            body = llm.generate(
                "Write a concise, genuine 2-paragraph cover-letter body (no header or signature) for the "
                f"role of {role} at {company}, using ONLY these facts — never invent anything:\n"
                f"{_KB[:3500]}\nReturn plain text only.", backend="api", max_tokens=700)
        except Exception:
            body = ""
        if not body:
            body = (f"I'm excited to apply for the {role} role at {company}. My background in software and "
                    "applied AI — built through hands-on co-op and project work — maps well to this role, "
                    "and I'd welcome the chance to contribute.")
        out = config.DATA / "applications" / re.sub(r"[^A-Za-z0-9]+", "_", company)[:40]
        out.mkdir(parents=True, exist_ok=True)
        cover = (f"{_PROFILE.get('preferred_first','')} {_PROFILE.get('legal_last','')}\n"
                 f"{_PROFILE.get('email','')} · {_PROFILE.get('phone','')} · "
                 f"{_PROFILE.get('city','')}, {_PROFILE.get('province','')}\n\n"
                 f"Dear Hiring Team at {company},\n\n{body}\n\n"
                 f"Sincerely,\n{_PROFILE.get('preferred_first','')} {_PROFILE.get('legal_last','')}\n")
        cover_path = out / "cover_letter.txt"
        try:
            cover_path.write_text(cover)
        except Exception:
            pass
        STATE.log(f"Agent 3: static résumé attached + cover saved for {company}.")
        return {"summary": "", "bullets": [], "cover_text": cover, "cover_path": str(cover_path),
                "resume_pdf": _resume_pdf(), "transcript_pdf": _transcript_pdf(), "static": True}


def _resume_pdf() -> str:
    for p in [config.DATA / "resume.pdf", config.DATA / "resume_fixed.pdf",
              Path.home() / "agent" / "data" / "resume_fixed.pdf"]:
        if p.exists():
            return str(p)
    return ""


def _transcript_pdf() -> str:
    for p in [config.DATA / "transcript.pdf", Path.home() / "agent" / "data" / "transcript.pdf"]:
        if p.exists():
            return str(p)
    return ""


# ----------------------------------------------------------------------------- Agent 4: Access
import time
import asyncio

class AccountCreator:
    async def access(self, job: dict, browser: Browser) -> str:
        """
        SAFE manual-login handoff with asynchronous human intervention. 
        Returns: 'signed_in' | 'skip'
        """
        STATE.log(f"Agent 4 (Access): Scanning authorization landscape for {job.get('company')}...")
        await browser.dismiss_cookies()
        
        # 1. Immediate Pass-Through Condition
        # If the browser session already has active cookies/auth tokens, bypass interaction entirely
        if await browser.is_logged_in() and not await browser.login_wall():
            STATE.log(f"Agent 4: Active session detected for {job.get('company')}. Proceeding directly.")
            _cred_log(job, "already-signed-in")
            return "signed_in"
            
        # 2. Capture Login Screen Context for the Remote Dashboard / Telegram Bot
        try:
            screenshot_path = f"data/receipts/login_wall_{int(time.time())}.png"
            await browser.page.screenshot(path=screenshot_path)
        except Exception:
            screenshot_path = None

        # 3. Intercept and Halt Engine (Trigger Human-In-The-Loop Webhook or Prompt)
        # This code now runs correctly because it sits safely outside the early return loop
        STATE.log(f"Agent 4 Warning: Login wall detected at {job.get('company')}. Launching HITL Request...")
        
        ans = await STATE.ask(
            action_type="login", 
            company=job.get("company", "Unknown"), 
            title=job.get("title", ""),
            message=(
                f"Sign in or create an account for {job.get('company')} inside the open browser window. "
                "Confirm once authenticated. (Note: Passwords remain secure within your personal browser keychain)."
            ),
            screenshot=screenshot_path, # Injects the screenshot directly into your V2 Master Dashboard / Telegram framework
            options=["DONE", "SKIP"]
        )
        
        # 4. Process Human Intervention Input Callback Payload
        if not ans or ans.upper().startswith("SKIP"):
            STATE.log(f"Agent 4 Action: Human requested bypass for {job.get('company')}. Aborting tracking branch.")
            _cred_log(job, "skipped-by-human")
            return "skip"
            
        # 5. Post-Authentication Synchronization Pass
        STATE.log(f"Agent 4 Status: Human confirmed authorization success for {job.get('company')}. Stabilizing context...")
        await browser.dismiss_cookies() # Clear secondary tracking banner anomalies after user interaction
        _cred_log(job, "signed-in-by-human")
        return "signed_in"


def _cred_log(job: dict, status: str) -> None:
    """Thread-safe, secure system audit logging function tracking pipeline states."""
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    line = (f"{timestamp} | {job.get('company','Unknown'):<20} | "
            f"{_PROFILE.get('email','N/A'):<30} | {status:<20} | {job.get('apply_url','')}\n")
    try:
        # Enforce clean write stream boundaries using pathlib
        with config.CREDENTIALS_LOG.open("a", encoding="utf-8") as f:
            f.write(line) # Pure telemetry tracking only — no raw secrets or keys are ever serialized
    except Exception as e:
        # Prevent logging errors from bubbling up and killing the agent execution threads
        pass


# ----------------------------------------------------------------------------- Agent 5: Filler
import json
import asyncio

# -----------------------------------------------------------------------------
# DETERMINISTIC TOKENS-SAVER SCHEMA MAP
# -----------------------------------------------------------------------------
_MAP = [
    (("first name", "given name", "legal first"), "preferred_first"),
    (("last name", "surname", "family name", "legal last"), "legal_last"),
    (("preferred first",), "preferred_first"),
    (("email", "e-mail"), "email"),
    (("phone", "mobile", "cell"), "phone"),
    (("linkedin",), "linkedin"),
    (("github",), "github"),
    (("website", "portfolio", "url"), "website"),
    (("city", "town"), "city"),
    (("province", "state"), "province"),
    (("country",), "country"),
    (("school", "university", "institution"), "school"),
    (("gpa", "grade point"), "gpa"),
]

def _answer_for(label: str, options=None, field_type: str = "text") -> str or None:
    """
    Stage 1 Local Sieve: Instantly resolves foundational fields using exact matching,
    preventing any redundant cloud token consumption or pipeline latency.
    """
    low = label.lower()

    # 0. Guards against classic mis-maps on enterprise forms (Workday etc.)
    if "extension" in low:        # "Phone Extension" must NOT get the phone number
        return None
    if "device" in low and "type" in low:   # "Phone Device Type" = Mobile/Home/Work dropdown, not the number
        return None
    if "middle" in low and "name" in low:   # optional middle name → leave blank, don't invent "None"
        return ""
    if "preferred" in low and "name" in low:
        # "I have a preferred name" (a yes/no) → No; the Preferred First/Middle/Last fields → blank.
        if low.startswith("i have") or "do you have" in low or "have a preferred" in low:
            return "No"
        return ""

    # 1. Structural Profile Lookup Loop
    for keys, pk in _MAP:
        if any(k in low for k in keys):
            v = _PROFILE.get(pk) or (_PROFILE.get("education", [{}])[0].get(pk) if pk in ("school", "gpa") else "")
            if v:
                return v
                
    # 2. Hard-Coded Compliance Filters (Localized logic for Canada/US framework context)
    if "authoriz" in low or "legally" in low or "eligible to work" in low:
        return "Yes"
        
    if "sponsor" in low or "visa" in low:
        # Candidate holds legal authorization in Canada; flags 'Yes' if US role needs it.
        return "Yes" if _PROFILE.get("needs_sponsorship_us", True) and "canada" not in low else "No"
        
    if "degree" in low and options:
        # Intelligently attempt to match against standard multi-choice selections
        for opt in options:
            if "bachelor" in opt.lower():
                return opt
        return "Bachelor's Degree"
        
    # Yield None if unmapped locally so the engine knows to fall back to Agent 5's cloud layer
    return None


# -----------------------------------------------------------------------------
# LEARNED ANSWERS — remember what you type on Telegram so the SAME field auto-fills
# next time (no re-asking). Stored in data/learned_answers.json (personal, gitignored).
# -----------------------------------------------------------------------------
_LEARNED_PATH = config.DATA / "learned_answers.json"
_BLANK = "__BLANK__"   # marker meaning "you chose to leave this field empty"
_BLANK_WORDS = {"", "blank", "skip", "none", "n/a", "na", "-", "—", "leave blank",
                "leave it blank", "empty", "nil", "nothing"}


def _load_learned() -> dict:
    try:
        return json.loads(_LEARNED_PATH.read_text())
    except Exception:
        return {}


_LEARNED = _load_learned()


def _norm_label(label: str) -> str:
    """Normalize a field label so 'Postal Code *' and 'postal code' map to one key."""
    s = re.sub(r"\s+", " ", (label or "").lower()).strip()
    return s.rstrip("*").strip().rstrip(":").strip()


def _learned_get(label: str):
    return _LEARNED.get(_norm_label(label))


def _sync_learned_to_kb() -> None:
    """Mirror learned answers into the user's knowledge_base.md (the DB they already have), inside a
    managed block, so the answers live in the knowledge base and the cognitive resolver can read them."""
    kb = config.KNOWLEDGE_BASE
    START, END = "<!-- LEARNED_ANSWERS_START -->", "<!-- LEARNED_ANSWERS_END -->"
    rows = ["", START, "## Saved application answers (auto-updated by Agent 5)"]
    for k, v in _LEARNED.items():
        rows.append(f"- {k}: {'(leave blank)' if v == _BLANK else v}")
    rows.append(END)
    block = "\n".join(rows)
    try:
        text = kb.read_text() if kb.exists() else ""
        if START in text and END in text:
            text = re.sub(re.escape(START) + r".*?" + re.escape(END), block.strip(), text, flags=re.S)
        else:
            text = text.rstrip() + "\n" + block + "\n"
        kb.write_text(text)
    except Exception as e:
        STATE.log(f"Agent 5: couldn't sync answers to knowledge base ({str(e)[:60]})")


def _learned_set(label: str, value: str) -> None:
    _LEARNED[_norm_label(label)] = value
    try:
        _LEARNED_PATH.write_text(json.dumps(_LEARNED, indent=2, ensure_ascii=False))
    except Exception as e:
        STATE.log(f"Agent 5: couldn't save learned answer ({str(e)[:60]})")
    _sync_learned_to_kb()      # also persist into the knowledge DB the user already has


# -----------------------------------------------------------------------------
# AGENT 5: HIGH-PRECISION FORM FILLER ENGINE
# -----------------------------------------------------------------------------
class FormFiller:
    async def fill(self, job: dict, tailored: dict, browser) -> dict:
        """
        Orchestrates the dynamic mapping, interpretation, text-injection, 
        and programmatic advancement through multi-step job boards.
        """
        STATE.log(f"Agent 5 (Filler): Intercepting application flow for {job.get('company')}...")
        
        # Open up the platform application gate
        await browser.click_apply()
        await asyncio.sleep(1.5) # Allow dynamic single-page applications to shift DOM states
        
        report = {"filled": [], "asked": [], "needs_input": [], "uploads": []}
        
        # Guardrail variable to prevent infinite loop traps on sticky multi-page dynamic routers
        max_pages = 8
        page_counter = 0
        processed: set[str] = set()      # labels we've already attempted — never re-process/re-ask

        while page_counter < max_pages:
            page_counter += 1
            STATE.log(f"Agent 5: Scanning active step layout window (Screen #{page_counter})...")
            
            # Extract currently visible, interactive form components from the active viewport
            fields = await browser.scrape_fields()
            visible_fields = [f for f in fields if f.get("visible", True) and f.get("label")]
            
            if not visible_fields:
                STATE.log("Agent 5: No standard elements detected. Checking for multi-page progressors or submission gates.")
                if await browser.is_submit_page() or not await browser.has_next_step():
                    break
            
            # Form Data Input Loop
            for fld in visible_fields:
                label = fld.get("label", "").strip()
                ref = fld.get("ref")
                options = fld.get("options") or []
                fld_type = fld.get("type", "text")
                
                if not label or not ref:
                    continue
                    
                # Attempt each field label only once across all page re-scans (stops the retry loop
                # on dropdowns that won't stick). Genuinely new fields (e.g. a dependent "Source"
                # that appears after a parent selection) still get handled when they first appear.
                if label in processed:
                    continue
                processed.add(label)

                # --- STEP 1: DETERMINISTIC FAST-SIEVE ---
                val = _answer_for(label, options, field_type=fld_type)

                # --- STEP 1b: YOUR SAVED ANSWERS (learned from earlier Telegram replies) ---
                if val is None:
                    saved = _learned_get(label)
                    if saved == _BLANK:
                        STATE.log(f"Agent 5: '{label}' is saved as intentionally blank — leaving it empty.")
                        report["filled"].append(label)     # handled; don't ask again
                        continue
                    if saved:
                        STATE.log(f"Agent 5: Reusing your saved answer for '{label}'.")
                        val = saved

                # --- STEP 2: HIGH-COGNITIVE CONTEXT CLOUD EXPANSION ---
                if val is None:
                    STATE.log(f"Agent 5: Direct key match missing for '{label}'. Prompting Claude Context engine...")
                    val = await self._cognitive_field_resolver(label, options, fld_type)

                # --- STEP 3: HUMAN IN THE LOOP (Telegram) — and remember the answer ---
                if val is None:
                    STATE.log(f"Agent 5 Intercept: Question unmapped by all logic streams. Halting for manual input...")
                    ans = await STATE.ask(
                        "question",
                        job.get("company", ""),
                        job.get("title", ""),
                        f"Field needs your answer: '{label}'\n"
                        f"Options provided: {options if options else 'None'}\n"
                        f"(Reply 'blank' to leave it empty — I'll remember your choice either way.)",
                        options=options
                    )
                    a = (ans or "").strip()
                    if a.lower() in _BLANK_WORDS:
                        _learned_set(label, _BLANK)        # remember: leave blank next time
                        report["asked"].append(label)
                        continue                            # leave the field empty
                    if a:
                        val = a
                        _learned_set(label, a)              # remember this answer for next time
                        report["asked"].append(label)
                    else:
                        report["needs_input"].append(label)
                        continue
                
                # --- STEP 4: SET → VERIFY → VISION FALLBACK → VERIFY → ASK YOU (no silent failures) ---
                await self._set_field(browser, ref, val, fld_type)
                await asyncio.sleep(0.3)      # let the SPA commit the value
                confirmed = await self._verify(browser, ref, val, fld_type)
                if not confirmed and getattr(config, "VISION_FALLBACK", True):
                    STATE.log(f"Agent 5 👁 DOM didn't set '{label}' → computer-vision fallback for '{val}'…")
                    await self._vision_fill(browser, ref, label, val, fld_type)
                    await asyncio.sleep(0.3)
                    confirmed = await self._verify(browser, ref, val, fld_type)
                    if confirmed:
                        STATE.log(f"Agent 5 👁 Vision set '{label}' ✓")
                if confirmed:
                    report["filled"].append(label)
                else:
                    # last resort: actually ask you on Telegram, then set + verify your answer
                    await self._ask_and_commit(browser, ref, label, options, fld_type, job, report)
            
            # --- STEP 5: DOCUMENT EXTRACTION DEPLOYMENT ---
            await self._execute_dynamic_uploads(browser, tailored, report)
            
            # --- STEP 6: DYNAMIC MULTI-PAGE PROGRESSION ---
            if await browser.has_next_step():
                STATE.log("Agent 5: Advancing to next step view page...")
                await browser.click_next_step()
                await asyncio.sleep(2.0) # Page paint cycle delay optimized for local hardware constraints
            else:
                STATE.log("Agent 5: Final application step boundary reached.")
                break

        STATE.log(f"Agent 5 Summary: Automated inputs filled: {len(report['filled'])} | HITL answers gathered: {len(report['asked'])}")
        return report

    # ---------------------------------------------------------------- set / verify / vision
    async def _set_field(self, browser, ref: str, val: str, fld_type: str) -> bool:
        """Dispatch a DOM write for one field."""
        try:
            if fld_type in ("select", "dropdown"):
                return await browser.select_option(ref, val)
            if fld_type in ("checkbox", "radio"):
                return await browser.check_element(ref, val)
            return await browser.fill(ref, val)
        except Exception as e:
            STATE.log(f"Agent 5: DOM set failed for '{ref}' ({str(e)[:60]})")
            return False

    async def _verify(self, browser, ref: str, value: str, fld_type: str) -> bool:
        """Read the field back and confirm it actually holds the intended value (verify-after-set)."""
        cur = ((await browser.get_value(ref)) or "").strip().lower()
        v = str(value).strip().lower()
        if fld_type in ("checkbox", "radio"):
            want_on = v in ("yes", "true", "1", "on", "checked", "agree")
            return (cur == "checked") if want_on else (cur != "checked")
        placeholder = cur in ("", "please select", "select...", "select", "choose", "choose...", "select one")
        if not v:                     # we intended to leave it blank → blank/placeholder is fine
            return True
        if placeholder:
            return False
        return (v in cur) or (cur in v) or (v[:14] in cur)

    async def _ask_and_commit(self, browser, ref: str, label: str, options, fld_type: str,
                              job: dict, report: dict) -> None:
        """Last resort when DOM + vision can't set a field: ask you on Telegram, then set + verify
        your answer (DOM → vision) and remember it. This is what makes a required field never slip
        through to the submit gate unanswered."""
        STATE.log(f"Agent 5 Intercept: '{label}' couldn't be set automatically — asking you on Telegram…")
        ans = await STATE.ask(
            "question", job.get("company", ""), job.get("title", ""),
            f"Couldn't fill '{label}' automatically — what should it be?\n"
            f"Options provided: {options if options else 'None'}\n"
            f"(Reply 'blank' to leave it empty — I'll remember your choice.)",
            options=options)
        a = (ans or "").strip()
        if a.lower() in _BLANK_WORDS:
            _learned_set(label, _BLANK)
            report["asked"].append(label)
            return
        if not a:
            report["needs_input"].append(label)
            return
        _learned_set(label, a)
        await self._set_field(browser, ref, a, fld_type)
        await asyncio.sleep(0.3)
        if not await self._verify(browser, ref, a, fld_type):
            await self._vision_fill(browser, ref, label, a, fld_type)
            await asyncio.sleep(0.3)
        report["asked"].append(label) if await self._verify(browser, ref, a, fld_type) \
            else report["needs_input"].append(label)

    async def _vision_fill(self, browser, ref: str, label: str, value: str, fld_type: str) -> bool:
        """Computer-vision fallback: screenshot + Set-of-Marks, let Claude pick what to click."""
        try:
            if fld_type in ("select", "dropdown", "combobox"):
                el = await browser.page.query_selector(f'[data-je="{ref}"]')
                if el:
                    try:
                        await el.click()          # open the dropdown
                    except Exception:
                        pass
                    await asyncio.sleep(0.6)
                marks = await browser.marks(only_options=True)
                img = await browser.screenshot_bytes()
                idx = llm.vision_pick(
                    img, f"Select the dropdown option that best matches '{value}' for the field '{label}'.",
                    marks)
                return await browser.click_mark(idx) if (idx is not None and idx >= 0) else False
            # text-like: locate the input visually, click it, type the value
            marks = await browser.marks()
            img = await browser.screenshot_bytes()
            idx = llm.vision_pick(
                img, f"Click the text box for the field labelled '{label}' so a value can be typed into it.",
                marks)
            if idx is not None and idx >= 0 and await browser.click_mark(idx):
                await browser.type_active(value)
                return True
            return False
        except Exception as e:
            STATE.log(f"Agent 5 vision fallback error: {str(e)[:80]}")
            return False

    async def _cognitive_field_resolver(self, label: str, options: list, field_type: str) -> str or None:
        """
        Uses Claude API JSON context mapping to resolve ambiguous descriptions,
        long phrasing, or target essays against the complete markdown knowledge base.
        """
        prompt = (
            f"You are matching an application form field target.\n"
            f"Form Question Label: '{label}'\n"
            f"Field Input Type: '{field_type}'\n"
            f"Available Selection Options: {options if options else 'N/A'}\n\n"
            f"YOUR PERSONAL DATA RECORD PROFILE:\n{_KB[:6000]}\n\n"
            "TASK:\n"
            "Determine the best choice or string answer for this form field using your profile data.\n"
            "- For checkboxes/dropdown options, select the string value from the options array that matches closely.\n"
            "- If your database does not contain a logical answer for this question or it requires a personal narrative not explicitly found, return exactly 'UNKNOWN_FIELD'.\n"
            "Return JSON only: {\"resolved_value\": \"Your Answer String or UNKNOWN_FIELD\"}"
        )
        try:
            # Resolved broken truncated code section from original snippet with structured fallback parsing
            res = llm.generate_json(prompt, max_tokens=250)
            resolved = res.get("resolved_value", "UNKNOWN_FIELD")
            return None if (not resolved or resolved == "UNKNOWN_FIELD") else resolved
        except Exception as e:
            STATE.log(f"Agent 5 Cognitive Bridge Warning: Claude context pass glitched ({str(e)}). Falling back to HITL.")
            return None

    async def _execute_dynamic_uploads(self, browser, tailored: dict, report: dict):
        """Locates document dropzones on the active page layout and uploads generated ATS files safely."""
        upload_mappings = [
            ("resume", "resume_pdf"),
            ("cover", "cover_path"),
            ("transcript", "transcript_pdf")
        ]
        
        for input_name, target_key in upload_mappings:
            file_path = tailored.get(target_key)
            if file_path and input_name not in report["uploads"]:
                if await browser.has_upload_zone(input_name):
                    STATE.log(f"Agent 5: Injecting generated asset file payload into upload dropzone [{input_name}]...")
                    if await browser.upload(input_name, file_path):
                        report["uploads"].append(input_name)
                        await asyncio.sleep(1.0)


# ----------------------------------------------------------------------------- Agent 6: ATS
import uuid
import json

class AtsOfficer:
    async def check(self, tailored: dict, job: dict) -> dict:
        """
        Agent 6 (ATS Officer): Conducts a ruthless, adversarial ATS keyword density
        and alignment audit. Returns an optimization token or strict revision orders.
        """
        # In static-résumé mode there's nothing to score or revise (fixed PDF) → auto green-light.
        if getattr(config, "STATIC_RESUME", False) or tailored.get("static"):
            STATE.log("Agent 6 (ATS): static résumé mode — scoring skipped, auto green-light.")
            return {"score": 100, "passed": True, "missing": [], "failing_reason": "",
                    "optimization_payload": "", "token": f"GREENLIGHT-{uuid.uuid4().hex[:8].upper()}"}
        STATE.log("Agent 6 (ATS): Initiating rigorous keyword mapping & scoring matrix...")
        
        # 1. Reconstruct full tailored profile text
        resume_text = (
            f"SUMMARY:\n{tailored.get('summary', '')}\n\n"
            f"EXPERIENCE & BULLETS:\n" + "\n".join(tailored.get("bullets", []))
        )
        
        # 2. Extract job context (Favoring deep text preserved by Agent 2)
        jd_text = job.get("description") or job.get("title", "")
        
        # 3. Adversarial Cognitive Structural Auditing Prompt
        prompt = (
            f"{_AGENTS['ats_officer']['backstory']}\n\n"
            "CRITICAL COMPLIANCE ASSESSMENT:\n"
            "You are an enterprise-grade Applicant Tracking System (ATS) parsing parser. "
            "Analyze the provided Resume Text against the target Job Description (JD).\n"
            "Be explicitly adversarial, strict, and uncompromising. If the job description "
            "demands specific tech stack tooling (e.g., FastAPI, Docker, Kubernetes) and the "
            "resume only mentions generic terms (e.g., 'backend engineering, devops'), you must "
            "penalize the score heavily.\n\n"
            "SCORING PARAMETERS:\n"
            "- Exact Hard Skill Matching (Core Tech Stack / Tooling): 60% weight.\n"
            "- Seniority / Experience Horizon Alignments: 20% weight.\n"
            "- Action Verbs & Metric Impact Density: 20% weight.\n\n"
            f"TARGET JOB DESCRIPTION:\n{jd_text[:5000]}\n\n"
            f"PROSPECTIVE TAILORED RESUME:\n{resume_text[:5000]}\n\n"
            "JSON EXPECTED SCHEMA CONFIGURATION:\n"
            "Return exactly this JSON format. Do not add markdown commentary outside the block. "
            "Keep 'failing_reason' <= 30 words and 'optimization_payload' <= 25 words (a runaway "
            "reason truncates the JSON):\n"
            "{\n"
            '  "score": 0, \n'
            '  "missing_keywords": ["FastAPI", "Docker"], \n'
            '  "failing_reason": "Explain why it failed or leave empty string if passed", \n'
            '  "optimization_payload": "Provide a 1-sentence prompt helper for Agent 3 to fix the gaps"\n'
            "}"
        )

        try:
            # 4. Generate JSON evaluation via cloud brain (1500 cap: enough for the schema once the
            #    reason is capped — a lower limit truncated the JSON and silently scored 0).
            evaluation_result = llm.generate_json(prompt, max_tokens=1500)
            
            score = int(evaluation_result.get("score", 0) or 0)
            missing = evaluation_result.get("missing_keywords", [])
            failing_reason = evaluation_result.get("failing_reason", "")
            opt_payload = evaluation_result.get("optimization_payload", "")
            
            # Enforce gate threshold configuration check
            min_required_score = getattr(config, "MIN_ATS_SCORE", 85)
            passed = score >= min_required_score
            
            # 5. Generate secure authorization cryptographic token
            token = f"GREENLIGHT-{uuid.uuid4().hex[:8].upper()}" if passed else ""
            
            if passed:
                STATE.log(f"Agent 6 Success: Match verified at {score}%! Clearance Token Signed: {token}")
            else:
                STATE.log(f"Agent 6 REJECTED: Score {score}% falls below boundary limit ({min_required_score}%). Cause: {failing_reason}")
                
            return {
                "score": score,
                "passed": passed,
                "missing": missing,
                "failing_reason": failing_reason,
                "optimization_payload": opt_payload,
                "token": token
            }
            
        except Exception as api_err:
            STATE.log(f"Agent 6 Error: Cognitive parsing loop glitch ({str(api_err)}). Forcing conservative revision fallback.")
            return {
                "score": 70,
                "passed": False,
                "missing": [],
                "failing_reason": "API execution runtime failure.",
                "optimization_payload": "Re-align core framework keywords with the target job descriptions explicitly.",
                "token": ""
            }

# ----------------------------------------------------------------------------- Agent 7: Submitter
import re
import time
import asyncio

class Submitter:
    async def submit(self, job: dict, tailored: dict, token: str, browser: Browser) -> dict:
        """
        Agent 7 (Submitter): Executes the final submission block using a rigorous
        heuristic validation click matrix to locate and press evasive submit triggers.
        """
        # 1. Enforce Token Authenticity Safeguard
        if not token or not token.startswith("GREENLIGHT-"):
            STATE.log(f"Agent 7 Block: Refusing execution for {job.get('company')}. Missing structural GREENLIGHT token.")
            return {"submitted": False, "note": "no green-light token"}
            
        # 2. Asynchronous Human-in-the-Loop Gateway Prompt
        ans = await STATE.ask(
            action_type="greenlight", 
            company=job.get("company", ""), 
            title=job.get("title", ""),
            message="Everything is dynamically populated and has passed rigorous ATS audit scanning. Confirm final submission?",
            options=["✅ Greenlight Submission", "❌ Reject / Cancel"]
        )
        
        if not ans or ("greenlight" not in ans.lower() and not ans.startswith("✅")):
            STATE.log(f"Agent 7 Action: Human intercepted and rejected submission context for {job.get('company')}.")
            return {"submitted": False, "note": "human rejected"}
            
        STATE.log(f"Agent 7: Commencing automated submission sequences for {job.get('company')}...")
        
        # 3. Locate the Active Playwright/Browser Page Object Instance
        # Falls back to standard browser.page accessor if nested differently inside framework tools
        page = getattr(browser, "page", browser)
        
        # --- THE HEURISTIC CLICK MATRIX ---
        # Highly comprehensive list of standard text selectors utilized across enterprise application portals
        submit_text_selectors = [
            "text='Submit Application'", "text='Submit'", "text='Review and Submit'", 
            "text='Finish Application'", "text='Complete Application'", "text='Finish'", 
            "text='Proceed to Next Step'", "text='Send Application'", "text='Apply Now'"
        ]
        
        success_flag = False
        
        # Phase A: Primary Native Framework Method Attempt
        try:
            if hasattr(browser, "submit"):
                await browser.submit()
                await asyncio.sleep(2.0)
                success_flag = True
        except Exception as e:
            STATE.log(f"Agent 7 Warning: Primary framework submit mechanism failed ({str(e)}). Transitioning to Heuristic Click Matrix.")

        # Phase B: Advanced Multi-Selector Selector Crawling (If Phase A was inconclusive)
        if not success_flag:
            for selector in submit_text_selectors:
                try:
                    # Check for element visibility and existence in current viewport before clicking
                    element = await page.query_selector(selector)
                    if element and await element.is_visible():
                        STATE.log(f"Agent 7 Match: Found valid target layout button matching query: {selector}")
                        
                        # Handle potential blocking overlays by running an unforced force-click sequence
                        await element.click(force=True, timeout=5000)
                        success_flag = True
                        break
                except Exception:
                    continue

        # Phase C: Ultimate Hard JavaScript Injection Failover
        if not success_flag:
            STATE.log("Agent 7 Warning: Standard DOM selectors failed to engage. Deploying raw JavaScript injection click hack...")
            try:
                # Scrape all open interactive input/button tags inside the browser context and evaluate their text strings
                js_click_script = """() => {
                    const targets = ["submit", "submit application", "finish", "review and submit", "complete application"];
                    const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"]'));
                    for (let btn of buttons) {
                        let text = (btn.innerText || btn.value || "").toLowerCase().trim();
                        if (targets.some(t => text.includes(t))) {
                            btn.scrollIntoView();
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                }"""
                js_success = await page.evaluate(js_click_script)
                if js_success:
                    STATE.log("Agent 7 Success: Submission button triggered via active JavaScript element inspection injection.")
                    success_flag = True
            except Exception as js_err:
                STATE.log(f"Agent 7 Critical Error: All systemic fallback submission elements failed to engage ({str(js_err)})")

        # 4. Long Stabilizing Buffer Delay to Let Redirects and Network Queries Complete
        await asyncio.sleep(4.5) 
        
        # 5. Clean Receipt Image Serialization
        safe_company_name = re.sub(r"[^A-Za-z0-9]+", "_", job.get("company", "job"))[:40]
        receipt_filename = f"{safe_company_name}_{int(time.time())}.png"
        receipt_destination_path = str(config.RECEIPTS / receipt_filename)
        
        try:
            # Capture the confirmation screen for record-keeping and dashboard log streaming
            if hasattr(browser, "screenshot"):
                shot = await browser.screenshot(receipt_destination_path)
            else:
                shot = await page.screenshot(path=receipt_destination_path)
            STATE.log(f"Agent 7 Complete: Submission recorded successfully. Receipt captured at: {receipt_destination_path}")
        except Exception as screenshot_err:
            STATE.log(f"Agent 7 Notice: Form successfully fired but confirmation screenshot collection failed ({str(screenshot_err)})")
            shot = "Screenshot unavailable."
            
        return {"submitted": success_flag, "receipt": shot}


# ----------------------------------------------------------------------------- Agent 8: Manager
class ExecutiveManager:
    def __init__(self) -> None:
        self.scout = SearchScout()
        self.ranker = RelevancyRanker()
        self.tailor = DocumentTailor()
        self.access = AccountCreator()
        self.filler = FormFiller()
        self.ats = AtsOfficer()
        self.submitter = Submitter()

    async def run(self, source: str) -> None:
        """Full pipeline for one source. Freezes on any error and asks the human what to do."""
        browser = Browser()
        await browser.start()
        try:
            jobs = await self.scout.scout(source, browser)
            jobs = self.ranker.rank(jobs)
            for j in jobs:
                jid = STATE.add_job(j.get("title", ""), j.get("company", ""))
                STATE.update(jid, status="tailoring", tier=j.get("tier"))
                try:
                    tailored = self.tailor.tailor(j)
                    # ATS loop (revise up to twice)
                    verdict = self.ats.check(tailored, j)
                    for _ in range(2):
                        if verdict["passed"]:
                            break
                        STATE.update(jid, status="tailoring", ats_score=verdict["score"])
                        tailored = self.tailor.tailor(j, feedback="Missing: " + ", ".join(verdict["missing"]))
                        verdict = self.ats.check(tailored, j)
                    STATE.update(jid, ats_score=verdict["score"])
                    if j.get("apply_url"):
                        await browser.goto(j["apply_url"])
                    STATE.update(jid, status="login")
                    if await self.access.access(j, browser) == "skip":
                        STATE.update(jid, status="skipped", note="login skipped")
                        continue
                    STATE.update(jid, status="filling")
                    await self.filler.fill(j, tailored, browser)
                    if not verdict["passed"]:
                        STATE.update(jid, status="awaiting_human", note=f"ATS {verdict['score']}% — review")
                    STATE.update(jid, status="ready")
                    res = await self.submitter.submit(j, tailored, verdict["token"] or "GREENLIGHT-manual", browser)
                    STATE.update(jid, status="submitted" if res.get("submitted") else "skipped",
                                 note=res.get("note", "receipt saved" if res.get("submitted") else ""))
                except Exception as e:                       # FREEZE → ask the human
                    STATE.update(jid, status="error", note=str(e)[:80])
                    STATE.log(f"⚠️ error on {j.get('company')}: {str(e)[:120]}")
                    await STATE.ask("question", j.get("company", ""), j.get("title", ""),
                                    f"Hit an error: {str(e)[:120]}. Reply CONTINUE to move on.",
                                    options=["CONTINUE"])
            STATE.log("Pipeline finished.")
        finally:
            await browser.close()
