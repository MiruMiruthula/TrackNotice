"""One refresh cycle per source: fetch -> parse -> normalise -> upsert -> detect changes -> alert.
Failure policy: if a source cannot be refreshed, OLD DATA IS KEPT BUT NEVER RELABELLED AS LIVE."""
import json
import logging
import threading
import time

from . import alerts, store
from .adapters import get_adapter
from .adapters.base import FetchError, FetchResult, ParseError
from .db import connect
from .normalize import NoticeError, normalize, notice_id_for
from .sources import load_sources
from .timeutil import to_iso, utcnow

log = logging.getLogger("transitnotice.refresh")


class Refresher:
    def __init__(self, cfg, fetcher, sender=None, now_fn=utcnow):
        self.cfg, self.fetcher, self.now_fn = cfg, fetcher, now_fn
        self.sender = sender or alerts.make_sender(cfg)
        self._lock = threading.Lock()

    def refresh_all(self, only=None):
        if not self._lock.acquire(blocking=False):
            return [{"skipped": "a refresh is already running"}]
        try:
            results = []
            for src in load_sources(self.cfg["SOURCES_FILE"], self.cfg):
                if only and src["source_id"] != only:
                    continue
                if not src.get("enabled", True):
                    continue
                results.append(self.refresh_source(src))
            return results
        finally:
            self._lock.release()

    # ------------------------------------------------------------------------------------------
    def refresh_source(self, src):
        conn = connect(self.cfg["DATABASE_PATH"])
        try:
            return self._refresh(conn, src)
        finally:
            conn.close()

    def _log(self, conn, sid, started, t0, outcome, s, error=None, detail=None):
        now = self.now_fn()
        with conn:
            conn.execute(
                """INSERT INTO refresh_log(source_id,started_at,finished_at,duration_ms,outcome,found,new_count,revised,
                   withdrawn,rejected,duplicates,error,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, to_iso(started), to_iso(now), int((time.monotonic() - t0) * 1000), outcome, s["found"], s["new"],
                 s["revised"], s["withdrawn"], s["rejected"], s["duplicates"], error,
                 json.dumps(detail, ensure_ascii=False)[:4000] if detail else None))

    def _fail(self, conn, src, started, t0, s, outcome, error):
        now_iso = to_iso(self.now_fn())
        sid = src["source_id"]
        log.error("refresh %s FAILED: %s", sid, error)
        with conn:
            conn.execute("UPDATE sources SET last_attempt_at=?, last_error=?, consecutive_failures=consecutive_failures+1 "
                         "WHERE source_id=?", (now_iso, error[:300], sid))
            store.refresh_statuses(conn, now_iso)       # expiry follows the clock, independent of the source
        s.update(outcome=outcome, error=error)
        self._log(conn, sid, started, t0, outcome, s, error)
        return s

    def _refresh(self, conn, src):
        sid, tz = src["source_id"], src.get("timezone", "Asia/Kolkata")
        started, t0 = self.now_fn(), time.monotonic()
        s = dict(source_id=sid, outcome=None, found=0, new=0, revised=0, unchanged=0, withdrawn=0,
                 rejected=0, duplicates=0, error=None)
        srow = conn.execute("SELECT * FROM sources WHERE source_id=?", (sid,)).fetchone()
        if srow is None:
            raise RuntimeError(f"source {sid} not in database; run initdb")
        src = {**src, "_data_dir": self.cfg["DATA_DIR"]}
        if src.get("listing") is False:          # pdf_urls / inbox only: there is no listing page to download
            res = FetchResult("ok", "", None, None, 200, 0, 0, b"")
        else:
            try:
                res = self.fetcher.get(src["url"], etag=srow["etag"], last_modified=srow["last_modified"],
                                       min_delay=src.get("min_delay_seconds", 5))
            except FetchError as e:
                return self._fail(conn, src, started, t0, s, "error", f"{e.kind}: {e}")

        now_iso = to_iso(self.now_fn())
        if res.status == "not_modified":
            with conn:
                conn.execute("UPDATE notices SET last_confirmed_at=? WHERE source_id=? AND status!='withdrawn'", (now_iso, sid))
                conn.execute("UPDATE sources SET last_attempt_at=?, last_success_at=?, last_error=NULL, consecutive_failures=0 "
                             "WHERE source_id=?", (now_iso, now_iso, sid))
                store.refresh_statuses(conn, now_iso)
            s["outcome"] = "not_modified"
            self._log(conn, sid, started, t0, "not_modified", s)
            return s

        adapter = get_adapter(src)
        try:
            if getattr(adapter, "document_source", False):
                known = {r[0] for r in conn.execute("SELECT url FROM processed_documents WHERE source_id=?", (sid,))}
                raw_items = adapter.parse(res.body, src, known=known, min_delay=None,
                                          fetch=lambda u: self.fetcher.get(u, min_delay=src.get("min_delay_seconds", 5), max_bytes=6_000_000))
            else:
                raw_items = adapter.parse(res.body, src)
        except ParseError as e:
            return self._fail(conn, src, started, t0, s, "error", f"parse: {e}")
        stats = getattr(adapter, "stats", {}) or {}
        s["found"] = len(raw_items)
        if stats.get("failed") and stats["failed"] == len([p for p in stats.get("processed", [])]) and not raw_items:
            first = stats["failures"][0]
            return self._fail(conn, src, started, t0, s, "error",
                              f"all {stats['failed']} document download(s) failed - first: {first['error']} ({first['document'][-70:]})")
        prev_live = conn.execute("SELECT COUNT(*) FROM notices WHERE source_id=? AND status IN ('active','scheduled')", (sid,)).fetchone()[0]
        doc_mode = getattr(adapter, "document_source", False)
        if not raw_items and prev_live and (not doc_mode or stats.get("links_found", 0) == 0):
            return self._fail(conn, src, started, t0, s, "suspicious_empty",
                              f"parsed 0 notices but {prev_live} were live before - page layout changed? nothing withdrawn")

        notices, seen, rejects, withdrawals = {}, set(), [], []
        for raw in raw_items:
            if raw.get("skip_notice"):
                withdrawals.append(raw)
                continue
            try:
                n = normalize(raw, src, page_url=src["url"])
            except NoticeError as e:
                s["rejected"] += 1
                rejects.append({"ref": e.ref, "reason": e.reason})
                log.warning("%s: rejected item %s: %s", sid, e.ref, e.reason)
                if e.ref:
                    seen.add(notice_id_for(sid, e.ref))   # present but unparseable: must NOT be withdrawn
                continue
            seen.add(n["notice_id"])
            if n["notice_id"] in notices:
                s["duplicates"] += 1
                if notices[n["notice_id"]]["content_hash"] != n["content_hash"]:
                    log.warning("%s: duplicate %s with differing content; keeping first", sid, n["notice_id"])
                continue
            notices[n["notice_id"]] = n

        events = []
        with conn:
            for n in notices.values():
                o = store.upsert_notice(conn, sid, n, now_iso, tz)
                s[{"new": "new", "revised": "revised", "unchanged": "unchanged"}[o.kind]] += 1
                if o.kind != "unchanged":
                    events.append(o)
            gone = store.withdraw_missing(conn, sid, seen, now_iso) if src.get("withdraw_missing", True) else []
            for w in withdrawals:
                o = store.withdraw_by_ref(conn, sid, w["withdraws"], now_iso, f"Withdrawn by press release {w['ref']}")
                if o:
                    gone.append(o)
            s["withdrawn"] = len(gone)
            events += gone
            for (url, ref, outcome, _err) in stats.get("processed", []):
                if outcome != "failed":
                    conn.execute("INSERT OR REPLACE INTO processed_documents(source_id,url,ref,outcome,processed_at) VALUES (?,?,?,?,?)",
                                 (sid, url, ref, outcome, now_iso))
            listed = stats.get("listed_urls") or []
            if listed:     # a document still linked on the official listing is re-confirmed on every successful check
                marks = ",".join("?" * len(listed))
                conn.execute(f"UPDATE notices SET last_confirmed_at=? WHERE source_id=? AND source_url IN ({marks})", [now_iso, sid, *listed])
            store.refresh_statuses(conn, now_iso)
            for o in events:
                alerts.queue_alerts(conn, o, now_iso)
            conn.execute("UPDATE sources SET etag=?, last_modified=?, last_attempt_at=?, last_success_at=?, last_error=NULL, "
                         "consecutive_failures=0 WHERE source_id=?", (res.etag, res.last_modified, now_iso, now_iso, sid))
        alerts.deliver_pending(conn, self.sender, self.cfg, now_iso)
        s["outcome"] = "ok"
        self._log(conn, sid, started, t0, "ok", s, detail=({"rejected": rejects[:20], "attempts": res.attempts} if rejects else {"attempts": res.attempts}) | ({"documents": {"listed": stats.get("links_found"), "failed": stats.get("failed"), "failures": stats.get("failures"), "new_processed": len(stats.get("processed", []))}} if stats else {}))
        log.info("refresh %s ok: %s", sid, {k: v for k, v in s.items() if isinstance(v, int)})
        return s


class Scheduler(threading.Thread):
    """Background thread that refreshes every REFRESH_INTERVAL_MINUTES. Manual refresh uses the same code path."""

    def __init__(self, refresher, interval_minutes, first_delay=3):
        super().__init__(daemon=True, name="transitnotice-scheduler")
        self.refresher, self.interval, self.first_delay = refresher, interval_minutes * 60, first_delay
        self.stop_event = threading.Event()

    def run(self):
        if self.stop_event.wait(self.first_delay):
            return
        while not self.stop_event.is_set():
            try:
                self.refresher.refresh_all()
            except Exception:                    # never let the loop die
                log.exception("scheduled refresh crashed")
            if self.stop_event.wait(self.interval):
                break
