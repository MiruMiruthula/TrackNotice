"""Alerting: exactly one alert per (notice, revision, subscriber), guaranteed by a UNIQUE constraint.
Delivery has a safe demo mode (writes to data/alerts_demo.log) and an optional SMTP mode, with a delivery log."""
import json
import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

from . import store
from .timeutil import fmt_local

log = logging.getLogger("transitnotice.alerts")


def mask(contact):
    name, _, dom = contact.partition("@")
    return (name[:1] + "***@" + dom) if dom else "***"


def matches(sub_filter, routes, area, ntype):
    f = json.loads(sub_filter or "{}")
    wanted = {r.strip().upper() for r in f.get("routes", []) if r.strip()}
    if wanted and "ALL" not in routes and not (wanted & set(routes)):
        return False
    if f.get("areas") and not any(a.lower() in (area or "").lower() for a in f["areas"]):
        return False
    if f.get("types") and ntype not in f["types"]:
        return False
    return True


def queue_alerts(conn, outcome, now_iso):
    """Create pending alert rows for matching subscribers. Safe to call repeatedly (INSERT OR IGNORE)."""
    row = conn.execute("SELECT * FROM notices WHERE notice_id=?", (outcome.notice_id,)).fetchone()
    if row is None:
        return 0
    if outcome.status == "expired":      # do not alert about something already over when first seen
        conn.execute("UPDATE notices SET alert_status='none' WHERE notice_id=?", (outcome.notice_id,))
        return 0
    routes = [r[0] for r in conn.execute("SELECT route_id FROM notice_routes WHERE notice_id=?", (outcome.notice_id,))]
    created = 0
    for sub in conn.execute("SELECT * FROM subscribers WHERE active=1").fetchall():
        if matches(sub["subscriber_filter"], routes, row["area"], row["type"]):
            cur = conn.execute("INSERT OR IGNORE INTO alert_log(notice_id,revision_id,subscriber_id,created_at) VALUES (?,?,?,?)",
                               (outcome.notice_id, outcome.revision_id, sub["subscriber_id"], now_iso))
            created += cur.rowcount
    conn.execute("UPDATE notices SET alert_status=? WHERE notice_id=? AND revision_id=?",
                 ("pending" if created else "no_subscribers", outcome.notice_id, outcome.revision_id))
    return created


def build_message(row, routes, tz, kind_hint):
    window = f"{fmt_local(row['valid_from'], tz)} to " + (fmt_local(row["valid_to"], tz) if row["valid_to"] else "end not announced")
    subject = f"[TransitNotice] {row['type'].replace('_', ' ').title()} - routes {', '.join(routes)} (rev {row['revision_id']})"
    body = (f"{row['title']}\n\nRoutes: {', '.join(routes)}\nArea: {row['area'] or '-'}\nValid: {window}\n"
            f"Change: {row['change_summary']}\n\n{row['summary']}\n\nSource: {row['agency']} - {row['source_url']}\n"
            f"Last verified by TransitNotice: {fmt_local(row['last_confirmed_at'], tz)}\n"
            "Always confirm with the transport agency before travelling.\n")
    return subject, body


class DemoSender:
    mode = "demo"

    def __init__(self, path):
        self.path = Path(path)

    def send(self, contact, subject, body):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(f"TO: {contact}\nSUBJECT: {subject}\n{body}\n{'-' * 60}\n")


class SmtpSender:
    mode = "smtp"

    def __init__(self, cfg):
        self.cfg = cfg

    def send(self, contact, subject, body):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.cfg["SMTP_FROM"], contact, subject
        msg.set_content(body)
        with smtplib.SMTP(self.cfg["SMTP_HOST"], self.cfg["SMTP_PORT"], timeout=15) as s:
            s.starttls()
            if self.cfg["SMTP_USER"]:
                s.login(self.cfg["SMTP_USER"], self.cfg["SMTP_PASSWORD"])
            s.send_message(msg)


def make_sender(cfg):
    if cfg["ALERT_MODE"] == "smtp" and cfg["SMTP_HOST"]:
        return SmtpSender(cfg)
    return DemoSender(Path(cfg["DATA_DIR"]) / "alerts_demo.log")


def deliver_pending(conn, sender, cfg, now_iso):
    """Send every pending/failed alert (max ALERT_MAX_ATTEMPTS tries). Returns number delivered."""
    rows = conn.execute(
        """SELECT a.*, s.contact FROM alert_log a JOIN subscribers s USING(subscriber_id)
           WHERE a.alert_status IN ('pending','failed') AND a.attempts < ?""", (cfg["ALERT_MAX_ATTEMPTS"],)).fetchall()
    sent, touched = 0, set()
    for a in rows:
        n = conn.execute("SELECT * FROM notices WHERE notice_id=?", (a["notice_id"],)).fetchone()
        routes = [r[0] for r in conn.execute("SELECT route_id FROM notice_routes WHERE notice_id=?", (a["notice_id"],))]
        # message reflects the revision that triggered the alert
        subject, body = build_message(n, routes, cfg["DISPLAY_TZ"], None)
        try:
            sender.send(a["contact"], subject, body)
            status = "demo_logged" if sender.mode == "demo" else "sent"
            err = None
            sent += 1
        except Exception as e:
            status, err = "failed", f"{type(e).__name__}: {e}"[:300]
            log.warning("alert %s to %s failed: %s", a["alert_id"], mask(a["contact"]), err)
        with conn:
            conn.execute("UPDATE alert_log SET alert_status=?, attempts=attempts+1, alerted_at=?, error=? WHERE alert_id=?",
                         (status, now_iso if err is None else None, err, a["alert_id"]))
        touched.add((a["notice_id"], a["revision_id"]))
    with conn:
        for nid, rev in touched:
            st = [r[0] for r in conn.execute("SELECT alert_status FROM alert_log WHERE notice_id=? AND revision_id=?", (nid, rev))]
            if "pending" in st or "failed" in st:
                agg = "failed" if "failed" in st and "pending" not in st else "pending"
            else:
                agg = "demo_logged" if "demo_logged" in st else "delivered"
            conn.execute("UPDATE notices SET alert_status=?, alerted_at=? WHERE notice_id=? AND revision_id=?",
                         (agg, now_iso if agg in ("delivered", "demo_logged") else None, nid, rev))
    return sent
