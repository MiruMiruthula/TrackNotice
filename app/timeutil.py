"""Time handling. Rule: everything is STORED in UTC (ISO-8601, 'Z'), source text is interpreted in the
source's own time zone (default Asia/Kolkata) and everything is DISPLAYED in DISPLAY_TZ with the zone name."""
import re
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def get_tz(name):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    if name in ("Asia/Kolkata", "Asia/Calcutta", "IST"):
        return IST
    if name in ("UTC", "Etc/UTC"):
        return timezone.utc
    raise ValueError(f"unknown time zone {name!r} (pip install tzdata)")


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt):
    return dt.astimezone(timezone.utc).strftime(ISO_FMT)


def from_iso(s):
    return datetime.strptime(s, ISO_FMT).replace(tzinfo=timezone.utc)


_DT_FORMATS = [
    "%d %b %Y %I:%M %p", "%d %B %Y %I:%M %p", "%d %b %Y %I %p", "%d %B %Y %I %p",
    "%d %b %Y %H:%M", "%d %B %Y %H:%M",
    "%d/%m/%Y %H:%M", "%d/%m/%Y %I:%M %p", "%d-%m-%Y %H:%M", "%d-%m-%Y %I:%M %p",
    "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
]
_D_FORMATS = ["%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d"]
_WEEKDAY = re.compile(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?\s+", re.I)


def _clean(text):
    s = str(text).replace("\u00a0", " ").strip()
    s = s.replace(",", " ").replace("T", " ") if re.match(r"^\d{4}-\d{2}-\d{2}T", s) else s.replace(",", " ")
    s = re.sub(r"\b([ap])\.m\.?", lambda m: m.group(1).upper() + "M", s, flags=re.I)
    s = re.sub(r"\b(hrs?|hours?|IST)\b\.?", "", s, flags=re.I)
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    s = re.sub(r"(\d)\s*(am|pm)\b", lambda m: f"{m.group(1)} {m.group(2).upper()}", s, flags=re.I)
    s = re.sub(r"\bSept\b", "Sep", s)
    s = _WEEKDAY.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_local(text, tzname="Asia/Kolkata", end_of_day=False):
    """Parse human/ISO text from a source into an aware UTC datetime. Date-only values become
    00:00:00 (start) or 23:59:59 (end_of_day=True) in the SOURCE time zone. Raises ValueError."""
    if text is None or not str(text).strip():
        raise ValueError("empty date")
    s = _clean(text)
    tz = get_tz(tzname)
    off = re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", s.replace("Z", "+0000"))
    for fmt in ("%Y-%m-%d %H:%M%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return datetime.strptime(off, fmt).astimezone(timezone.utc)
        except ValueError:
            pass
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            pass
    for fmt in _D_FORMATS:
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        d = d.replace(hour=23, minute=59, second=59) if end_of_day else d
        return d.replace(tzinfo=tz).astimezone(timezone.utc)
    raise ValueError(f"unrecognised date/time: {text!r}")


def fmt_local(iso, tzname="Asia/Kolkata"):
    if not iso:
        return ""
    dt = from_iso(iso).astimezone(get_tz(tzname))
    return dt.strftime("%d %b %Y, %I:%M %p ") + (dt.tzname() or tzname)


def fmt_delta(delta):
    secs = int(abs(delta.total_seconds()))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d:
        parts.append(f"{d} d")
    if h:
        parts.append(f"{h} h")
    if m and not d:
        parts.append(f"{m} min")
    return " ".join(parts) or "0 min"


def humanize_age(iso, now):
    if not iso:
        return "never"
    secs = int((now - from_iso(iso)).total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"
