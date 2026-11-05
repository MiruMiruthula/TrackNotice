#!/usr/bin/env python3
"""Command line helper.   python manage.py initdb | run | refresh [--source ID] | loop | inspect URL | subscribe EMAIL --routes 12B,21G"""
import argparse
import json
import secrets
import sys

from app import create_app
from app.db import connect
from app.timeutil import to_iso, utcnow


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("initdb", help="create / migrate the database")
    r = sub.add_parser("run", help="start the web app (with scheduler)")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=5000)
    rf = sub.add_parser("refresh", help="refresh sources once and print a summary (use from cron)")
    rf.add_argument("--source")
    sub.add_parser("loop", help="run only the scheduler (no web server)")
    i = sub.add_parser("inspect", help="check robots.txt and analyse a real source page before configuring it")
    i.add_argument("url")
    s = sub.add_parser("subscribe", help="add a subscriber")
    s.add_argument("email")
    s.add_argument("--routes", default="")
    s.add_argument("--areas", default="")
    a = ap.parse_args()

    if a.cmd == "run":
        app = create_app()
        print(f"\n  TransitNotice running at http://{a.host}:{a.port}   (admin: /admin)\n")
        app.run(host=a.host, port=a.port, threaded=True, use_reloader=False)
        return
    app = create_app(start_scheduler=False)
    if a.cmd == "initdb":
        print("Database ready:", app.config["DATABASE_PATH"])
    elif a.cmd == "inspect":
        from app.inspect_page import inspect
        sys.exit(0 if inspect(a.url, app.extensions["fetcher"]) else 1)
    elif a.cmd == "refresh":
        print(json.dumps(app.extensions["refresher"].refresh_all(only=a.source), indent=2))
    elif a.cmd == "loop":
        from app.refresh import Scheduler
        sch = Scheduler(app.extensions["refresher"], app.config["REFRESH_INTERVAL_MINUTES"], first_delay=0)
        sch.start()
        sch.join()
    elif a.cmd == "subscribe":
        conn = connect(app.config["DATABASE_PATH"])
        filt = json.dumps({"routes": [x.strip().upper() for x in a.routes.split(",") if x.strip()],
                           "areas": [x.strip() for x in a.areas.split(",") if x.strip()], "types": []}, sort_keys=True)
        with conn:
            conn.execute("INSERT OR IGNORE INTO subscribers(contact,subscriber_filter,unsubscribe_token,created_at) VALUES (?,?,?,?)",
                         (a.email.lower(), filt, secrets.token_urlsafe(24), to_iso(utcnow())))
        print("Subscribed", a.email, filt)


if __name__ == "__main__":
    sys.exit(main())
