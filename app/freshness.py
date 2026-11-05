"""Freshness labelling. A snapshot is only ever called LIVE when the latest refresh attempt succeeded AND is recent."""
from .timeutil import from_iso, humanize_age


def source_health(src, now, stale_after_minutes):
    ok, att, err = src["last_success_at"], src["last_attempt_at"], src["last_error"]
    if not att:
        return {"level": "unknown", "label": "NOT YET CHECKED", "text": "This source has not been checked yet."}
    if err and (not ok or att >= ok):
        since = f"Last successful check: {humanize_age(ok, now)}." if ok else "No successful check yet."
        return {"level": "error", "label": "SOURCE ERROR",
                "text": f"The latest attempt {humanize_age(att, now)} failed ({err}). {since} Data shown is the last "
                        f"verified snapshot, not live."}
    age_min = (now - from_iso(ok)).total_seconds() / 60
    if age_min > stale_after_minutes:
        return {"level": "stale", "label": "STALE",
                "text": f"Last verified {humanize_age(ok, now)} (older than {stale_after_minutes} min). Not live."}
    return {"level": "live", "label": "LIVE", "text": f"Verified {humanize_age(ok, now)}."}


def notice_freshness(src, notice_last_confirmed, now, stale_after_minutes):
    h = source_health(src, now, stale_after_minutes)
    age_min = (now - from_iso(notice_last_confirmed)).total_seconds() / 60
    level = h["level"]
    if level == "live" and age_min > stale_after_minutes:
        level = "stale"      # source is fine but this notice was not seen in the latest pages
    label = {"live": "Live", "stale": "Stale", "error": "Source error", "unknown": "Unchecked"}[level]
    return {"level": level, "label": label, "last_verified": humanize_age(notice_last_confirmed, now)}
