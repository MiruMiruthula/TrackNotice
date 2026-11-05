"""A local SIMULATED transport-authority website used for the live demo and mentor challenges.
Scenario files live in demo_source/scenarios/*.html and can be edited by hand while the app is running -
the mentor can extend an outage, drop an end date, etc. and press 'Refresh now'. Dates in the files are
templates like {{d:+1@22:00}} (tomorrow 22:00 IST) or {{h:+5}} (now + 5 hours), so the demo never goes stale."""
import re
from datetime import timedelta
from pathlib import Path

from flask import Blueprint, Response, abort, current_app

from .config import BASE_DIR
from .timeutil import get_tz, utcnow

bp = Blueprint("demo_source", __name__, url_prefix="/demo-source")
SCEN_DIR = BASE_DIR / "demo_source" / "scenarios"
SCENARIOS = ("baseline", "update", "malformed", "failure")
_TOKEN = re.compile(r"\{\{(d|iso|h|date):([+-]\d+)(?:@(\d{2}):(\d{2}))?\}\}")


def render_template_text(text, now=None, tzname="Asia/Kolkata"):
    tz = get_tz(tzname)
    now_local = (now or utcnow()).astimezone(tz)

    def sub(m):
        kind, off, hh, mm = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        if kind == "h":
            dt = now_local + timedelta(hours=off)
            return dt.strftime("%d %b %Y, %I:%M %p")
        base = (now_local + timedelta(days=off)).replace(hour=int(hh or 0), minute=int(mm or 0), second=0, microsecond=0)
        if kind == "iso":
            return base.strftime("%Y-%m-%d %H:%M")
        if kind == "date":
            return base.strftime("%d %b %Y")
        return base.strftime("%d %b %Y, %I:%M %p")

    return _TOKEN.sub(sub, text)


def scenario_file(data_dir):
    return Path(data_dir) / "demo_scenario.txt"


def get_scenario(data_dir):
    f = scenario_file(data_dir)
    name = f.read_text().strip() if f.exists() else "baseline"
    return name if name in SCENARIOS else "baseline"


def set_scenario(data_dir, name):
    if name not in SCENARIOS:
        raise ValueError("unknown scenario")
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    scenario_file(data_dir).write_text(name)


@bp.before_request
def _only_in_demo():
    if not current_app.config["DEMO_MODE"]:
        abort(404)


@bp.route("/robots.txt")
def robots():
    return Response("User-agent: *\nAllow: /demo-source/\nDisallow: /demo-source/private/\nCrawl-delay: 1\n",
                    mimetype="text/plain")


@bp.route("/advisories")
def advisories():
    scen = get_scenario(current_app.config["DATA_DIR"])
    if scen == "failure":
        return Response("Service temporarily unavailable", status=503, headers={"Retry-After": "1"})
    page = SCEN_DIR / f"{scen}.html"
    return Response(render_template_text(page.read_text(encoding="utf-8")), mimetype="text/html")


@bp.route("/feed.xml")
def feed():
    scen = get_scenario(current_app.config["DATA_DIR"])
    if scen == "failure":
        return Response("Service temporarily unavailable", status=503)
    f = SCEN_DIR / ("feed_update.xml" if scen == "update" else "feed.xml")
    return Response(render_template_text(f.read_text(encoding="utf-8")), mimetype="application/rss+xml")


@bp.route("/private/internal")
def private():     # disallowed by robots.txt - used to prove the crawler respects it
    return Response("internal", mimetype="text/plain")
