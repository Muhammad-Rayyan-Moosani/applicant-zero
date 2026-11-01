"""Playwright browser tool — the agents' hands. One persistent, headful-by-default Chromium so
logins the human performs once are remembered. Async throughout (fits the asyncio orchestrator).

Exposes high-level ops the agents need: open, read text, scrape form fields (with real labels),
fill a field, upload a file, detect a login wall, click 'apply', and screenshot.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from playwright.async_api import async_playwright

from .. import config

# JS that reads a field's REAL human label (climbs to the field container; ignores ids/asterisks).
_LABEL_JS = r"""
(el) => {
  const clean = s => (s||'').replace(/\s+/g,' ').trim();
  const bad = s => { s=clean(s); return !s||s.length<2||/[0-9a-f]{6,}(-[0-9a-f]{3,})+/i.test(s)
      ||/^_?systemfield/i.test(s)||/_gh_quest_/i.test(s); };
  let t = el.getAttribute('aria-label') || '';
  if (bad(t) && el.id){ const l=document.querySelector('label[for="'+CSS.escape(el.id)+'"]'); if(l) t=l.innerText; }
  if (bad(t)){ const l=el.closest('label'); if(l) t=l.innerText; }
  if (bad(t)){ const lb=el.getAttribute('aria-labelledby'); if(lb){ const e=document.getElementById(lb); if(e) t=e.innerText; } }
  if (bad(t)){ const fs=el.closest('fieldset'); const lg=fs&&fs.querySelector('legend'); if(lg) t=lg.innerText; }
  if (bad(t)){ let n=el; for(let i=0;i<5&&n;i++){ n=n.parentElement; if(!n)break;
      const lab=n.querySelector('label,legend,[class*=label],[class*=title],[class*=question]');
      if(lab){ const x=clean(lab.innerText); if(!bad(x)){ t=x; break; } } } }
  if (bad(t)) t = el.getAttribute('placeholder') || '';
  t=clean(t); return bad(t) ? '' : t.slice(0,160);
}
"""

_LOGIN_WORDS = ("sign in to apply", "log in to apply", "create an account to apply",
                "please sign in", "you must be signed in", "register to apply",
                "create a profile to apply", "sign in", "log in", "create account")


class Browser:
    def __init__(self) -> None:
        self._pw = None
        self._ctx = None
        self.page = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            str(config.BROWSER_PROFILE), headless=config.HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1440, "height": 900},
        )
        self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()

    async def close(self) -> None:
        try:
            if self._ctx:
                await self._ctx.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def goto(self, url: str):
        """Navigate and return the live Playwright Page (so callers can `page.evaluate(...)`)."""
        await self.page.goto(url, timeout=60000, wait_until="domcontentloaded")
        try:
            await self.page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        await asyncio.sleep(1.5)
        return self.page

    async def text(self, limit: int = 8000) -> str:
        try:
            return (await self.page.inner_text("body"))[:limit]
        except Exception:
            return ""

    async def dismiss_cookies(self) -> None:
        for t in ("Decline", "Reject All", "Reject", "Only essential", "Accept all", "Accept"):
            try:
                el = await self.page.query_selector(f'button:has-text("{t}"), a:has-text("{t}")')
                if el and await el.is_visible():
                    await el.click(timeout=3000)
                    await asyncio.sleep(0.6)
                    return
            except Exception:
                pass

    async def is_logged_in(self) -> bool:
        """Heuristic: a visible sign-in/create-account button ⇒ NOT logged in."""
        for t in ("Sign In", "Sign in", "Log In", "Log in", "Create Account", "Create an Account"):
            try:
                el = await self.page.query_selector(f'button:has-text("{t}"), a:has-text("{t}")')
                if el and await el.is_visible():
                    return False
            except Exception:
                pass
        return True

    async def login_wall(self) -> bool:
        body = (await self.text(1500)).lower()
        return any(w in body for w in _LOGIN_WORDS[:8])

    async def click_apply(self) -> None:
        for t in ("Apply for this job", "Apply Now", "Apply now", "Apply", "Start Application"):
            try:
                el = await self.page.query_selector(f'button:has-text("{t}"), a:has-text("{t}")')
                if el and await el.is_visible():
                    await el.click()
                    await asyncio.sleep(2)
                    return
            except Exception:
                pass

    async def scrape_fields(self) -> list[dict]:
        """Return [{ref,label,type,options,required}] for visible fillable controls."""
        out = []
        sels = "input,textarea,select,div.select__control,[role=combobox]"
        els = await self.page.query_selector_all(sels)
        n = 0
        for el in els:
            try:
                tag = (await el.evaluate("e=>e.tagName")).lower()
                typ = (await el.get_attribute("type")) or (await el.get_attribute("role")) or tag
                typ = typ.lower()
                if typ in ("hidden", "submit", "button", "file"):
                    continue
                if not await el.is_visible():
                    continue
                label = await el.evaluate(_LABEL_JS)
                ref = f"f{n}"
                await el.evaluate("(e,r)=>e.setAttribute('data-je',r)", ref)
                n += 1
                rec = {"ref": ref, "label": label, "type": "select" if tag == "select" else typ,
                       "required": bool(await el.get_attribute("required") or
                                        (await el.get_attribute("aria-required")) == "true")}
                if tag == "select":
                    rec["options"] = await el.evaluate(
                        "e=>[...e.options].map(o=>o.text.trim()).filter(Boolean)")
                out.append(rec)
            except Exception:
                continue
        return out

    async def fill(self, ref: str, value: str) -> bool:
        el = await self.page.query_selector(f'[data-je="{ref}"]')
        if not el:
            return False
        try:
            tag = (await el.evaluate("e=>e.tagName")).lower()
            if tag == "select":
                try:
                    await el.select_option(label=value)
                except Exception:
                    await el.select_option(value)
                return True
            cls = (await el.get_attribute("class")) or ""
            if "select__control" in cls:                     # react-select
                inp = await el.query_selector("input")
                if inp:
                    await inp.click()
                    await inp.fill(value)
                    await asyncio.sleep(0.6)
                    opt = await self.page.query_selector(
                        f'div.select__option:has-text("{value}"), [role=option]:has-text("{value}")')
                    if opt:
                        await opt.click()
                        return True
                return False
            await el.fill(value)
            return True
        except Exception:
            return False

    async def new_page(self):
        """Open a second tab (used by the Ranker to fetch a posting's full description)."""
        return await self._ctx.new_page()

    async def select_option(self, ref: str, value: str) -> bool:
        """Native <select> or react-select — fill() already handles both."""
        return await self.fill(ref, value)

    async def check_element(self, ref: str, value: str) -> bool:
        """Tick a checkbox / pick a radio by ref. Affirmative value or a label match → check it."""
        el = await self.page.query_selector(f'[data-je="{ref}"]')
        if not el:
            return False
        try:
            lab = ((await el.evaluate(_LABEL_JS)) or "").lower()
            v = str(value).strip().lower()
            affirmative = v in ("yes", "true", "1", "on", "agree", "i agree", "i consent", "checked")
            if affirmative or (v and (v in lab or lab in v)):
                await el.check()
                return True
            return False
        except Exception:
            return False

    _NEXT = ("Save and Continue", "Save & Continue", "Continue", "Next Step", "Next", "Save and continue")

    async def has_next_step(self) -> bool:
        for t in self._NEXT:
            el = await self.page.query_selector(f'button:has-text("{t}"), [role=button]:has-text("{t}")')
            if el and await el.is_visible():
                return True
        return False

    async def click_next_step(self) -> bool:
        for t in self._NEXT:
            el = await self.page.query_selector(f'button:has-text("{t}"), [role=button]:has-text("{t}")')
            if el and await el.is_visible():
                try:
                    await el.click()
                    await asyncio.sleep(1.5)
                    return True
                except Exception:
                    pass
        return False

    async def is_submit_page(self) -> bool:
        return await self.has_submit()

    async def has_upload_zone(self, name: str) -> bool:
        inputs = await self.page.query_selector_all("input[type=file]")
        for f in inputs:
            try:
                lab = ((await f.evaluate(_LABEL_JS)) or "").lower()
            except Exception:
                lab = ""
            if name.lower() in lab:
                return True
        return bool(inputs) if name.lower() == "resume" else False

    async def upload(self, label_contains: str, path: str) -> bool:
        """Attach `path` to the file input whose label matches (resume/cover/transcript)."""
        inputs = await self.page.query_selector_all("input[type=file]")
        for f in inputs:
            try:
                lab = (await f.evaluate(_LABEL_JS)).lower()
                if label_contains.lower() in lab or (label_contains == "resume" and not lab):
                    await f.set_input_files(path)
                    return True
            except Exception:
                continue
        return False

    async def has_submit(self) -> bool:
        for sel in ('button:has-text("Submit Application")', 'button:has-text("Submit")',
                    'input[type=submit]'):
            el = await self.page.query_selector(sel)
            if el and await el.is_visible():
                return True
        return False

    async def submit(self) -> None:
        for sel in ('button:has-text("Submit Application")', 'button:has-text("Submit")',
                    'input[type=submit]'):
            el = await self.page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await asyncio.sleep(3)
                return

    async def post_submit_state(self) -> str:
        """After clicking submit: 'confirmed' | 'error' | 'unknown' — drives self-healing."""
        body = (await self.text(3000)).lower()
        if any(s in body for s in ("thank you for applying", "application received", "successfully "
                                   "submitted", "we have received your application", "application complete",
                                   "thanks for applying")):
            return "confirmed"
        if any(s in body for s in ("is required", "please fix", "required field", "please correct",
                                   "please complete", "invalid", "error", "please enter")):
            return "error"
        return "unknown"

    async def errors_on_page(self) -> list[str]:
        """Visible validation-error messages, for the swarm to reason about."""
        try:
            return await self.page.evaluate(
                "() => [...document.querySelectorAll('[class*=error],[aria-invalid=true],[role=alert]')]"
                ".map(e=>(e.innerText||'').trim()).filter(t=>t&&t.length<120).slice(0,10)")
        except Exception:
            return []

    async def screenshot(self, path: str) -> str:
        try:
            await self.page.screenshot(path=path, full_page=True)
            return path
        except Exception:
            return ""
