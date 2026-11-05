"""Southern Railway (Chennai Division) press-release adapter.

The division publishes service changes as PDF press releases ("CHANGES IN PATTERN OF TRAIN SERVICES",
"LINE BLOCK CANCELLED ...", "RESTORATION OF SUBURBAN TRAIN SERVICES ...") numbered PUB/MAS/YYYY/MM/NN.
Flow: listing page -> PDF links (plus optional fixed `pdf_urls`) -> download each *new* PDF politely ->
extract text -> parse one notice per release.  Each PDF is processed once (table processed_documents).
"""
import calendar
import io
import re
from pathlib import Path
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse

from .base import FetchError, ParseError

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
REF_RE = re.compile(r"PUB\s*/\s*MAS\s*/\s*(\d{4})\s*/\s*(\d{2})\s*/\s*(\d)\s*(\d?)", re.I)
DATE_HEADER_RE = re.compile(r"Date\s*:?\s*(\d{1,2})\s*[./]\s*(\d{1,2})\s*[./]\s*(\d{4})", re.I)
DAYMONTH_RE = re.compile(r"((?:\d{1,2}(?:st|nd|rd|th)?(?:\s*(?:&|,|and|to|-)\s*)+)*\d{1,2}(?:st|nd|rd|th)?)\s+(" + MONTH_RE + r")\b\.?(?:\s*,?\s*(\d{4}))?", re.I)
NUMDATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
TIME_RANGE_RE = re.compile(r"from\s+(\d{1,2})[:.](\d{2})\s*hrs?\b.{0,40}?\bto\s+(\d{1,2})[:.](\d{2})\s*hrs?", re.I | re.S)
SERVICE_WORDS = re.compile(r"cancel|divert|line block|power block|traffic block|restor|resum|regular pattern|sunday pattern|"
                           r"changes in pattern|frequency|short.?terminat|originat|extension of|revised|regulat|reschedul", re.I)
TRAIN_NO_RE = re.compile(r"Train\s*No\.?\s*:?\s*(\d{5})|(?<![\d./:])(\d{5})(?=\s+[A-Z][a-z])", re.I)
DETAIL_HDR_RE = re.compile(r"(\d{1,4}(?:\s*,\s*\d{1,4})*\s*/\s*20\d\d-\d\d)\s+(\d{2})-(\d{2})-(\d{4})\s+([A-Za-z]+)\s+(.+)", re.S)


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def extract_pdf_text(data):
    """PDF bytes -> text (pypdf). Raises ParseError when the file cannot be read."""
    try:
        from pypdf import PdfReader
    except ImportError as e:                      # pragma: no cover
        raise ParseError("pypdf is not installed (pip install -r requirements.txt)") from e
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((pg.extract_text() or "") for pg in reader.pages)
    except Exception as e:
        raise ParseError(f"PDF could not be read: {e}")


