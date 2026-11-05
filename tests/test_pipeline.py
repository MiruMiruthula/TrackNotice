"""End-to-end tests: fake network -> refresh -> database -> web/API. Includes the 5 mandatory acceptance tests
from the project brief (section 11) and the live mentor challenges (section 10)."""
import json
import sqlite3
import unittest

from app.alerts import DemoSender
from app.db import connect, migrate
from helpers import URL, fx, make_env


class Base(unittest.TestCase):
    def setUp(self):
        self.app, self.web, self.clock, self.sleeps = make_env()
        self.web.set(URL, fx("page_v1.html"))
        self.client = self.app.test_client()
        self.refresher = self.app.extensions["refresher"]
        self.db = connect(self.app.config["DATABASE_PATH"])

    def tearDown(self):
        self.db.close()

    def refresh(self):
        return self.refresher.refresh_all()[0]

    def api(self, qs=""):
        return self.client.get("/api/notices?status=all&" + qs).get_json()["notices"]

    def by_ref(self, ref):
        return self.db.execute("SELECT * FROM notices WHERE external_ref=?", (ref,)).fetchone()


class AcceptanceTests(Base):
    def test_1_expired_advisory_is_not_marked_active(self):
        self.refresh()
        self.assertEqual(self.by_ref("ADV-4")["status"], "expired")
        active = {n["title"] for n in self.client.get("/api/notices").get_json()["notices"]}
        self.assertNotIn("Egmore stop closed", active)
        archive = self.client.get("/api/notices?status=expired").get_json()["notices"]
        self.assertEqual([n["title"] for n in archive], ["Egmore stop closed"])

    def test_1b_expiry_follows_the_clock_even_without_a_refresh(self):
        self.refresh()
        self.clock.advance(hours=6)              # 21:30 IST: the 20:00 outage window has ended, no refresh ran
        titles = {n["title"] for n in self.client.get("/api/notices").get_json()["notices"]}
        self.assertNotIn("Blue Line interruption", titles)
        outage = [n for n in self.api() if n["title"] == "Blue Line interruption"][0]
        self.assertEqual(outage["status"], "expired")

    def test_2_route_filter_works_with_multiple_affected_routes(self):
        self.refresh()
        for route in ("12B", "21g", "29C"):
            hits = self.api(f"route={route}")
            self.assertEqual([n["title"] for n in hits], ["Route diversion at T. Nagar"], route)
        self.assertEqual(self.api("route=12B")[0]["route_id"], ["12B", "21G", "29C"])
        self.assertEqual(self.api("route=99Z"), [])

    def test_3_extended_advisory_updates_existing_history(self):
        self.refresh()
        before = self.by_ref("ADV-2")
        self.web.set(URL, fx("page_v2.html"))
        r = self.refresh()
        after = self.by_ref("ADV-2")
        self.assertEqual(after["notice_id"], before["notice_id"])           # same notice, not a duplicate
        self.assertEqual((before["revision_id"], after["revision_id"]), (1, 2))
        self.assertIn("Extended by 1 d", after["change_summary"])
        revs = self.db.execute("SELECT revision_id, change_summary FROM notice_revisions WHERE notice_id=? ORDER BY 1", (after["notice_id"],)).fetchall()
        self.assertEqual([x[0] for x in revs], [1, 2])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notices WHERE external_ref='ADV-2'").fetchone()[0], 1)
        self.assertGreaterEqual(r["revised"], 1)
        # and the window really moved by exactly 24 h
        self.assertEqual((before["valid_to"], after["valid_to"]), ("2026-11-01T14:30:00Z", "2026-11-02T14:30:00Z"))

    def test_4_time_zone_handling_is_documented_and_reproducible(self):
        self.refresh()
        n = self.by_ref("ADV-1")
        self.assertEqual(n["valid_from"], "2026-10-31T00:30:00Z")           # 06:00 IST stored as UTC
        self.assertEqual(n["valid_to"], "2026-11-03T16:30:00Z")             # 22:00 IST
        html = self.client.get("/?status=all").get_data(as_text=True)
        self.assertIn("31 Oct 2026, 06:00 AM IST", html)                    # displayed back in IST with the zone name
        self.assertIn("03 Nov 2026, 10:00 PM IST", html)

    def test_5_stale_notice_shows_latest_known_verification(self):
        self.refresh()
        self.clock.advance(hours=3)                                         # no refresh for 3 h (> 31 min stale limit)
        n = [x for x in self.api() if x["title"] == "Route diversion at T. Nagar"][0]
        self.assertEqual(n["freshness"]["level"], "stale")
        self.assertEqual(n["last_confirmed_at"], "2026-11-01T10:00:00Z")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("STALE", page)
        self.assertNotIn(">LIVE<", page.replace("<span", ">").replace("</span>", "<"))   # never labelled live
        self.assertIn("3 h ago", page)

    def test_6_source_error_does_not_turn_old_data_into_live_data(self):
        self.refresh()
        self.web.set(URL, "down", 503)
        self.clock.advance(minutes=20)
        r = self.refresh()
        self.assertEqual(r["outcome"], "error")
        self.assertEqual(len(self.sleeps), 2)                               # 3 attempts -> 2 backoff sleeps
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notices").fetchone()[0], 4)   # old data retained
        n = [x for x in self.api() if x["title"] == "Route diversion at T. Nagar"][0]
        self.assertEqual(n["freshness"]["level"], "error")                  # 20 min old would be "live" if the error were ignored
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("SOURCE ERROR", page)
        self.assertNotIn("health-live", page)
        src = self.client.get("/api/sources").get_json()[0]
        self.assertEqual(src["health"]["level"], "error")
        self.assertEqual(src["last_success_at"], "2026-11-01T10:00:00Z")


