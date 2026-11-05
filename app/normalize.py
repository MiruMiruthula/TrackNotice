"""Normalisation: raw scraped strings -> validated notice dict (UTC times, canonical type, route list, hash)."""
import hashlib
import json
import re
from urllib.parse import urljoin, urlparse

from .timeutil import parse_local, to_iso

NOTICE_TYPES = ("DIVERSION", "SERVICE_DISRUPTION", "PLANNED_MAINTENANCE",
                "TIMETABLE_CHANGE", "STOP_CLOSURE", "CANCELLATION", "OTHER")

# Ordered: the FIRST matching rule wins (documented in the report, section 6.3).
TYPE_RULES = [
    ("DIVERSION", r"divert|diversion|re-?rout|detour|alternat(e|ive) route"),
    ("STOP_CLOSURE", r"stop (is )?clos|station clos|closure of|bus stop|relocat|entry.?/?.?exit"),
    ("PLANNED_MAINTENANCE", r"maintenance|engineering|track work|construction|repair|upgrad|\bworks\b"),
    ("TIMETABLE_CHANGE", r"timetable|timing|schedule|frequency|headway|first (train|bus)|last (train|bus)|revised"),
    ("CANCELLATION", r"cancel|suspen|curtail"),
    ("SERVICE_DISRUPTION", r"outage|disrupt|interruption|breakdown|delay|signal|flood|waterlog|power failure"),
]
WITHDRAWN_WORDS = {"withdrawn", "cancelled", "canceled", "rescinded", "revoked", "closed notice", "withdrawn notice"}
ROUTE_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,24}$")


class NoticeError(Exception):
    def __init__(self, reason, ref=None):
        super().__init__(reason)
        self.reason = reason
        self.ref = ref


def collapse(text, limit=None):
    s = re.sub(r"<[^>]+>", " ", text or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] if limit else s


def classify_type(type_text, title="", summary=""):
    for text in (type_text, title, summary):
        t = (text or "").lower()
        if not t:
            continue
        for name, pattern in TYPE_RULES:
            if re.search(pattern, t):
                return name
    return "OTHER"


def parse_routes(text):
    if not text:
        return []
    s = re.sub(r"(?i)\b(routes?|lines?\s+no\.?|no\.)\s*[:\-]?\s*", "", text)
    out = []
    for tok in re.split(r"[,;/&|]|\band\b", s, flags=re.I):
        tok = re.sub(r"\s+", "-", tok.strip().upper())
        if tok in ("ALL-ROUTES", "ALL"):
            tok = "ALL"
        if tok and ROUTE_RE.match(tok) and tok not in out:
            out.append(tok)
    return sorted(out)


def parse_stations(text):
    if not text:
        return []
    out = []
    for tok in re.split(r"[,;|]", text):
        tok = collapse(tok, 80)
        if tok and tok not in out:
            out.append(tok)
    return out[:25]


def notice_id_for(source_id, ref):
    return "TN-" + hashlib.sha256(f"{source_id}|{ref}".encode()).hexdigest()[:14]


def compute_hash(n):
    """Hash of the *meaningful* content only. published_at / page chrome are excluded so cosmetic
    re-stamping does not create a new revision."""
    canon = {
        "routes": sorted(n["routes"]), "area": n["area"] or "", "type": n["type"], "title": n["title"],
        "summary": n["summary"], "valid_from": n["valid_from"], "valid_to": n["valid_to"],
        "stations": sorted(n["stations"]), "withdrawn": bool(n["withdrawn"]),
    }
    return hashlib.sha256(json.dumps(canon, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _safe_url(link, base):
    if not link:
        return base
    url = urljoin(base, link.strip())
    return url if urlparse(url).scheme in ("http", "https") else base


def normalize(raw, source, page_url=None):
    """raw: dict from an adapter. source: dict from sources.json. Raises NoticeError with a clear reason."""
    tz = source.get("timezone", "Asia/Kolkata")
    ref = collapse(raw.get("ref") or "", 120) or None
    title = collapse(raw.get("title"), 200)
    summary = collapse(raw.get("summary"), 2000)
    if not title and not summary:
        raise NoticeError("no title or text", ref)
    title = title or summary[:120]
    summary = summary or title

    routes = parse_routes(raw.get("routes"))
    if not routes:
        raise NoticeError("no valid route code", ref)

    notes = []
    published = None
    if raw.get("published"):
        try:
            published = parse_local(raw["published"], tz)
        except ValueError:
            notes.append("published time unreadable")
    try:
        vf = parse_local(raw["valid_from"], tz) if raw.get("valid_from") else None
    except ValueError as e:
        raise NoticeError(f"bad start time: {e}", ref)
    if vf is None:
        if published is None:
            raise NoticeError("no start time and no published time", ref)
        vf = published
        notes.append("start inferred from publication time")
    vt = None
    if raw.get("valid_to") and collapse(raw["valid_to"]).lower() not in ("", "tbd", "tba", "until further notice", "n/a", "-"):
        try:
            vt = parse_local(raw["valid_to"], tz, end_of_day=True)
        except ValueError as e:
            raise NoticeError(f"bad end time: {e}", ref)
    if vt is not None and vt < vf:
        raise NoticeError("end time is before start time", ref)

    status_text = collapse(raw.get("status")).lower()
    withdrawn = status_text in WITHDRAWN_WORDS
    n = {
        "external_ref": ref, "agency": source["agency"], "routes": routes,
        "area": collapse(raw.get("area"), 120) or None,
        "source_url": _safe_url(raw.get("link"), page_url or source["url"]),
        "type": classify_type(collapse(raw.get("type")), title, summary),
        "title": title, "summary": summary,
        "published_at": to_iso(published) if published else None,
        "valid_from": to_iso(vf), "valid_to": to_iso(vt) if vt else None,
        "stations": parse_stations(raw.get("stations")), "withdrawn": withdrawn, "notes": notes,
    }
    if ref:
        n["notice_id"] = notice_id_for(source["source_id"], ref)
    else:  # no stable reference published: derive identity from fields that do not change on extension
        key = f"{title.lower()}|{','.join(routes)}|{n['valid_from']}"
        n["notice_id"] = "TN-" + hashlib.sha256(f"{source['source_id']}|{key}".encode()).hexdigest()[:14]
    n["content_hash"] = compute_hash(n)
    return n
