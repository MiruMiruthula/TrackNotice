"""HTML adapter: finds repeated 'advisory card' elements and reads fields by CSS class names.
The class names are configurable per source in sources.json, so a real agency page can be supported
by editing configuration instead of code. Uses only the standard library (html.parser) and is tolerant of
unclosed tags."""
import re
from html.parser import HTMLParser

from .base import ParseError

DEFAULT_CFG = {
    "item_class": "advisory",
    "ref_attr": "data-ref",
    "fields": {
        "title": "adv-title", "routes": "adv-routes", "area": "adv-area", "type": "adv-type",
        "valid_from": "adv-from", "valid_to": "adv-to", "published": "adv-published",
        "summary": "adv-text", "stations": "adv-stations", "status": "adv-status", "link": "adv-link",
    },
}
VOID = {"br", "img", "hr", "input", "meta", "link", "wbr", "col", "area", "base", "source"}


class _CardParser(HTMLParser):
    def __init__(self, cfg):
        super().__init__(convert_charrefs=True)
        self.cfg = cfg
        self.field_by_class = {cls: name for name, cls in cfg["fields"].items()}
        self.items, self.stack, self.cur = [], [], None

    def _flush(self):
        if self.cur is not None:
            raw = {"ref": self.cur["ref"], "link": self.cur.get("link")}
            for name in self.cfg["fields"]:
                if name != "link":
                    raw[name] = re.sub(r"\s+", " ", " ".join(self.cur["text"].get(name, []))).strip()
            self.items.append(raw)
            self.cur = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        if tag in VOID:
            if tag == "br" and self.cur is not None:
                self.handle_data(" ")
            return
        is_item = self.cfg["item_class"] in classes
        if is_item:
            self._flush()
            self.cur = {"ref": a.get(self.cfg["ref_attr"]), "text": {}}
        field = None
        if self.cur is not None and not is_item:
            for cls in classes:
                if cls in self.field_by_class:
                    field = self.field_by_class[cls]
                    break
            if field == "link" and a.get("href"):
                self.cur["link"] = a["href"]
            if field and field != "link":
                # Fields never nest. If a previous field element was left unclosed (sloppy markup), close it now
                # so its text does not leak into the next field.
                for i, entry in enumerate(self.stack):
                    if entry["field"] and entry["field"] != "link":
                        del self.stack[i:]
                        break
        self.stack.append({"tag": tag, "item": is_item, "field": field})

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                popped = self.stack[i:]
                del self.stack[i:]
                if any(p["item"] for p in popped):
                    self._flush()
                return
        # stray end tag: ignore

    def handle_data(self, data):
        if self.cur is None:
            return
        for entry in self.stack:
            if entry["field"]:
                self.cur["text"].setdefault(entry["field"], []).append(data)

    def close(self):
        super().close()
        self._flush()


class HtmlCardsAdapter:
    def __init__(self, parser_cfg=None):
        cfg = {**DEFAULT_CFG, **(parser_cfg or {})}
        cfg["fields"] = {**DEFAULT_CFG["fields"], **(parser_cfg or {}).get("fields", {})}
        self.cfg = cfg

    def parse(self, body, source=None):
        if not body or "<" not in body:
            raise ParseError("response is not HTML")
        p = _CardParser(self.cfg)
        try:
            p.feed(body)
            p.close()
        except Exception as e:  # html.parser is lenient, but never let a parser bug kill the refresh loop
            raise ParseError(f"HTML parsing failed: {e}")
        return p.items
