"""The cloud brain. Every agent offloads cognition here — NO local LLM is ever started.

Thin wrapper over the Anthropic SDK (Claude). `generate` returns text; `generate_json` parses a
single JSON object out of the reply; `research` uses Claude's server-side web_search tool.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
from typing import Optional

from . import config

_client = None


def client():
    """Lazy Anthropic API client (only used when LLM_BACKEND=api)."""
    global _client
    if _client is None:
        from anthropic import Anthropic
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (LLM_BACKEND=api needs it).")
        _client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def _cli(prompt: str, system: str = "", timeout: int = 180) -> str:
    """Run one prompt through the `claude` CLI (uses your subscription — no per-token cost)."""
    full = f"{system}\n\n{prompt}" if system else prompt
    cmd = [config.CLI_BIN, "-p", full]
    if config.CLI_MODEL:
        cmd += ["--model", config.CLI_MODEL]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            from .state import STATE
            STATE.log(f"CLI error: {(out.stderr or out.stdout).strip()[:160]}")
            return ""
        return (out.stdout or "").strip()
    except Exception as e:
        from .state import STATE
        STATE.log(f"CLI exception: {str(e)[:140]}")
        return ""


def _api(prompt: str, system: str, max_tokens: int, model: Optional[str]) -> str:
    try:
        kw = {"model": model or config.CLAUDE_MODEL, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]}
        if system:
            kw["system"] = system
        r = client().messages.create(**kw)
        return "\n".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
    except Exception as e:
        from .state import STATE
        STATE.log(f"LLM error: {str(e)[:140]}")
        return ""


def generate(prompt: str, system: str = "", max_tokens: int = 2000,
             model: Optional[str] = None, backend: Optional[str] = None) -> str:
    """One-shot completion. `backend` ('api'|'cli') overrides the default; an 'api' request with no
    key falls back to CLI. Returns text, '' on failure."""
    b = (backend or config.LLM_BACKEND).lower()
    if b == "api" and not config.ANTHROPIC_API_KEY:
        b = "cli"
    if b == "cli":
        return _cli(prompt, system)
    return _api(prompt, system, max_tokens, model)


def generate_json(prompt: str, system: str = "", max_tokens: int = 2000,
                  backend: Optional[str] = None) -> dict:
    """Completion that must return a JSON object. Returns {} if it can't be parsed."""
    raw = generate(prompt + "\n\nReply with ONLY a single JSON object.", system, max_tokens, backend=backend)
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return {}
    # strict=False lets JSON string values hold literal newlines/tabs — essential because the
    # Tailor returns a multi-paragraph cover letter inside a string (strict=True rejects it → {}).
    try:
        return json.loads(m.group(0), strict=False)
    except Exception:
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0)), strict=False)
        except Exception:
            return {}


def vision_pick(image_bytes: bytes, instruction: str, candidates: list) -> int:
    """Computer vision: show Claude a screenshot + a numbered list of clickable elements and ask
    which single one to click to carry out `instruction`. Returns that mark number, or -1.

    Vision needs the multimodal API (the CLI can't take images), so this always uses the key."""
    if not config.ANTHROPIC_API_KEY or not image_bytes or not candidates:
        return -1
    b64 = base64.standard_b64encode(image_bytes).decode()
    listing = "\n".join(f"[{c['mark']}] <{c.get('tag','')}> {c.get('text','')}".rstrip()
                        for c in candidates[:140])
    prompt = (
        f"{instruction}\n\n"
        "The screenshot is a web form. Each clickable element is listed with its number:\n"
        f"{listing}\n\n"
        "Decide which SINGLE element to click to do this. Reply with ONLY that number "
        "(e.g. 42). If none fits, reply -1.")
    try:
        r = client().messages.create(
            model=config.CLAUDE_MODEL, max_tokens=12,
            messages=[{"role": "user", "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": prompt}]}],
        )
        txt = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        m = re.search(r"-?\d+", txt)
        return int(m.group(0)) if m else -1
    except Exception as e:
        from .state import STATE
        STATE.log(f"Vision error: {str(e)[:120]}")
        return -1


def research(query: str, max_tokens: int = 700) -> str:
    """Company research. On the API backend this uses the live web_search tool; on the CLI backend
    the `claude` CLI answers (it may use its own web tools, else it grounds from model knowledge).
    Research is optional — '' on failure never breaks a caller."""
    if config.LLM_BACKEND == "cli":
        return _cli("Research this and answer concisely (use web search if available):\n" + query)
    try:
        r = client().messages.create(
            model=config.CLAUDE_MODEL, max_tokens=max_tokens,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": query}],
        )
        return "\n".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
    except Exception:
        return ""
