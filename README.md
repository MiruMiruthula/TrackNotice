# TransitNotice - Public Transport Service Update Tracker  (WEB-07)

Monitors **official, publicly available** transport notices for Chennai, extracts affected routes / validity window /
change type, keeps a full revision history, shows how fresh every notice is and where it came from, archives expired or
withdrawn notices, and sends **one** alert per new or revised advisory.  Flask + SQLite + standard library (plus `pypdf`).

## Real data - how it works

The official Southern Railway website (`sr.indianrailways.gov.in`) **refuses automated downloads** (the app and other tools get HTTP 404 /
client errors), and we do not try to get around that. So real notices are added the way a person normally uses a public website:

1. Open the official press-release page: <https://sr.indianrailways.gov.in/view_section.jsp?fontColor=black&backgroundColor=LIGHTSTEELBLUE&lang=0&id=0%2C4%2C268>
   and choose **Chennai** under "Press Release - Divisions".
2. Click a notice (e.g. "Changes in the pattern of Train Services"), select all the text (Ctrl+A), copy (Ctrl+C).
3. In the app: **/admin -> Add real notices from the official website** -> paste the web address and the text -> **Import this notice**.
   (A saved PDF can be uploaded instead.) The notice is parsed immediately: routes/train numbers, dates, type, summary, official link, revision history.

Importing the same notice twice does nothing; importing a revised notice with the same number updates it and keeps the history. One real official
release (PUB/MAS/2024/10/18, Chennai Division) is pre-loaded so the Archive tab is not empty. There is **no demo data** in the default setup.

| Source | Status |
|---|---|
| **Notices you copy from the official website** (`sr-manual-import`) | **Works.** Source link on every notice points to the official page you give. |
| Automatic PDF download (`sr-chennai-press-archive`) | **Disabled** - the site answers HTTP 404/403. Kept only for reference. |
| CMRL announcements (`cmrl-announcements`) | Needs a URL + selectors (`python manage.py inspect <url>`); only enable if robots.txt allows automated access. |

### Formats understood
* Division PDFs / text: `No. PUB/MAS/YYYY/MM/NN  Date: dd.mm.yyyy ... PRESS RELEASE ...`
* Website detail pages: `326/2026-27 21-08-2026 Chennai Regulation of Train Service ...`
Type (Cancellation / Diversion / Timetable change / Planned maintenance), service classes (EMU, MEMU, MRTS, EXPRESS) and every train number become
searchable "routes". Window = first announced date to the end time of the block (approximation); "until further advice" -> open-ended.
A release saying an earlier block **"STANDS CANCELLED"** withdraws the earlier notice. Non-service releases (ceremonies, awards) are ignored.

## 1. Run it (Windows / Mac / Linux, Python 3.10+)

```bash
python -m venv .venv
.venv\Scripts\activate.bat           # Windows (cmd).   Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env               # Mac/Linux: cp .env.example .env   -> then edit SECRET_KEY and ADMIN_TOKEN
python manage.py initdb
python manage.py run                 # open http://127.0.0.1:5000
```
Open **/admin**, sign in with your `ADMIN_TOKEN`, press **Refresh all enabled sources**. (The scheduler also refreshes every `REFRESH_INTERVAL_MINUTES`, default 15.)
Needs internet access to `sr.indianrailways.gov.in`.

Tests: `python -m unittest discover -s tests -v` (69 tests, offline).  Evaluation numbers: `python tools/evaluate.py`.

## 2. Offline demo mode (optional - for tests and mentor challenges only)
`SOURCES_FILE=sources.demo.json` and `DEMO_MODE=1` in `.env` start a **simulated** agency website; `/admin` then shows scenario buttons
(update / malformed / failure) to demonstrate extension, withdrawal and source-error handling without a real incident. It is **off by default**; real runs show only real data.

## 3. Project layout
```
app/            Flask app + logic            tests/        unit + end-to-end tests (fake network, real release text fixtures)
  adapters/     fetcher (robots, backoff),   migrations/   SQL schema (versioned, repeatable)
                HTML / RSS / railway_pdf     docs/         source approval, diagrams, screenshots
  normalize.py  dates -> UTC, types, routes  sources.json  REAL source registry     sources.demo.json  simulated sources
  store.py      idempotent upsert, history   manage.py     CLI: initdb | run | refresh | loop | inspect | subscribe
  refresh.py    refresh cycle + scheduler    demo_source/  simulated agency pages (demo mode only)
  alerts.py     dedup alerts + delivery log
```

## 4. Configuration (`.env`)
| Variable | Default | Meaning |
|---|---|---|
| `SECRET_KEY`, `ADMIN_TOKEN` | random per run | **Set both.** Never commit `.env`. |
| `SR_PRESS_URL`, `CMRL_URL` | empty | official listing pages (see above) |
| `REFRESH_INTERVAL_MINUTES` | 15 | scheduler interval; "stale" after 2x interval + 1 min |
| `SCHEDULER_ENABLED` | 1 | `0` = run `python manage.py refresh` from cron |
| `DEMO_MODE` / `SOURCES_FILE` | 0 / sources.json | demo = simulated agency (see section 2) |
| `ALERT_MODE` | demo | `demo` logs to `data/alerts_demo.log`; `smtp` needs `SMTP_*` |
| `BOT_CONTACT` | - | your e-mail, shown in the crawler's User-Agent (set it!) |

## 5. Deployment
Local: `python manage.py run`.  Production-style: `gunicorn -w 1 --threads 8 "app:create_app()"` behind Nginx/HTTPS.
Docker: `docker build -t transitnotice . && docker run -p 5000:5000 --env-file .env -v tn_data:/app/data transitnotice`.
Cron instead of the built-in scheduler: `SCHEDULER_ENABLED=0` and `*/15 * * * * cd /path && .venv/bin/python manage.py refresh`.
Health: `GET /health`. Logs: `logs/transitnotice.log`. Back up `data/transitnotice.db`.

## 6. Ethics & limits
Public data only; robots.txt, terms and rate limits respected (10 s between requests to a host, each PDF downloaded once); no login bypass or
CAPTCHA evasion; subscribers: e-mail + filter only. Scheduled monitoring is **not real time**, is not an official service, and parsing of
agency documents is best-effort - always confirm with the agency (see `/about`).