class ChangeDetectionTests(Base):
    def test_first_refresh_counts_and_duplicate_removal(self):
        r = self.refresh()
        self.assertEqual((r["outcome"], r["found"], r["new"], r["duplicates"]), ("ok", 5, 4, 1))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notices").fetchone()[0], 4)

    def test_repeat_run_is_idempotent(self):
        self.refresh()
        snap = [tuple(r) for r in self.db.execute("SELECT notice_id, revision_id, content_hash, observed_at FROM notices ORDER BY 1")]
        self.clock.advance(minutes=15)
        r = self.refresh()
        self.assertEqual((r["new"], r["revised"], r["withdrawn"]), (0, 0, 0))
        self.assertEqual(snap, [tuple(x) for x in self.db.execute("SELECT notice_id, revision_id, content_hash, observed_at FROM notices ORDER BY 1")])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notice_revisions").fetchone()[0], 4)
        self.assertEqual(self.by_ref("ADV-1")["last_confirmed_at"], "2026-11-01T10:15:00Z")   # confirmation timestamp moved

    def test_update_page_new_cancelled_and_vanished(self):
        self.refresh()
        self.web.set(URL, fx("page_v2.html"))
        r = self.refresh()
        self.assertEqual(self.by_ref("ADV-3")["status"], "withdrawn")             # explicit "Cancelled"
        self.assertEqual(self.by_ref("ADV-4")["status"], "expired")                # vanished but already over -> NOT withdrawn
        self.assertEqual(self.by_ref("ADV-4")["revision_id"], 1)
        n5 = self.by_ref("ADV-5")
        self.assertIsNone(n5["valid_to"])                                          # advisory without an end date (mentor challenge)
        api5 = [x for x in self.api() if x["title"] == "Waterlogging Velachery"][0]
        self.assertEqual((api5["status"], api5["end_announced"]), ("active", False))
        self.assertIn("Waterlogging Velachery", self.client.get("/").get_data(as_text=True))
        self.assertIn("End time not announced", self.client.get("/").get_data(as_text=True))

    def test_vanished_live_notice_is_withdrawn(self):
        self.refresh()
        page = fx("page_v1.html")
        a = page.index('<div class="advisory" data-ref="ADV-2">'); b = page.index("</div>", a) + 6
        self.web.set(URL, page[:a] + page[b:])
        r = self.refresh()
        self.assertEqual(r["withdrawn"], 1)
        self.assertEqual(self.by_ref("ADV-2")["status"], "withdrawn")
        self.assertIn("withdrawn", self.by_ref("ADV-2")["change_summary"])

    def test_empty_page_is_not_mass_withdrawal(self):
        self.refresh()
        self.web.set(URL, "<html><body><p>Site redesigned</p></body></html>")
        r = self.refresh()
        self.assertEqual(r["outcome"], "suspicious_empty")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notices WHERE status='withdrawn'").fetchone()[0], 0)
        self.assertEqual(self.client.get("/api/sources").get_json()[0]["health"]["level"], "error")

    def test_unparseable_card_is_not_withdrawn(self):
        self.refresh()
        page = fx("page_v1.html").replace('<span class="adv-routes">Blue Line</span>', "")   # ADV-2 loses its route
        self.web.set(URL, page)
        r = self.refresh()
        self.assertEqual(r["rejected"], 1)
        self.assertEqual(self.by_ref("ADV-2")["status"], "active")
        self.assertEqual(self.by_ref("ADV-2")["revision_id"], 1)

    def test_malformed_page_partial_success(self):
        self.web.set(URL, fx("page_malformed.html"))
        r = self.refresh()
        self.assertEqual((r["outcome"], r["new"], r["rejected"]), ("ok", 2, 3))
        detail = json.loads(self.db.execute("SELECT detail FROM refresh_log ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual({d["ref"] for d in detail["rejected"]}, {"M-2", "M-3", "M-4"})

    def test_html_in_titles_is_escaped_on_the_page(self):
        self.web.set(URL, '<div class="advisory" data-ref="X"><span class="adv-title">&lt;script&gt;alert(1)&lt;/script&gt;</span>'
                          '<span class="adv-routes">7</span><span class="adv-from">01 Nov 2026</span><p class="adv-text">x</p></div>')
        self.refresh()
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", page)

    def test_not_modified_keeps_data_confirmed(self):
        self.web.set(URL, fx("page_v1.html"), headers={"content-type": "text/html", "etag": '"v1"'})
        self.refresh()
        self.clock.advance(minutes=15)
        self.web.set(URL, "", 304)
        r = self.refresh()
        self.assertEqual(r["outcome"], "not_modified")
        self.assertEqual(self.by_ref("ADV-1")["last_confirmed_at"], "2026-11-01T10:15:00Z")
        sent = [c for c in self.web.calls if c[0] == URL][-1][1]
        self.assertEqual(sent.get("If-None-Match"), '"v1"')

    def test_robots_disallow_end_to_end(self):
        self.web.set("http://fake.test/robots.txt", "User-agent: *\nDisallow: /\n")
        r = self.refresh()
        self.assertEqual(r["outcome"], "error")
        self.assertIn("robots_disallowed", r["error"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notices").fetchone()[0], 0)
        self.assertFalse([c for c in self.web.calls if c[0] == URL])


class FilterTests(Base):
    def setUp(self):
        super().setUp()
        self.refresh()

    def test_filters(self):
        titles = lambda qs: sorted(n["title"] for n in self.api(qs))
        self.assertEqual(titles("q=diversion"), ["Route diversion at T. Nagar"])
        self.assertEqual(titles("station=guindy"), [])                      # stations only listed for ADV-1
        self.assertEqual(titles("station=usman"), ["Route diversion at T. Nagar"])
        self.assertEqual(titles("area=adyar"), ["Night maintenance 5C"])
        self.assertEqual(titles("type=STOP_CLOSURE"), ["Egmore stop closed"])
        self.assertEqual(titles("q=%25"), [])                                # LIKE wildcards are escaped

    def test_service_date_filter_uses_overlap_in_display_timezone(self):
        t = lambda d: sorted(n["title"] for n in self.api("date=" + d))
        self.assertEqual(t("2026-11-04"), ["Night maintenance 5C"])         # ADV-1 ended 3 Nov 22:00 IST; 5C 3 Nov 22:00 -> 4 Nov 05:00
        self.assertIn("Route diversion at T. Nagar", t("2026-11-03"))
        self.assertEqual(t("2026-10-29"), ["Egmore stop closed"])

    def test_status_views(self):
        self.assertEqual(len(self.client.get("/api/notices").get_json()["notices"]), 2)                  # active: ADV-1, ADV-2
        self.assertEqual(len(self.client.get("/api/notices?status=scheduled").get_json()["notices"]), 1)  # ADV-3
        self.assertEqual(self.client.get("/api/notices?status=all").get_json()["total"], 4)

    def test_pages_render(self):
        nid = self.by_ref("ADV-1")["notice_id"]
        for path in ("/", "/?status=all", f"/notice/{nid}", "/sources", "/about", "/subscribe", "/health", "/api/version"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.client.get("/notice/nope").status_code, 404)


class AlertTests(Base):
    def subscribe(self, email, routes):
        self.db.execute("INSERT INTO subscribers(contact,subscriber_filter,unsubscribe_token,created_at) VALUES (?,?,?,?)",
                        (email, json.dumps({"routes": routes, "areas": [], "types": []}), email + "tok", "2026-11-01T00:00:00Z"))
        self.db.commit()

    def alert_rows(self):
        return self.db.execute("SELECT * FROM alert_log ORDER BY alert_id").fetchall()

    def test_one_alert_per_new_or_revised_advisory(self):
        self.subscribe("blue@example.com", ["BLUE-LINE"])
        self.subscribe("bus@example.com", ["12B"])
        self.refresh()
        self.assertEqual([(a["notice_id"] == self.by_ref("ADV-2")["notice_id"]) for a in self.alert_rows() if a["subscriber_id"] == 1], [True])
        self.assertEqual(len(self.alert_rows()), 2)                          # ADV-1 -> bus, ADV-2 -> blue ; expired ADV-4 -> none
        for _ in range(3):                                                    # repeated identical refreshes: no more alerts
            self.clock.advance(minutes=15)
            self.refresh()
        self.assertEqual(len(self.alert_rows()), 2)
        self.web.set(URL, fx("page_v2.html"))                                 # outage extended -> exactly one more, for blue only
        self.refresh()
        self.refresh()
        rows = self.alert_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual((rows[-1]["revision_id"], rows[-1]["subscriber_id"]), (2, 1))
        self.assertEqual({r["alert_status"] for r in rows}, {"demo_logged"})
        self.assertEqual(self.by_ref("ADV-2")["alert_status"], "demo_logged")
        with open(self.app.config["DATA_DIR"] + "/alerts_demo.log", encoding="utf-8") as fh:
            log = fh.read()
        self.assertEqual(log.count("TO: blue@example.com"), 2)
        self.assertIn("Extended by 1 d", log)

    def test_no_subscribers_status_and_expired_not_alerted(self):
        self.refresh()
        self.assertEqual(self.by_ref("ADV-1")["alert_status"], "no_subscribers")
        self.assertEqual(self.by_ref("ADV-4")["alert_status"], "none")

    def test_failed_delivery_is_retried_then_stops(self):
        self.subscribe("bus@example.com", ["12B"])
        calls = []

        class Flaky:
            mode = "smtp"
            def send(self, *a):
                calls.append(a)
                raise ConnectionError("smtp down")
        self.refresher.sender = Flaky()
        for _ in range(5):
            self.clock.advance(minutes=15)
            self.refresh()
        a = self.alert_rows()[0]
        self.assertEqual((a["alert_status"], a["attempts"]), ("failed", 3))   # ALERT_MAX_ATTEMPTS = 3
        self.assertEqual(len(calls), 3)
        self.assertIn("smtp down", a["error"])
        self.assertEqual(self.by_ref("ADV-1")["alert_status"], "failed")
        self.refresher.sender = DemoSender(self.app.config["DATA_DIR"] + "/x.log")


class WebSecurityTests(Base):
    def test_subscribe_requires_csrf_and_validates_email(self):
        r = self.client.post("/subscribe", data={"email": "a@b.co"})
        self.assertEqual(r.status_code, 400)
        with self.client.session_transaction() as s:
            s["csrf"] = "tok"
        bad = self.client.post("/subscribe", data={"email": "nope", "csrf_token": "tok"})
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post("/subscribe", data={"email": "A@B.co", "routes": "12b, 21g", "csrf_token": "tok"})
        self.assertEqual(ok.status_code, 302)
        row = self.db.execute("SELECT * FROM subscribers").fetchone()
        self.assertEqual((row["contact"], json.loads(row["subscriber_filter"])["routes"]), ("a@b.co", ["12B", "21G"]))

    def test_admin_actions_need_token(self):
        self.assertEqual(self.client.post("/admin/refresh").status_code, 400)                      # no csrf, no token
        self.assertEqual(self.client.get("/admin").status_code, 302)                                # redirected to login
        r = self.client.post("/admin/refresh", headers={"X-Admin-Token": "wrong"})
        self.assertIn(r.status_code, (400, 403))
        r = self.client.post("/admin/refresh", headers={"X-Admin-Token": "adm-token-xyz"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()[0]["outcome"], "ok")
        self.assertEqual(self.client.get("/api/sources").get_json()[0]["health"]["level"], "live")

    def test_admin_login_flow(self):
        with self.client.session_transaction() as s:
            s["csrf"] = "tok"
        self.assertEqual(self.client.post("/admin/login", data={"token": "bad", "csrf_token": "tok"}).status_code, 403)
        self.assertEqual(self.client.post("/admin/login", data={"token": "adm-token-xyz", "csrf_token": "tok"}).status_code, 302)
        self.assertEqual(self.client.get("/admin").status_code, 200)

    def test_no_secrets_in_pages(self):
        self.refresh()
        for path in ("/", "/sources", "/api/sources", "/api/notices", "/admin/login"):
            body = self.client.get(path).get_data(as_text=True)
            self.assertNotIn("adm-token-xyz", body, path)
            self.assertNotIn(self.app.config["SECRET_KEY"], body, path)


class MigrationTests(unittest.TestCase):
    def test_migrations_are_repeatable(self):
        app, *_ = make_env()
        conn = connect(app.config["DATABASE_PATH"])
        self.assertEqual(migrate(conn, app.config["MIGRATIONS_DIR"]), [])
        with self.assertRaises(sqlite3.IntegrityError):                       # DB-level constraint: window must not be inverted
            conn.execute("INSERT INTO sources(source_id,name,agency,url,kind) VALUES ('s','n','a','u','html')")
            conn.execute("""INSERT INTO notices(notice_id,source_id,agency,route_id,source_url,type,title,summary,valid_from,valid_to,
                            status,observed_at,last_confirmed_at,content_hash,updated_at)
                            VALUES ('x','s','a','1','u','OTHER','t','s','2026-11-02T00:00:00Z','2026-11-01T00:00:00Z','active','n','n','h','n')""")
        with self.assertRaises(sqlite3.IntegrityError):                       # unknown type rejected
            conn.execute("""INSERT INTO notices(notice_id,source_id,agency,route_id,source_url,type,title,summary,valid_from,
                            status,observed_at,last_confirmed_at,content_hash,updated_at)
                            VALUES ('y','s','a','1','u','BOGUS','t','s','2026-11-02T00:00:00Z','active','n','n','h','n')""")
        conn.close()


if __name__ == "__main__":
    unittest.main()
