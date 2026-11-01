#!/usr/bin/env bash
# One-time setup. Works on Kubuntu/Linux and macOS.
set -e
cd "$(dirname "$0")"

echo "==> Creating virtualenv (.venv)"
python3 -m venv .venv
source .venv/bin/activate

echo "==> Installing dependencies"
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Installing the Playwright Chromium browser"
python -m playwright install chromium

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Created .env — open it and set ANTHROPIC_API_KEY (and Telegram token/chat id)."
fi

echo "==> Drop your resume.pdf (and optional transcript.pdf) into ./data/"
echo "==> Done. Run:  source .venv/bin/activate && python main.py"
