import hashlib
import hmac
import json
import re
import secrets
from functools import wraps
from pathlib import Path

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for)

from . import demo_source, freshness, store
from .db import connect
from .normalize import NOTICE_TYPES, parse_routes
from .sources import load_sources
from .timeutil import fmt_local, from_iso, humanize_age, to_iso

bp = Blueprint("web", __name__)
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


# ------------------------------------------------------------------------------------------ helpers
def now():
    return current_app.extensions["now"]()


def db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE_PATH"])
    return g.db


@bp.teardown_app_request
def _close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def _admin_header_ok():
    tok = request.headers.get("X-Admin-Token", "")
    return bool(tok) and hmac.compare_digest(tok, current_app.config["ADMIN_TOKEN"])


@bp.before_request
def _csrf_protect():
    if request.method == "POST" and not _admin_header_ok():
        sent = request.form.get("csrf_token", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Invalid or missing CSRF token")


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if session.get("is_admin") or _admin_header_ok():
            return fn(*a, **kw)
        if request.path.startswith("/api/") or request.method == "POST":
            abort(403)
        return redirect(url_for("web.admin_login", next=request.path))
    return wrapper


@bp.app_context_processor
def inject():
    return {"csrf_token": csrf_token, "tz": current_app.config["DISPLAY_TZ"], "types": NOTICE_TYPES,
            "demo_mode": current_app.config["DEMO_MODE"]}


@bp.app_template_filter("local")
def _local(iso):
    return fmt_local(iso, current_app.config["DISPLAY_TZ"])


@bp.app_template_filter("ago")
def _ago(iso):
    return humanize_age(iso, now())


TYPE_ICONS = {"DIVERSION": "\u21aa\ufe0f", "SERVICE_DISRUPTION": "\u26a0\ufe0f", "PLANNED_MAINTENANCE": "\U0001F6E0\ufe0f",
              "TIMETABLE_CHANGE": "\U0001F552", "STOP_CLOSURE": "\U0001F6A7", "CANCELLATION": "\u26d4", "OTHER": "\u2139\ufe0f"}


@bp.app_template_filter("icon")
def _icon(t):
    return TYPE_ICONS.get(t, "\u2139\ufe0f")


@bp.app_template_filter("pretty")
def _pretty(s):
    return (s or "").replace("_", " ").title()


def sources_with_health():
    cfg, n = current_app.config, now()
    out = []
    for s in db().execute("SELECT * FROM sources WHERE enabled=1 ORDER BY name").fetchall():
        out.append({"row": s, "health": freshness.source_health(s, n, cfg["STALE_AFTER_MINUTES"])})
    return out


def _filters():
    a = request.args
    return {k: (a.get(k) or "").strip()[:100] for k in ("q", "route", "station", "area", "type", "date", "status", "source")}


def _serialize(row, routes, srcs, n, cfg):
    eff = store.effective_status(row, to_iso(n))
    fr = freshness.notice_freshness(srcs[row["source_id"]], row["last_confirmed_at"], n, cfg["STALE_AFTER_MINUTES"])
    progress = None
    if row["valid_to"] and eff == "active":
        span = (from_iso(row["valid_to"]) - from_iso(row["valid_from"])).total_seconds()
        progress = int(max(0, min(100, 100 * (n - from_iso(row["valid_from"])).total_seconds() / span))) if span > 0 else 100
    return {
        "notice_id": row["notice_id"], "agency": row["agency"], "route_id": routes, "area": row["area"],
        "source_url": row["source_url"], "type": row["type"], "title": row["title"], "summary": row["summary"],
        "published_at": row["published_at"], "valid_from": row["valid_from"], "valid_to": row["valid_to"],
        "status": eff, "end_announced": row["valid_to"] is not None, "observed_at": row["observed_at"],
        "last_confirmed_at": row["last_confirmed_at"], "content_hash": row["content_hash"],
        "revision_id": row["revision_id"], "change_summary": row["change_summary"],
        "alert_status": row["alert_status"], "alerted_at": row["alerted_at"],
        "freshness": fr, "source_id": row["source_id"], "progress": progress,
    }


def _list(limit, offset):
    cfg, n = current_app.config, now()
    f = _filters()
    rows, total = store.query_notices(db(), f, to_iso(n), cfg["DISPLAY_TZ"], limit, offset)
    routes = store.routes_for(db(), [r["notice_id"] for r in rows])
    srcs = {s["source_id"]: s for s in db().execute("SELECT * FROM sources")}
    return [_serialize(r, routes[r["notice_id"]], srcs, n, cfg) for r in rows], total, f


# ------------------------------------------------------------------------------------------ public pages
@bp.route("/")
def index():
    cfg = current_app.config
    page = max(1, request.args.get("page", 1, type=int))
    items, total, f = _list(cfg["PAGE_SIZE"], (page - 1) * cfg["PAGE_SIZE"])
    chart = [dict(r) for r in store.counts_by_type(db(), to_iso(now()))]
    return render_template("index.html", counts=store.status_counts(db(), to_iso(now())), items=items, total=total, f=f, page=page, pages=max(1, -(-total // cfg["PAGE_SIZE"])),
                           sources=sources_with_health(), chart=chart, version=store.data_version(db()),
                           all_sources=[dict(r) for r in db().execute("SELECT source_id,name FROM sources WHERE enabled=1")])


@bp.route("/notice/<nid>")
def notice(nid):
    cfg, n = current_app.config, now()
    row = db().execute("SELECT * FROM notices WHERE notice_id=?", (nid,)).fetchone()
    if row is None:
        abort(404)
    src = db().execute("SELECT * FROM sources WHERE source_id=?", (row["source_id"],)).fetchone()
    routes = store.routes_for(db(), [nid])[nid]
    item = _serialize(row, routes, {row["source_id"]: src}, n, cfg)
    revs = db().execute("SELECT * FROM notice_revisions WHERE notice_id=? ORDER BY revision_id DESC", (nid,)).fetchall()
    return render_template("notice.html", n=item, stations=store.stations_for(db(), nid), revs=revs, src=src,
                           health=freshness.source_health(src, n, cfg["STALE_AFTER_MINUTES"]))


@bp.route("/sources")
def sources_page():
    logs = db().execute("SELECT * FROM refresh_log ORDER BY id DESC LIMIT 15").fetchall()
    cfgs = {x["source_id"]: x for x in load_sources(current_app.config["SOURCES_FILE"], current_app.config)}
    pending = [{"row": r, "note": cfgs.get(r["source_id"], {}).get("approval", "")} for r in db().execute("SELECT * FROM sources WHERE enabled=0 ORDER BY name")]
    docs = db().execute("SELECT source_id, COUNT(*) c FROM processed_documents GROUP BY source_id").fetchall()
    problems = []
    for l in db().execute("SELECT * FROM refresh_log WHERE detail IS NOT NULL ORDER BY id DESC LIMIT 10"):
        try:
            for f in (json.loads(l["detail"]).get("documents") or {}).get("failures") or []:
                problems.append({"when": l["finished_at"], "source": l["source_id"], **f})
        except ValueError:
            pass
    return render_template("sources.html", problems=problems[:10], sources=sources_with_health(), logs=logs, pending=pending, docs={r["source_id"]: r["c"] for r in docs}, interval=current_app.config["REFRESH_INTERVAL_MINUTES"])


@bp.route("/about")
def about():
    return render_template("about.html")


# ------------------------------------------------------------------------------------------ API
@bp.route("/api/notices")
def api_notices():
    limit = min(max(request.args.get("limit", 50, type=int), 1), 200)
    items, total, f = _list(limit, max(request.args.get("offset", 0, type=int), 0))
    return jsonify({"generated_at": to_iso(now()), "total": total, "filters": f, "notices": items})


@bp.route("/api/notices/<nid>")
def api_notice(nid):
    row = db().execute("SELECT * FROM notices WHERE notice_id=?", (nid,)).fetchone()
    if row is None:
        abort(404)
    srcs = {s["source_id"]: s for s in db().execute("SELECT * FROM sources")}
    data = _serialize(row, store.routes_for(db(), [nid])[nid], srcs, now(), current_app.config)
    data["revisions"] = [dict(r) for r in db().execute(
        "SELECT revision_id, content_hash, change_summary, observed_at FROM notice_revisions WHERE notice_id=? ORDER BY revision_id", (nid,))]
    return jsonify(data)


@bp.route("/api/version")
def api_version():
    return jsonify({"version": store.data_version(db())})


@bp.route("/api/sources")
def api_sources():
    return jsonify([{"source_id": s["row"]["source_id"], "name": s["row"]["name"], "agency": s["row"]["agency"],
                     "url": s["row"]["url"], "last_attempt_at": s["row"]["last_attempt_at"],
                     "last_success_at": s["row"]["last_success_at"], "health": s["health"]} for s in sources_with_health()])


@bp.route("/health")
def health():
    try:
        db().execute("SELECT 1").fetchone()
        return jsonify({"status": "ok", "time": to_iso(now())})
    except Exception as e:          # pragma: no cover
        return jsonify({"status": "error", "detail": str(e)}), 500


# ------------------------------------------------------------------------------------------ subscriptions
@bp.route("/subscribe", methods=["GET", "POST"])
def subscribe():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        routes = parse_routes(request.form.get("routes", ""))
        areas = [a.strip() for a in request.form.get("areas", "").split(",") if a.strip()][:5]
        types = [t for t in request.form.getlist("types") if t in NOTICE_TYPES]
        if not EMAIL_RE.match(email):
            flash("Please enter a valid e-mail address.", "error")
            return render_template("subscribe.html"), 400
        filt = json.dumps({"routes": routes, "areas": areas, "types": types}, sort_keys=True)
        token = secrets.token_urlsafe(24)
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO subscribers(contact,subscriber_filter,unsubscribe_token,created_at) VALUES (?,?,?,?)",
                         (email, filt, token, to_iso(now())))
            row = conn.execute("SELECT * FROM subscribers WHERE contact=? AND subscriber_filter=?", (email, filt)).fetchone()
            conn.execute("UPDATE subscribers SET active=1 WHERE subscriber_id=?", (row["subscriber_id"],))
        flash("Subscribed. You will get one alert for each new or revised advisory that matches your filter.", "ok")
        return redirect(url_for("web.unsubscribe", token=row["unsubscribe_token"], created=1))
    return render_template("subscribe.html")


@bp.route("/unsubscribe/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    row = db().execute("SELECT * FROM subscribers WHERE unsubscribe_token=?", (token,)).fetchone()
    if row is None:
        abort(404)
    if request.method == "POST":
        with db() as conn:
            conn.execute("UPDATE subscribers SET active=0 WHERE subscriber_id=?", (row["subscriber_id"],))
        flash("You have been unsubscribed.", "ok")
        return redirect(url_for("web.index"))
    return render_template("unsubscribe.html", sub=row, created=request.args.get("created"))


# ------------------------------------------------------------------------------------------ admin
@bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("token", ""), current_app.config["ADMIN_TOKEN"]):
            session["is_admin"] = True
            return redirect(request.args.get("next") if (request.args.get("next") or "").startswith("/") else url_for("web.admin"))
        flash("Wrong admin token.", "error")
        return render_template("admin_login.html"), 403
    return render_template("admin_login.html")


@bp.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("web.index"))


@bp.route("/admin")
@admin_required
def admin():
    cfg = current_app.config
    alerts = db().execute(
        """SELECT a.*, s.contact, n.title FROM alert_log a JOIN subscribers s USING(subscriber_id)
           JOIN notices n USING(notice_id) ORDER BY alert_id DESC LIMIT 20""").fetchall()
    subs = db().execute("SELECT COUNT(*) FROM subscribers WHERE active=1").fetchone()[0]
    return render_template("admin.html", sources=sources_with_health(), alerts=alerts, subs=subs,
                           scenarios=demo_source.SCENARIOS if cfg["DEMO_MODE"] else (),
                           scenario=demo_source.get_scenario(cfg["DATA_DIR"]),
                           logs=db().execute("SELECT * FROM refresh_log ORDER BY id DESC LIMIT 10").fetchall(),
                           generated=cfg.get("ADMIN_TOKEN_GENERATED"))


@bp.route("/admin/refresh", methods=["POST"])
@admin_required
def admin_refresh():
    results = current_app.extensions["refresher"].refresh_all(only=request.form.get("source") or None)
    if request.headers.get("X-Admin-Token") or request.accept_mimetypes.best == "application/json":
        return jsonify(results)
    for r in results:
        if r.get("skipped"):
            flash(r["skipped"], "error")
        elif r.get("outcome") == "ok":
            flash(f"{r['source_id']}: {r['new']} new, {r['revised']} revised, {r['withdrawn']} withdrawn, {r['rejected']} rejected", "ok")
        else:
            flash(f"{r['source_id']}: {r['outcome']} - {r['error'] or ''}", "error")
    return redirect(url_for("web.admin"))


@bp.route("/admin/import", methods=["POST"])
@admin_required
def admin_import():
    """Import a public press-release PDF/TXT that a person downloaded from the official website in a browser."""
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a PDF (or .txt) file first.", "error")
        return redirect(url_for("web.admin"))
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    data = f.read(5_000_001)
    if ext not in ("pdf", "txt") or len(data) > 5_000_000 or (ext == "pdf" and not data.startswith(b"%PDF")):
        flash("Only PDF (real PDF files) or .txt press releases up to 5 MB can be imported.", "error")
        return redirect(url_for("web.admin"))
    name = re.sub(r"[^A-Za-z0-9._-]", "_", f.filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])[:80]
    inbox = Path(current_app.config["DATA_DIR"]) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / name).write_bytes(data)
    r = current_app.extensions["refresher"].refresh_all(only="sr-manual-import")
    res = r[0] if r else {}
    if res.get("outcome") == "ok":
        flash(f"Imported {name}: {res['new']} new notice(s), {res['revised']} revised, {res['withdrawn']} withdrawn. "
              + ("It was not a train-service notice, so nothing was added." if not (res['new'] or res['revised'] or res['withdrawn']) else ""), "ok")
    else:
        flash(f"Import problem: {res.get('error') or res.get('skipped') or 'unknown'}", "error")
    return redirect(url_for("web.admin"))


