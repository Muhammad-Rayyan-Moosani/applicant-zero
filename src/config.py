"""Central settings. Reads .env once; exposes paths and tunables."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent          # .../jobengine
load_dotenv(ROOT / ".env")

DATA = ROOT / "data"
CONFIG = ROOT / "config"
RECEIPTS = ROOT / "receipts"
LOGS = ROOT / "logs"
for _d in (DATA, RECEIPTS, LOGS):
    _d.mkdir(parents=True, exist_ok=True)

KNOWLEDGE_BASE = DATA / "knowledge_base.md"
PROFILE_JSON = DATA / "profile.json"
CREDENTIALS_LOG = LOGS / "credentials.log"             # site + email + status ONLY (never passwords)

# How the agents reach Claude:
#   cli → the `claude` CLI on your subscription (flat cost — best for a private server)
#   api → the Anthropic API (per-token)
LLM_BACKEND = os.getenv("LLM_BACKEND", "cli").lower()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")       # used by the API backend
CLI_BIN = (os.getenv("CLAUDE_CLI", "") or shutil.which("claude")
           or os.path.expanduser("~/.local/bin/claude"))           # the `claude` executable
CLI_MODEL = os.getenv("CLI_MODEL", "")                             # empty = CLI's default model

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8000"))

HEADLESS = os.getenv("HEADLESS", "0") == "1"
MIN_TIER = int(os.getenv("MIN_TIER", "3"))
MIN_ATS_SCORE = int(os.getenv("MIN_ATS_SCORE", "85"))

# A dedicated, persistent browser profile so logins the human does once are remembered.
BROWSER_PROFILE = DATA / "browser_profile"
BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
