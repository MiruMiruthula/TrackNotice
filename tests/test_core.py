import unittest
from datetime import timezone

from app.adapters.html_cards import HtmlCardsAdapter
from app.adapters.rss import RssAdapter
from app.adapters.base import Fetcher, FetchError, ParseError
from app.normalize import NoticeError, classify_type, normalize, parse_routes
from app.timeutil import fmt_local, parse_local, to_iso
from helpers import FakeWeb, fx

SRC = {"source_id": "s", "agency": "A", "url": "http://x/y", "timezone": "Asia/Kolkata"}


class TimeTests(unittest.TestCase):
    def test_ist_is_converted_to_utc(self):
        self.assertEqual(to_iso(parse_local("31 Oct 2026, 06:00 AM")), "2026-10-31T00:30:00Z")
        self.assertEqual(to_iso(parse_local("2026-11-03 22:00")), "2026-11-03T16:30:00Z")
        self.assertEqual(to_iso(parse_local("05/11/2026 10:00 PM")), "2026-11-05T16:30:00Z")   # dd/mm/yyyy
        self.assertEqual(to_iso(parse_local("Thu, 5th Nov 2026 22:00 hrs")), "2026-11-05T16:30:00Z")
        self.assertEqual(to_iso(parse_local("2026-11-05T22:00:00+05:30")), "2026-11-05T16:30:00Z")
        self.assertEqual(to_iso(parse_local("2026-11-05T16:30:00Z")), "2026-11-05T16:30:00Z")

    def test_date_only_start_and_end(self):
        self.assertEqual(to_iso(parse_local("05 Nov 2026")), "2026-11-04T18:30:00Z")           # 00:00 IST
        self.assertEqual(to_iso(parse_local("05 Nov 2026", end_of_day=True)), "2026-11-05T18:29:59Z")  # 23:59:59 IST

    def test_display_has_zone_name(self):
        self.assertEqual(fmt_local("2026-11-03T16:30:00Z"), "03 Nov 2026, 10:00 PM IST")

    def test_garbage_rejected(self):
        for bad in ("", None, "sometime next week", "32/13/2026"):
            with self.assertRaises(ValueError):
                parse_local(bad)


class NormalizeTests(unittest.TestCase):
    def test_multiple_routes(self):
        self.assertEqual(parse_routes("Routes: 12B, 21G / 29c and 5C"), ["12B", "21G", "29C", "5C"])
        self.assertEqual(parse_routes("Blue Line"), ["BLUE-LINE"])
        self.assertEqual(parse_routes("<script>"), [])

    def test_type_rules(self):
        self.assertEqual(classify_type("Diversion"), "DIVERSION")
        self.assertEqual(classify_type("", "Service interruption on Blue Line"), "SERVICE_DISRUPTION")
        self.assertEqual(classify_type("", "Night maintenance"), "PLANNED_MAINTENANCE")
        self.assertEqual(classify_type("", "Revised last train timing"), "TIMETABLE_CHANGE")
        self.assertEqual(classify_type("", "Hello"), "OTHER")

    def test_open_ended_allowed(self):
        n = normalize({"ref": "1", "title": "t", "routes": "47D", "valid_from": "01 Nov 2026", "valid_to": "", "summary": "s"}, SRC)
        self.assertIsNone(n["valid_to"])

    def test_rejections(self):
        base = {"ref": "1", "title": "t", "routes": "12B", "valid_from": "01 Nov 2026", "summary": "s"}
        for patch, why in [({"routes": ""}, "route"), ({"valid_from": "later"}, "start"),
                           ({"valid_from": "03 Nov 2026", "valid_to": "02 Nov 2026"}, "before"), ({"title": "", "summary": ""}, "title")]:
            with self.assertRaises(NoticeError, msg=why):
                normalize({**base, **patch}, SRC)

    def test_hash_ignores_published_time_but_not_end_time(self):
        a = normalize({"ref": "1", "title": "t", "routes": "12B", "valid_from": "01 Nov 2026", "valid_to": "02 Nov 2026", "summary": "s", "published": "01 Nov 2026 01:00"}, SRC)
        b = normalize({"ref": "1", "title": "t", "routes": "12B", "valid_from": "01 Nov 2026", "valid_to": "02 Nov 2026", "summary": "s", "published": "01 Nov 2026 09:00"}, SRC)
        c = normalize({"ref": "1", "title": "t", "routes": "12B", "valid_from": "01 Nov 2026", "valid_to": "03 Nov 2026", "summary": "s"}, SRC)
        self.assertEqual(a["content_hash"], b["content_hash"])
        self.assertNotEqual(a["content_hash"], c["content_hash"])
        self.assertEqual(a["notice_id"], c["notice_id"])        # identity survives an extension


