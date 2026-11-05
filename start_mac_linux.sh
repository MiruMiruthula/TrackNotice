#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv || { echo "Python 3.10+ needed"; exit 1; }
source .venv/bin/activate
pip install -q -r requirements.txt || { echo "package install failed"; exit 1; }
[ -f .env ] || cp .env.example .env
python manage.py initdb
echo "Open http://127.0.0.1:5000   (admin token: see .env, default admin123)"
(sleep 4; (xdg-open http://127.0.0.1:5000 || open http://127.0.0.1:5000) >/dev/null 2>&1) &
python manage.py run
