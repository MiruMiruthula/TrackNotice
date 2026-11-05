"""Persistence + change detection. All writes are idempotent: running the same refresh twice never creates
duplicates, and only a change in meaningful content creates a new revision."""
import json
from dataclasses import dataclass

from .normalize import compute_hash
from .timeutil import fmt_delta, fmt_local, from_iso, parse_local, to_iso


@dataclass
class Outcome:
    kind: str            # new | revised | unchanged
    notice_id: str
    revision_id: int
    change_summary: str
    status: str


def compute_status(valid_from, valid_to, withdrawn, now_iso):
    if withdrawn:
        return "withdrawn"
    if now_iso < valid_from:
        return "scheduled"
    if valid_to is not None and now_iso > valid_to:
        return "expired"
    return "active"


def effective_status(row, now_iso):
    """Status computed at READ time from the announced window, so an expired advisory is never shown as active
    even if no refresh has run since it ended."""
    return compute_status(row["valid_from"], row["valid_to"], row["status"] == "withdrawn", now_iso)


def _snapshot(n):
    keys = ("routes", "area", "type", "title", "summary", "published_at", "valid_from", "valid_to",
            "stations", "withdrawn", "source_url")
    return json.dumps({k: n[k] for k in keys}, ensure_ascii=False, sort_keys=True)


def row_to_notice(conn, row):
    routes = [r[0] for r in conn.execute("SELECT route_id FROM notice_routes WHERE notice_id=? ORDER BY 1", (row["notice_id"],))]
    stations = [r[0] for r in conn.execute("SELECT station FROM notice_stations WHERE notice_id=? ORDER BY 1", (row["notice_id"],))]
    return {"notice_id": row["notice_id"], "routes": routes, "stations": stations, "area": row["area"],
            "type": row["type"], "title": row["title"], "summary": row["summary"], "published_at": row["published_at"],
            "valid_from": row["valid_from"], "valid_to": row["valid_to"], "withdrawn": row["status"] == "withdrawn",
            "source_url": row["source_url"], "agency": row["agency"], "external_ref": row["external_ref"]}


def describe_change(old, new, tz="Asia/Kolkata"):
    parts = []
    if new["withdrawn"] and not old["withdrawn"]:
        parts.append("Advisory withdrawn / cancelled by the agency")
    elif old["withdrawn"] and not new["withdrawn"]:
        parts.append("Advisory reinstated")
    if old["valid_to"] != new["valid_to"]:
        if old["valid_to"] is None:
            parts.append(f"End time announced: {fmt_local(new['valid_to'], tz)}")
        elif new["valid_to"] is None:
            parts.append("End time removed (now open-ended)")
        else:
            delta = from_iso(new["valid_to"]) - from_iso(old["valid_to"])
            word = "Extended" if delta.total_seconds() > 0 else "Shortened"
            parts.append(f"{word} by {fmt_delta(delta)}: now ends {fmt_local(new['valid_to'], tz)} "
                         f"(was {fmt_local(old['valid_to'], tz)})")
    if old["valid_from"] != new["valid_from"]:
        parts.append(f"Start changed: {fmt_local(old['valid_from'], tz)} -> {fmt_local(new['valid_from'], tz)}")
    if sorted(old["routes"]) != sorted(new["routes"]):
        parts.append(f"Routes changed: {', '.join(old['routes'])} -> {', '.join(new['routes'])}")
    if old["type"] != new["type"]:
        parts.append(f"Type changed: {old['type']} -> {new['type']}")
    if (old["area"] or "") != (new["area"] or ""):
        parts.append("Area changed")
    if sorted(old["stations"]) != sorted(new["stations"]):
        parts.append("Stations changed")
    if old["summary"] != new["summary"] or old["title"] != new["title"]:
        parts.append("Description updated")
    return "; ".join(parts) or "Content changed"


def _set_children(conn, nid, n):
    conn.execute("DELETE FROM notice_routes WHERE notice_id=?", (nid,))
    conn.execute("DELETE FROM notice_stations WHERE notice_id=?", (nid,))
    conn.executemany("INSERT OR IGNORE INTO notice_routes(notice_id,route_id) VALUES (?,?)", [(nid, r) for r in n["routes"]])
    conn.executemany("INSERT OR IGNORE INTO notice_stations(notice_id,station) VALUES (?,?)", [(nid, s) for s in n["stations"]])


