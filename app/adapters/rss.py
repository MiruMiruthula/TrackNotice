"""RSS 2.0 adapter for authorities that publish a feed (preferred over HTML scraping when available).
Details such as routes/dates are read from 'Key: value' pairs in the item description."""
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from .base import ParseError
from ..timeutil import to_iso

KEYS = {"route": "routes", "routes": "routes", "area": "area", "from": "valid_from", "valid from": "valid_from",
        "to": "valid_to", "until": "valid_to", "valid to": "valid_to", "station": "stations", "stations": "stations",
        "status": "status", "type": "type"}
PAIR = re.compile(r"^\s*([A-Za-z ]{2,12}?)\s*:\s*(.+?)\s*$")


class RssAdapter:
    def parse(self, body, source=None):
        head = body[:2000].lower()
        if "<!doctype" in body[:5000].lower() or "<!entity" in body.lower():
            raise ParseError("DOCTYPE/ENTITY declarations are refused (XML entity expansion protection)")
        try:
            root = ET.fromstring(body.strip().encode("utf-8"))
        except ET.ParseError as e:
            raise ParseError(f"invalid XML: {e}")
        items = []
        for it in root.iter("item"):
            def t(tag):
                el = it.find(tag)
                return (el.text or "").strip() if el is not None else ""
            desc = re.sub(r"<[^>]+>", " ", t("description"))
            raw = {"ref": t("guid") or None, "title": t("title"), "link": t("link") or None, "type": t("category"),
                   "published": None}
            pub = t("pubDate")
            if pub:
                try:
                    raw["published"] = to_iso(parsedate_to_datetime(pub))
                except (TypeError, ValueError):
                    pass
            free = []
            for piece in re.split(r"[;\n|]", desc):
                m = PAIR.match(piece)
                key = KEYS.get(m.group(1).strip().lower()) if m else None
                if key:
                    raw[key] = m.group(2)
                elif piece.strip():
                    free.append(piece.strip())
            raw["summary"] = " ".join(free) or raw["title"]
            items.append(raw)
        if not items and "<rss" not in head and "<feed" not in head:
            raise ParseError("not an RSS document")
        return items