class ParserTests(unittest.TestCase):
    def test_clean_page(self):
        items = HtmlCardsAdapter().parse(fx("page_v1.html"))
        self.assertEqual(len(items), 5)                         # includes the duplicate ADV-1
        first = items[0]
        self.assertEqual(first["ref"], "ADV-1")
        self.assertEqual(first["routes"], "12B, 21G / 29C")
        self.assertEqual(first["link"], "/n/1")

    def test_malformed_html_does_not_crash_and_isolates_bad_cards(self):
        items = HtmlCardsAdapter().parse(fx("page_malformed.html"))
        self.assertEqual([i["ref"] for i in items], ["M-1", "M-2", "M-3", "M-4", "M-5"])
        ok, errors = [], []
        for it in items:
            try:
                ok.append(normalize(it, SRC))
            except NoticeError as e:
                errors.append((e.ref, e.reason))
        self.assertEqual([n["external_ref"] for n in ok], ["M-1", "M-5"])
        self.assertEqual([e[0] for e in errors], ["M-2", "M-3", "M-4"])
        self.assertEqual(ok[1]["title"], "Bad & escaped title")   # tags stripped, entities decoded

    def test_not_html(self):
        with self.assertRaises(ParseError):
            HtmlCardsAdapter().parse("just text")

    def test_missing_fields_become_empty_not_exceptions(self):
        items = HtmlCardsAdapter().parse('<div class="advisory" data-ref="Z"><span class="adv-title">Only title</span></div>')
        self.assertEqual(items[0]["routes"], "")

    def test_rss(self):
        items = RssAdapter().parse(fx("feed.xml"))
        n = normalize(items[0], {**SRC, "kind": "rss"})
        self.assertEqual(n["routes"], ["15", "15A"])
        self.assertEqual(n["type"], "TIMETABLE_CHANGE")
        self.assertEqual(n["valid_to"], "2026-11-09T17:30:00Z")
        self.assertIn("First bus now 4:45 AM", n["summary"])

    def test_rss_entity_bomb_refused(self):
        with self.assertRaises(ParseError):
            RssAdapter().parse(fx("feed_evil.xml"))


class FetcherTests(unittest.TestCase):
    def setUp(self):
        self.web, self.sleeps = FakeWeb(), []
        self.f = Fetcher("UA", max_retries=3, backoff_base=1.0, http_get=self.web, sleep=self.sleeps.append)

    def test_retry_with_exponential_backoff_then_success(self):
        seq = [(503, {}, b""), (503, {}, b""), (200, {"content-type": "text/html"}, b"<html>ok</html>")]
        self.web.routes["http://h/p"] = None
        calls = iter(seq)
        orig = self.web.__call__
        self.f.http_get = lambda url, h, t, m: (404, {}, b"") if url.endswith("robots.txt") else next(calls)
        r = self.f.get("http://h/p", min_delay=0)
        self.assertEqual((r.status, r.attempts), ("ok", 3))
        self.assertEqual(len(self.sleeps), 2)
        self.assertTrue(1.0 <= self.sleeps[0] < 1.5 and 2.0 <= self.sleeps[1] < 2.5)   # 1s then 2s (+jitter)

    def test_retry_ceiling_then_failure(self):
        self.web.fail_network = True
        with self.assertRaises(FetchError) as cm:
            self.f.get("http://h/p", min_delay=0)
        self.assertEqual((cm.exception.kind, cm.exception.attempts), ("network", 3))
        self.assertEqual(len([c for c in self.web.calls if not c[0].endswith("robots.txt")]), 3)

    def test_403_is_not_retried_or_bypassed(self):
        self.web.set("http://h/p", "forbidden", 403)
        with self.assertRaises(FetchError) as cm:
            self.f.get("http://h/p", min_delay=0)
        self.assertEqual(cm.exception.status, 403)
        self.assertEqual(self.sleeps, [])

    def test_robots_disallow_blocks_request(self):
        self.web.set("http://h/robots.txt", "User-agent: *\nDisallow: /private/\n")
        self.web.set("http://h/private/x", "secret")
        with self.assertRaises(FetchError) as cm:
            self.f.get("http://h/private/x", min_delay=0)
        self.assertEqual(cm.exception.kind, "robots_disallowed")
        self.assertFalse([c for c in self.web.calls if c[0].endswith("/private/x")])

    def test_robots_server_error_means_disallow(self):
        self.web.set("http://h/robots.txt", "", 500)
        with self.assertRaises(FetchError):
            self.f.get("http://h/p", min_delay=0)

    def test_conditional_get_and_user_agent(self):
        self.web.set("http://h/p", "", 304)
        r = self.f.get("http://h/p", etag='"abc"', min_delay=0)
        self.assertEqual(r.status, "not_modified")
        page_call = [c for c in self.web.calls if c[0] == "http://h/p"][0]
        self.assertEqual(page_call[1]["If-None-Match"], '"abc"')
        self.assertEqual(page_call[1]["User-Agent"], "UA")

    def test_rate_limit_sleeps_between_requests_to_same_host(self):
        self.web.set("http://h/p", "<html></html>")
        t = [0.0]
        f = Fetcher("UA", http_get=self.web, sleep=lambda s: self.sleeps.append(s), clock=lambda: t[0])
        f.get("http://h/p", min_delay=5)
        f.get("http://h/p", min_delay=5)
        self.assertEqual(self.sleeps, [5])


if __name__ == "__main__":
    unittest.main()