def upsert_notice(conn, source_id, n, now_iso, tz="Asia/Kolkata"):
    """Insert a new notice, confirm an unchanged one, or create a new revision. Call inside `with conn:`."""
    nid = n["notice_id"]
    row = conn.execute("SELECT * FROM notices WHERE notice_id=?", (nid,)).fetchone()
    status = compute_status(n["valid_from"], n["valid_to"], n["withdrawn"], now_iso)
    if row is None:
        conn.execute(
            """INSERT INTO notices(notice_id,source_id,external_ref,agency,route_id,area,source_url,type,title,summary,
               published_at,valid_from,valid_to,status,observed_at,last_confirmed_at,content_hash,revision_id,
               change_summary,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'New advisory',?)""",
            (nid, source_id, n["external_ref"], n["agency"], ",".join(n["routes"]), n["area"], n["source_url"],
             n["type"], n["title"], n["summary"], n["published_at"], n["valid_from"], n["valid_to"], status,
             now_iso, now_iso, n["content_hash"], now_iso))
        _set_children(conn, nid, n)
        conn.execute("INSERT INTO notice_revisions VALUES (?,?,?,?,?,?)",
                     (nid, 1, n["content_hash"], _snapshot(n), "New advisory", now_iso))
        return Outcome("new", nid, 1, "New advisory", status)
    if row["content_hash"] == n["content_hash"]:
        conn.execute("UPDATE notices SET last_confirmed_at=?, status=? WHERE notice_id=?", (now_iso, status, nid))
        return Outcome("unchanged", nid, row["revision_id"], row["change_summary"], status)
    old = row_to_notice(conn, row)
    change = describe_change(old, n, tz)
    rev = row["revision_id"] + 1
    conn.execute(
        """UPDATE notices SET agency=?, route_id=?, area=?, source_url=?, type=?, title=?, summary=?, published_at=?,
           valid_from=?, valid_to=?, status=?, last_confirmed_at=?, content_hash=?, revision_id=?, change_summary=?,
           alert_status='none', alerted_at=NULL, updated_at=? WHERE notice_id=?""",
        (n["agency"], ",".join(n["routes"]), n["area"], n["source_url"], n["type"], n["title"], n["summary"],
         n["published_at"], n["valid_from"], n["valid_to"], status, now_iso, n["content_hash"], rev, change, now_iso, nid))
    _set_children(conn, nid, n)
    conn.execute("INSERT INTO notice_revisions VALUES (?,?,?,?,?,?)", (nid, rev, n["content_hash"], _snapshot(n), change, now_iso))
    return Outcome("revised", nid, rev, change, status)


def withdraw_missing(conn, source_id, seen_ids, now_iso):
    """Notices that vanished from a SUCCESSFULLY parsed page are treated as withdrawn - unless they had already
    expired by their own announced end time (those are simply archived as expired)."""
    outcomes = []
    rows = conn.execute("SELECT * FROM notices WHERE source_id=? AND status!='withdrawn'", (source_id,)).fetchall()
    for row in rows:
        if row["notice_id"] in seen_ids:
            continue
        if effective_status(row, now_iso) == "expired":
            continue
        n = row_to_notice(conn, row)
        n["withdrawn"] = True
        h = compute_hash(n)
        change = "No longer listed on the source page - treated as withdrawn"
        rev = row["revision_id"] + 1
        n["content_hash"] = h
        conn.execute("UPDATE notices SET status='withdrawn', content_hash=?, revision_id=?, change_summary=?, "
                     "alert_status='none', alerted_at=NULL, updated_at=? WHERE notice_id=?",
                     (h, rev, change, now_iso, row["notice_id"]))
        conn.execute("INSERT INTO notice_revisions VALUES (?,?,?,?,?,?)",
                     (row["notice_id"], rev, h, _snapshot(n), change, now_iso))
        outcomes.append(Outcome("revised", row["notice_id"], rev, change, "withdrawn"))
    return outcomes


def refresh_statuses(conn, now_iso):
    conn.execute(
        """UPDATE notices SET status = CASE WHEN ? < valid_from THEN 'scheduled'
                WHEN valid_to IS NOT NULL AND ? > valid_to THEN 'expired' ELSE 'active' END
           WHERE status != 'withdrawn'""", (now_iso, now_iso))


# ------------------------------------------------------------------------------------------------ queries
_EFF = ("CASE WHEN n.status='withdrawn' THEN 'withdrawn' WHEN ? < n.valid_from THEN 'scheduled' "
        "WHEN n.valid_to IS NOT NULL AND ? > n.valid_to THEN 'expired' ELSE 'active' END")


