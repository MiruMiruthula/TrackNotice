"""'python manage.py inspect URL' - helps you set up a REAL source safely, on your own machine/network:
checks robots.txt for our crawler, downloads the page once, and reports PDF links and repeated CSS classes
(candidates for the advisory 'card' selector) so you can fill sources.json."""
import re
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse

from .adapters.base import BOT_TOKEN, FetchError


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.classes, self.pdfs, self.title, self._in_title = Counter(), [], "", False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for c in (a.get("class") or "").split():
            self.classes[(tag, c)] += 1
        if tag == "a" and a.get("href") and re.search(r"\.pdf(\?|#|$)", a["href"], re.I):
            self.pdfs.append(a["href"])
        self._in_title = tag == "title"

    def handle_data(self, d):
        if self._in_title:
            self.title += d

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False


def inspect(url, fetcher, out=print):
    out(f"\nInspecting {url}\nCrawler identity: {BOT_TOKEN}\n")
    if not fetcher.allowed(url):
        out("RESULT: robots.txt DISALLOWS (or could not confirm) automated access to this URL for our crawler.")
        out("        Do NOT scrape it. Use an official feed/API, another page that is allowed, or ask the agency for permission.")
        return False
    out("robots.txt: access allowed for this URL.")
    try:
        res = fetcher.get(url, min_delay=0)
    except FetchError as e:
        out(f"Fetch failed: {e.kind}: {e}")
        return False
    scan = _Scan()
    scan.feed(res.body)
    out(f"Page title: {' '.join(scan.title.split()) or '(none)'}   size: {len(res.body)} characters")
    pdfs = list(dict.fromkeys(urljoin(url, p) for p in scan.pdfs))
    out(f"\nPDF links found: {len(pdfs)}")
    for p in pdfs[:15]:
        out("   " + unquote(p)[:130])
    rep = [(f"{t}.{c}", n) for (t, c), n in scan.classes.most_common(40) if n >= 3]
    out("\nRepeated CSS classes (an advisory 'card' is usually a class repeated once per notice):")
    for name, n in rep[:15]:
        out(f"   {name:40s} x{n}")
    out("\nNEXT STEPS")
    if pdfs:
        out('  * The page links PDF documents. If they are Southern Railway-style press releases, use kind "railway_pdf" and put this URL in .env as SR_PRESS_URL.')
        out('    Adjust "link_pattern" in sources.json if the PDF file names do not contain PUB or PRESS.')
    else:
        out('  * No PDFs. If notices are repeated blocks, use kind "html": set parser.item_class to the repeated class above and map each field')
        out('    (title, routes, valid_from, valid_to, summary...) to the class that holds it. Check against "View page source".')
        out('  * If the notices are loaded by JavaScript (page source is nearly empty), look for an official RSS/API feed instead.')
    out("  * Record robots.txt + terms in docs/SOURCE_APPROVAL.md and get mentor approval before enabling.")
    return True
