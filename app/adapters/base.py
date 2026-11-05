"""Polite HTTP fetching: robots.txt, per-host rate limit, timeout, retry with exponential backoff + jitter,
same-host redirects only, size limit, conditional GET. No login bypass, no CAPTCHA evasion, ever."""
import logging
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

log = logging.getLogger("transitnotice.fetch")
BOT_TOKEN = "TransitNoticeBot"


class FetchError(Exception):
    def __init__(self, kind, message, status=None, attempts=1):
        super().__init__(message)
        self.kind, self.status, self.attempts = kind, status, attempts


class ParseError(Exception):
    pass


@dataclass
class FetchResult:
    status: str            # "ok" | "not_modified"
    body: str
    etag: str = None
    last_modified: str = None
    http_status: int = 200
    attempts: int = 1
    duration_ms: int = 0
    raw: bytes = b""


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).netloc != urlparse(req.full_url).netloc:
            return None  # refuse cross-host redirects (robots.txt was only checked for the original host)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_http_get(url, headers, timeout, max_bytes):
    """Returns (status, response_headers(lowercase keys), body_bytes). Raises OSError/URLError on network faults."""
    opener = urllib.request.build_opener(_SameHostRedirect())
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read(max_bytes + 1)
            return r.status, {k.lower(): v for k, v in r.headers.items()}, body
    except urllib.error.HTTPError as e:
        try:
            body = e.read(2048)
        except Exception:
            body = b""
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body


class Fetcher:
    def __init__(self, user_agent, timeout=10.0, max_retries=3, backoff_base=1.0, backoff_cap=30.0,
                 max_bytes=2_000_000, http_get=default_http_get, sleep=time.sleep, clock=time.monotonic):
        self.user_agent, self.timeout, self.max_retries = user_agent, timeout, max(1, max_retries)
        self.backoff_base, self.backoff_cap, self.max_bytes = backoff_base, backoff_cap, max_bytes
        self.http_get, self.sleep, self.clock = http_get, sleep, clock
        self._robots, self._last_hit = {}, {}

    # ---- robots.txt -------------------------------------------------------------------------
    def allowed(self, url):
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        cached = self._robots.get(origin)
        if cached and self.clock() - cached[0] < 3600:
            rules = cached[1]
        else:
            rules = self._load_robots(origin)
            self._robots[origin] = (self.clock(), rules)
        if rules == "deny":
            return False
        if rules == "allow":
            return True
        return rules.can_fetch(BOT_TOKEN, url)

    def _load_robots(self, origin):
        try:
            status, _h, body = self.http_get(origin + "/robots.txt", {"User-Agent": self.user_agent},
                                             self.timeout, 500_000)
        except Exception as e:
            log.warning("robots.txt unreachable for %s (%s) -> treating as DISALLOW", origin, e)
            return "deny"
        if status == 200:
            rp = RobotFileParser()
            rp.parse(body.decode("utf-8", "replace").splitlines())
            return rp
        if 400 <= status < 500:       # RFC 9309: robots.txt unavailable (4xx) -> crawler may proceed
            return "allow"
        log.warning("robots.txt returned %s for %s -> treating as DISALLOW", status, origin)
        return "deny"

    # ---- throttling -------------------------------------------------------------------------
    def _throttle(self, url, min_delay):
        host = urlparse(url).netloc
        last = self._last_hit.get(host)
        if last is not None and min_delay > 0:
            wait = min_delay - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
        self._last_hit[host] = self.clock()

    # ---- main entry -------------------------------------------------------------------------
    def get(self, url, etag=None, last_modified=None, min_delay=5.0, max_bytes=None):
        if not self.allowed(url):
            raise FetchError("robots_disallowed", f"robots.txt disallows {url} for {BOT_TOKEN}")
        headers = {"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml,application/xml,text/xml,application/rss+xml;q=0.9,*/*;q=0.5"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        t0 = self.clock()
        limit = max_bytes or self.max_bytes
        last_err = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle(url, min_delay)
            retry_after = None
            try:
                status, rh, body = self.http_get(url, headers, self.timeout, limit)
            except Exception as e:                       # timeouts, DNS, connection refused ...
                last_err = FetchError("network", f"{type(e).__name__}: {e}", attempts=attempt)
            else:
                if status == 304:
                    return FetchResult("not_modified", "", etag, last_modified, 304, attempt, self._ms(t0))
                if status == 200:
                    if len(body) > limit:
                        raise FetchError("too_large", f"response larger than {limit} bytes", 200, attempt)
                    ctype = rh.get("content-type", "")
                    if ctype and not any(x in ctype.lower() for x in ("html", "xml", "text", "pdf", "octet-stream")):
                        raise FetchError("content_type", f"unexpected content-type {ctype}", 200, attempt)
                    charset = "utf-8"
                    if "charset=" in ctype.lower():
                        charset = ctype.lower().split("charset=")[1].split(";")[0].strip() or "utf-8"
                    try:
                        text = body.decode(charset, "replace")
                    except LookupError:
                        text = body.decode("utf-8", "replace")
                    return FetchResult("ok", text, rh.get("etag"), rh.get("last-modified"), 200, attempt, self._ms(t0), body)
                if status == 429 or status >= 500:
                    last_err = FetchError("http_error", f"HTTP {status}", status, attempt)
                    try:
                        retry_after = min(float(rh.get("retry-after", "")), self.backoff_cap)
                    except ValueError:
                        retry_after = None
                else:   # 401/403/404 etc.: do not retry, do not try to get around it
                    raise FetchError("http_error", f"HTTP {status}", status, attempt)
            if attempt < self.max_retries:
                delay = retry_after if retry_after is not None else \
                    min(self.backoff_cap, self.backoff_base * 2 ** (attempt - 1)) + random.uniform(0, 0.25 * self.backoff_base)
                log.warning("fetch %s failed (%s); retry %d/%d in %.1fs", url, last_err, attempt, self.max_retries - 1, delay)
                self.sleep(delay)
        last_err.attempts = self.max_retries
        raise last_err

    def _ms(self, t0):
        return int((self.clock() - t0) * 1000)
