"""Telegram remote gateway (Agent 8's human interface).

- Pushes each pending human-gate as a Markdown message with inline buttons.
- Long-polls getUpdates in a background thread; routes button taps / text replies back into STATE
  (resolving the asyncio.Event the pipeline is blocked on).
- A plain URL or `/apply <url>` message starts a new pipeline run.

Native httpx (no extra framework) so it runs cleanly in a thread alongside asyncio.
"""
from __future__ import annotations

import threading
import time

import httpx

from . import config
from .state import STATE, Pending, set_notifier

_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
_on_source = None            # callback(url) → start a pipeline (set by main)


def configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def _post(method: str, payload: dict) -> dict:
    try:
        r = httpx.post(f"{_API}/{method}", json=payload, timeout=30)
        return r.json()
    except Exception:
        return {}


def send(text: str, buttons: list[tuple[str, str]] | None = None) -> None:
    # Plain text (NO parse_mode): field labels contain '*', '_', '[' etc. (e.g. "How did you hear
    # about us?*"), which break Telegram's Markdown parser and make the whole message fail to send.
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d}] for t, d in buttons]}
    _post("sendMessage", payload)


def _notify(p: Pending) -> None:
    """Push a pending gate to Telegram with inline buttons (or a text prompt)."""
    head = f"❓ {p.kind.upper()} — {p.company}\n{p.role}\n\n{p.prompt}"
    if p.options:
        btns = [(opt, f"ans|{p.id}|{i}") for i, opt in enumerate(p.options)]
        send(head, btns)
    else:
        send(head + "\n\nReply with your answer.")


def start() -> None:
    """Launch the long-poll loop in a daemon thread. No-op if not configured."""
    if not configured():
        STATE.log("Telegram not configured — dashboard-only mode.")
        return
    set_notifier(_notify)
    send("🟢 Job Application Engine online. Send a job URL or `/apply <url>` to begin.")
    threading.Thread(target=_poll_loop, daemon=True).start()


def set_source_handler(fn) -> None:
    global _on_source
    _on_source = fn


def _poll_loop() -> None:
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            r = httpx.get(f"{_API}/getUpdates", params=params, timeout=40).json()
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                _handle(upd)
        except Exception:
            time.sleep(3)


def _handle(upd: dict) -> None:
    # button tap
    cq = upd.get("callback_query")
    if cq:
        data = cq.get("data", "")
        _post("answerCallbackQuery", {"callback_query_id": cq["id"]})
        if data.startswith("ans|"):
            _, pid, idx = data.split("|", 2)
            p = STATE.pending(pid)
            choice = p.options[int(idx)] if (p and int(idx) < len(p.options)) else ""
            if STATE.answer(pid, choice):
                send(f"✔️ Noted: *{choice}*")
        return
    # text message
    msg = upd.get("message") or {}
    text = (msg.get("text") or "").strip()
    if not text:
        return
    low = text.lower()
    if low.startswith("/apply") or low.startswith("http"):
        url = text.split(" ", 1)[1].strip() if low.startswith("/apply") and " " in text else text
        if _on_source and url.startswith("http"):
            send(f"🚀 Starting pipeline on:\n{url}")
            _on_source(url)
        else:
            send("Send a full http(s) job URL.")
        return
    # otherwise: treat as the answer to the latest open question
    pid = STATE.latest_pending_id()
    if pid and STATE.answer(pid, text):
        send("✔️ Got it — applying your answer.")
    else:
        send("No question is open right now. Send a job URL to start.")