@bp.route("/admin/import-text", methods=["POST"])
@admin_required
def admin_import_text():
    """Import a notice by pasting the text of an official page/PDF that a person opened in a browser."""
    text = (request.form.get("text") or "").strip()
    url = (request.form.get("url") or "").strip()
    if len(text) < 80 or len(text) > 60_000:
        flash("Paste the full text of the notice (at least a few lines, at most 60,000 characters).", "error")
        return redirect(url_for("web.admin"))
    if url and not re.match(r"^https?://\S+$", url):
        flash("The page address must start with http:// or https:// (or leave it empty).", "error")
        return redirect(url_for("web.admin"))
    stamp = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    inbox = Path(current_app.config["DATA_DIR"]) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"pasted-{stamp}.txt").write_text((f"SOURCE-URL: {url}\n" if url else "") + text, encoding="utf-8")
    r = current_app.extensions["refresher"].refresh_all(only="sr-manual-import")
    res = r[0] if r else {}
    if res.get("outcome") == "ok":
        n = res["new"] + res["revised"] + res["withdrawn"]
        flash(f"Imported: {res['new']} new, {res['revised']} revised, {res['withdrawn']} withdrawn." if n else
              "Nothing was added: the text was already imported, or it is not a train-service notice (needs a notice number/date header, routes or train numbers and dates).", "ok" if n else "error")
    else:
        flash(f"Import problem: {res.get('error') or res.get('skipped') or 'unknown'}", "error")
    return redirect(url_for("web.admin"))


@bp.route("/admin/demo-scenario", methods=["POST"])
@admin_required
def admin_demo_scenario():
    if not current_app.config["DEMO_MODE"]:
        abort(404)
    try:
        demo_source.set_scenario(current_app.config["DATA_DIR"], request.form.get("scenario", ""))
    except ValueError:
        abort(400)
    flash(f"Demo source switched to scenario '{request.form['scenario']}'. Press Refresh to ingest it.", "ok")
    return redirect(url_for("web.admin"))
