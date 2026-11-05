import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask

from .adapters.base import Fetcher
from .config import BASE_DIR, load_config
from .db import connect, migrate, sync_sources
from .refresh import Refresher, Scheduler
from .sources import load_sources
from .timeutil import utcnow


def setup_logging(cfg):
    Path(cfg["LOG_DIR"]).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("transitnotice")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(Path(cfg["LOG_DIR"]) / "transitnotice.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def make_fetcher(cfg):
    ua = f"TransitNoticeBot/1.0 (student project; contact: {cfg['BOT_CONTACT']})"
    return Fetcher(ua, timeout=cfg["HTTP_TIMEOUT"], max_retries=cfg["HTTP_MAX_RETRIES"],
                   backoff_base=cfg["HTTP_BACKOFF_BASE"], max_bytes=cfg["HTTP_MAX_BYTES"])


def create_app(overrides=None, fetcher=None, now_fn=None, start_scheduler=None):
    cfg = load_config(overrides)
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(cfg)
    Path(cfg["DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    if not app.config.get("TESTING"):
        setup_logging(cfg)

    if not cfg["ADMIN_TOKEN"]:
        import secrets
        app.config["ADMIN_TOKEN"] = secrets.token_urlsafe(12)
        app.config["ADMIN_TOKEN_GENERATED"] = True
        logging.getLogger("transitnotice").warning(
            "ADMIN_TOKEN not set - generated a temporary one for this run: %s", app.config["ADMIN_TOKEN"])

    conn = connect(cfg["DATABASE_PATH"])
    try:
        migrate(conn, cfg["MIGRATIONS_DIR"])
        sync_sources(conn, load_sources(cfg["SOURCES_FILE"], cfg))
    finally:
        conn.close()

    app.extensions["now"] = now_fn or utcnow
    app.extensions["fetcher"] = fetcher or make_fetcher(cfg)
    app.extensions["refresher"] = Refresher(cfg, app.extensions["fetcher"], now_fn=app.extensions["now"])

    from . import demo_source, web
    app.register_blueprint(web.bp)
    app.register_blueprint(demo_source.bp)

    if start_scheduler is None:
        start_scheduler = cfg["SCHEDULER_ENABLED"] and not app.config.get("TESTING")
    if start_scheduler and (not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"):
        sch = Scheduler(app.extensions["refresher"], cfg["REFRESH_INTERVAL_MINUTES"])
        sch.start()
        app.extensions["scheduler"] = sch
    return app