def _like(s):
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def query_notices(conn, f, now_iso, display_tz="Asia/Kolkata", limit=25, offset=0):
    where, params = [], []
    status = f.get("status") or "active"
    if status in ("active", "scheduled", "expired", "withdrawn"):
        where.append("eff = ?")
        params.append(status)
    if f.get("q"):
        like = _like(f["q"])
        where.append("(title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR route_id LIKE ? ESCAPE '\\' "
                     "OR area LIKE ? ESCAPE '\\')")
        params += [like] * 4
    if f.get("route"):
        where.append("EXISTS (SELECT 1 FROM notice_routes r WHERE r.notice_id=x.notice_id AND r.route_id IN (?, 'ALL'))")
        params.append(f["route"].strip().upper().replace(" ", "-"))
    if f.get("station"):
        where.append("EXISTS (SELECT 1 FROM notice_stations s WHERE s.notice_id=x.notice_id AND s.station LIKE ? ESCAPE '\\')")
        params.append(_like(f["station"]))
    if f.get("area"):
        where.append("area LIKE ? ESCAPE '\\'")
        params.append(_like(f["area"]))
    if f.get("type"):
        where.append("type = ?")
        params.append(f["type"])
    if f.get("source"):
        where.append("source_id = ?")
        params.append(f["source"])
    if f.get("date"):  # service date, interpreted in the display time zone: window overlaps that calendar day
        try:
            start = parse_local(f["date"], display_tz)
            end = parse_local(f["date"], display_tz, end_of_day=True)
            where.append("valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)")
            params += [to_iso(end), to_iso(start)]
        except ValueError:
            pass
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    cte = f"WITH x AS (SELECT n.*, {_EFF} AS eff FROM notices n)"
    base_params = [now_iso, now_iso]
    total = conn.execute(f"{cte} SELECT COUNT(*) FROM x {sql_where}", base_params + params).fetchone()[0]
    rows = conn.execute(
        f"""{cte} SELECT * FROM x {sql_where}
            ORDER BY CASE eff WHEN 'active' THEN 0 WHEN 'scheduled' THEN 1 WHEN 'expired' THEN 2 ELSE 3 END,
                     updated_at DESC, notice_id LIMIT ? OFFSET ?""",
        base_params + params + [limit, offset]).fetchall()
    return rows, total


def routes_for(conn, notice_ids):
    out = {}
    for nid in notice_ids:
        out[nid] = [r[0] for r in conn.execute("SELECT route_id FROM notice_routes WHERE notice_id=? ORDER BY 1", (nid,))]
    return out


def stations_for(conn, nid):
    return [r[0] for r in conn.execute("SELECT station FROM notice_stations WHERE notice_id=? ORDER BY 1", (nid,))]


def counts_by_type(conn, now_iso):
    return conn.execute(
        f"WITH x AS (SELECT n.*, {_EFF} AS eff FROM notices n) SELECT type, COUNT(*) c FROM x WHERE eff='active' "
        "GROUP BY type ORDER BY c DESC, type", (now_iso, now_iso)).fetchall()


def data_version(conn):
    r = conn.execute("SELECT COUNT(*), COALESCE(MAX(updated_at),''), COALESCE(SUM(revision_id),0) FROM notices").fetchone()
    return f"{r[0]}-{r[1]}-{r[2]}"


def withdraw_by_ref(conn, source_id, ref, now_iso, reason):
    """A later document explicitly cancels an earlier one (e.g. 'line block ... STANDS CANCELLED')."""
    row = conn.execute("SELECT * FROM notices WHERE source_id=? AND external_ref=? AND status!='withdrawn'", (source_id, ref)).fetchone()
    if row is None:
        return None
    n = row_to_notice(conn, row)
    n["withdrawn"] = True
    h = compute_hash(n)
    rev = row["revision_id"] + 1
    n["content_hash"] = h
    conn.execute("UPDATE notices SET status='withdrawn', content_hash=?, revision_id=?, change_summary=?, alert_status='none', "
                 "alerted_at=NULL, updated_at=? WHERE notice_id=?", (h, rev, reason, now_iso, row["notice_id"]))
    conn.execute("INSERT INTO notice_revisions VALUES (?,?,?,?,?,?)", (row["notice_id"], rev, h, _snapshot(n), reason, now_iso))
    return Outcome("revised", row["notice_id"], rev, reason, "withdrawn")


def status_counts(conn, now_iso):
    rows = conn.execute(f"WITH x AS (SELECT n.*, {_EFF} AS eff FROM notices n) SELECT eff, COUNT(*) FROM x GROUP BY eff", (now_iso, now_iso)).fetchall()
    d = {"active": 0, "scheduled": 0, "expired": 0, "withdrawn": 0}
    d.update({r[0]: r[1] for r in rows})
    d["all"] = sum(d.values())
    return d