def _expand_dates(text, default_year):
    """All calendar dates mentioned: '28th & 29th October 2024', '20th to 23rd November 2024', '5th June to 4th August 2025', 05.06.2025"""
    found = []
    matches = list(DAYMONTH_RE.finditer(text))
    for i, m in enumerate(matches):
        year = int(m.group(3)) if m.group(3) else None
        if year is None:                           # '05th June to 04th August 2025' -> take the next explicit year
            nxt = next((int(n.group(3)) for n in matches[i + 1:i + 3] if n.group(3)), None)
            year = nxt or default_year
        mon = MONTHS[m.group(2).lower()]
        for d in re.findall(r"\d{1,2}", m.group(1)):
            try:
                found.append(date(year, mon, int(d)))
            except ValueError:
                pass
    for m in NUMDATE_RE.finditer(text):
        try:
            found.append(date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
        except ValueError:
            pass
    return sorted(set(found))


def parse_press_release(text, url="", fallback_ref=None):
    """Return a raw-notice dict, a withdrawal marker, or None (not a service notice)."""
    text = text.replace("\u00ad", "").replace("^", "")                      # soft hyphens / superscript markers from the website
    flat = re.sub(r"[ \t]+", " ", text.replace("\r", ""))
    m = REF_RE.search(flat)
    ref = f"PUB/MAS/{m.group(1)}/{m.group(2)}/{(m.group(3) + m.group(4)).zfill(2)}" if m else None
    hd = DATE_HEADER_RE.search(flat)
    published = f"{int(hd.group(3)):04d}-{int(hd.group(2)):02d}-{int(hd.group(1)):02d} 00:00" if hd else None
    pub_year = int(hd.group(3)) if hd else (int(m.group(1)) if m else date.today().year)
    title = ""
    pm = re.search(r"PRESS\s+RELEASE\s*\n+(.*?)\n\s*\n", flat, re.S | re.I)
    dh = None
    if not m:                                                                 # website detail page text
        dh = DETAIL_HDR_RE.search(" ".join(flat.split()))
        if dh:
            ref = re.sub(r"\s+", "", dh.group(1))
            published = f"{dh.group(4)}-{dh.group(3)}-{dh.group(2)} 00:00"
            pub_year = int(dh.group(4))
            rest = dh.group(6)
            title = re.split(r"\s(?:Line Block|Power Block|In view|Due to|Consequent|As part|On account|Following)", rest)[0][:150].strip()
        else:
            ref = fallback_ref
    if not ref:
        return None
    if pm and not dh:
        title = " ".join(pm.group(1).split())
    if not title:
        title = next((ln.strip() for ln in flat.splitlines() if re.search(r"[A-Z]{4}", ln) and "PUB/" not in ln and "RAILWAY" not in ln.upper()), "")
    title = title[:200]

    body = flat
    if pm and not dh:
        body = flat[pm.end():]
    elif dh:
        body = dh.group(6)
    body = re.sub(r"(Follow us|SD/-|Sd/-).*", "", body, flags=re.S)           # signature block / links
    body_one = " ".join(body.split())

    # ---- a release that cancels an earlier one ("LINE BLOCK CANCELLED ... STANDS CANCELLED") -------------------
    if re.search(r"stands?\s+cancelled", body_one, re.I):
        ref_m = re.search(r"Press\s+Release\s+No\.?:?\s*(PUB\s*/\s*MAS\s*/\s*\d{4}\s*/\s*\d{2}\s*/\s*\d{1,2})", body_one, re.I)
        if ref_m:
            return {"ref": ref, "withdraws": re.sub(r"\s+", "", ref_m.group(1)).upper(), "skip_notice": True, "link": url,
                    "title": title}

    if not SERVICE_WORDS.search(title + " " + body_one[:1500]) or not re.search(r"train|EMU|MEMU|MRTS|express|suburban", body_one, re.I):
        return None                                                          # ceremonies, awards, sports ... not a service notice

    # ---- type -----------------------------------------------------------------------------------------------------
    up = body_one.upper()
    if re.search(r"FULL(Y)? CANCELL|PARTIAL(LY)? CANCELL|CANCELLED", up):
        ntype = "Cancellation"
    elif "DIVERT" in up:
        ntype = "Diversion"
    elif re.search(r"RESTOR|RESUM|REGULAR PATTERN|SUNDAY PATTERN|FREQUENCY|ORIGINAT|REGULATED|REGULATION|RESCHEDUL|DELAYED", up):
        ntype = "Timetable change"
    else:
        ntype = "Planned maintenance"

    # ---- "routes": service classes + every train number mentioned -------------------------------------------------
    classes = [c for c, pat in (("EMU", r"\bEMU\b"), ("MEMU", r"\bMEMU\b"), ("MRTS", r"\bMRTS\b"),
                                ("EXPRESS", r"\bexpress\b|superfast"), ("SUBURBAN", r"\bsuburban\b")) if re.search(pat, body_one, re.I)]
    trains = list(dict.fromkeys(a or b for a, b in TRAIN_NO_RE.findall(body_one)))[:40]
    routes = ", ".join(classes + trains) or "CHENNAI-DIVISION"

    # ---- stations -------------------------------------------------------------------------------------------------
    st = []
    for a, b in re.findall(r"Train\s*No\.?\s*\d{5}\s*,\s*([A-Z][A-Za-z .'()]+?)\s*[–-]\s*([A-Z][A-Za-z .'()]+?)\s+(?:EMU|MEMU|Express|Superfast|Semi|Passenger|local|Special|Mail|MRTS)", body_one):
        st += [a.strip(), b.strip()]
    st += [y.strip() for y in re.findall(r"at\s+([A-Z][A-Za-z .]+?)\s+Yard", body_one)]
    stations = ", ".join(list(dict.fromkeys(s for s in st if len(s) > 2))[:25])

    # ---- validity window ------------------------------------------------------------------------------------------
    scrub = re.sub(r"Date\s*:?\s*\d{1,2}\s*[./]\s*\d{1,2}\s*[./]\s*\d{4}", " ", flat)
    scrub = re.sub(r"(?:DATED|dated)\s*:?\s*\d{1,2}\.\d{1,2}\.\d{4}", " ", scrub)
    dates = _expand_dates(" ".join(scrub.split()), pub_year)
    if not dates and hd:
        dates = [date(int(hd.group(3)), int(hd.group(2)), int(hd.group(1)))]
    valid_from = valid_to = ""
    if dates:
        d0, d1 = dates[0], dates[-1]
        tm = TIME_RANGE_RE.search(body_one)
        if tm:
            h1, m1, h2, m2 = (int(x) for x in tm.groups())
            start = datetime(d0.year, d0.month, d0.day, h1 % 24, m1)
            end = datetime(d1.year, d1.month, d1.day, h2 % 24, m2)
            if end <= start:
                end += timedelta(days=1)
            valid_from, valid_to = start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M")
        else:
            valid_from, valid_to = d0.strftime("%Y-%m-%d") + " 00:00", d1.strftime("%Y-%m-%d") + " 23:59"
        if re.search(r"until\s+further\s+(advice|notice|orders?)|w\.?e\.?f\.?\b(?!.*\bto\b)", body_one, re.I) and not re.search(
                r"with effect from[^.]{0,40}\bto\b|from\s+\d{1,2}(?:st|nd|rd|th)?\s+\w+\s+to\s+\d", body_one, re.I):
            valid_to = ""                                                       # open-ended

    full = len(re.findall(r"FULLY CANCELLED", up))
    part = len(re.findall(r"PARTIALLY CANCELLED", up))
    div = len(re.findall(r"DIVERTED", up))
    sentences = re.split(r"(?<=[a-z0-9\)])\.\s+(?=[A-Z])", body_one)
    key = next((x for x in sentences if re.search(r"block|resumed|restored|cancelled|diverted|frequency", x, re.I)), sentences[0] if sentences else "")
    key = re.sub(r"^(According|Accordingly)[^,]*?(priority|Priority)[^,]*?,\s*", "", key)
    key = re.sub(r"\s+", " ", key).strip()
    key = re.sub(r"^as a part of ongoing engineering works,?\s*", "Engineering works: ", key, flags=re.I)
    if dh and title and key.startswith(title):
        key = key[len(title):].strip(" .:-") or key
    key = key[:1].upper() + key[1:]
    counts = "; ".join(x for x in (f"{full} train(s) fully cancelled" if full else "", f"{part} partially cancelled" if part else "",
                                   f"{div} diverted" if div else "") if x)
    summary = (key[:460] + (f" [{counts}]" if counts else "")).strip()
    if title.isupper() and not dh:
        title = re.sub(r"\b(Emu|Memu|Mrts|Prs)\b", lambda mm: mm.group(1).upper(), title.title())
        title = re.sub(r"\b(Of|In|At|And|To|On)\b", lambda mm: mm.group(1).lower(), title)
        title = title[:1].upper() + title[1:]
    if dates and re.match(r"(?i)changes in pattern", title):
        a, b = dates[0], dates[-1]
        title += f" ({a.day} {a.strftime('%b')}" + (f" - {b.day} {b.strftime('%b')}" if b != a else "") + f" {b.year})"
    return {"ref": ref, "title": title or "Chennai Division - change in train services", "routes": routes, "area": "Chennai Division, Southern Railway",
            "type": ntype, "valid_from": valid_from, "valid_to": valid_to, "published": published, "summary": summary,
            "stations": stations, "status": "", "link": url}


class RailwayPdfAdapter:
    document_source = True

    def __init__(self, cfg=None, extract_text=None):
        self.cfg = cfg or {}
        self.extract_text = extract_text or (lambda data: extract_pdf_text(data))
        self.stats = {}

    def list_pdf_links(self, body, base_url):
        lp = _LinkParser()
        lp.feed(body or "")
        pat = re.compile(self.cfg.get("link_pattern", r"(?i)(PUB|PRESS)"))
        urls = []
        for href, text in lp.links:
            if not href or not re.search(r"\.pdf(\?|#|$)", href, re.I):
                continue
            full = urljoin(base_url, href.strip())
            if urlparse(full).scheme in ("http", "https") and (pat.search(unquote(full)) or pat.search(text)):
                urls.append(full)
        return urls

    def parse(self, body, source, fetch=None, known=(), min_delay=None):
        base = source["url"]
        listed = list(dict.fromkeys(self.list_pdf_links(body, base) + list(source.get("pdf_urls", []))))
        known = set(known)
        todo = [u for u in listed if u not in known][: self.cfg.get("max_documents_per_run", 20)]
        items, processed = [], []
        failures = []

        def handle(key, text_fn, link, fallback):
            try:
                text = text_fn()
                item = parse_press_release(text, link, fallback_ref=fallback)
            except (FetchError, ParseError) as e:
                failures.append({"document": key, "error": f"{getattr(e, 'kind', 'parse')}: {e}"})
                processed.append((key, None, "failed", str(e)))
                return
            if item is None:
                processed.append((key, None, "non_service", ""))
            elif item.get("skip_notice"):
                items.append(item)
                processed.append((key, item["ref"], "withdrawal", ""))
            else:
                items.append(item)
                processed.append((key, item["ref"], "notice", ""))

        for url in todo:
            fallback = unquote(urlparse(url).path.rsplit("/", 1)[-1])[:80]
            handle(url, lambda u=url: self.extract_text(fetch(u).raw), url, fallback)

        # documents a person downloaded in a browser and imported (data/inbox): same parser, no scraping involved
        inbox_listed = []
        if source.get("use_inbox") and source.get("_data_dir"):
            folder = Path(source["_data_dir"]) / "inbox"
            for f in sorted(folder.glob("*")) if folder.exists() else []:
                if f.suffix.lower() not in (".pdf", ".txt"):
                    continue
                key = f"inbox:{f.name}:{f.stat().st_size}"
                inbox_listed.append(key)
                if key in known:
                    continue
                link = source.get("inbox_link", "https://sr.indianrailways.gov.in/")
                if f.suffix.lower() == ".txt":
                    first = f.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
                    mm = re.match(r"\s*SOURCE-URL:\s*(https?://\S+)", first)
                    link = mm.group(1) if mm else link
                reader = (lambda f=f: self.extract_text(f.read_bytes())) if f.suffix.lower() == ".pdf" else (lambda f=f: re.sub(r"^\s*SOURCE-URL:.*\n", "", f.read_text(encoding="utf-8", errors="replace")))
                handle(key, reader, link, f.stem[:80])

        self.stats = {"links_found": len(listed) + len(inbox_listed), "listed_urls": listed, "processed": processed,
                      "failed": len(failures), "failures": failures[:15], "skipped_known": len(known & set(listed + inbox_listed))}
        return items
