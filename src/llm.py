"""The cloud brain. Every agent offloads cognition here — NO local LLM is ever started.

Thin wrapper over the Anthropic SDK (Claude). `generate` returns text; `generate_json` parses a
single JSON object out of the reply; `research` uses Claude's server-side web_search tool.
"""
from __future__ import annotations

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
    try:
        return json.loads(m.group(0))
    except Exception:
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0)))
        except Exception:
            return {}


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
