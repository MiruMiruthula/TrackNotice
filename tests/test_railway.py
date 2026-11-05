"""Southern Railway press-release adapter: text parsing (real release text), PDF extraction, and the full pipeline."""
import unittest
from unittest import mock

from app.adapters import railway_pdf
from app.adapters.railway_pdf import RailwayPdfAdapter, extract_pdf_text, parse_press_release
from app.db import connect
from app.normalize import normalize
from helpers import FIX, fx, make_env

SRC = {"source_id": "sr", "agency": "Southern Railway", "url": "http://x/", "timezone": "Asia/Kolkata"}
LIST = "http://sr.test/press"
P = {n: f"http://sr.test/uploads/{n}.pdf" for n in ("a-PUB-MAS-2024-10-18", "b-PUB-MAS-2024-07-06", "c-PUB-MAS-2024-07-09", "d-PUB-MAS-2024-08-05")}


class ParserTests(unittest.TestCase):
    def test_real_release_cancellation(self):
        raw = parse_press_release(fx("sr_2024_10_28.txt"), "http://x/a.pdf")
        n = normalize(raw, SRC)
        self.assertEqual(raw["ref"], "PUB/MAS/2024/10/18")
        self.assertEqual(n["type"], "CANCELLATION")
        self.assertTrue({"EMU", "43894", "66007", "43001", "06744"} <= set(n["routes"]))
        self.assertEqual(n["valid_from"], "2024-10-27T18:30:00Z")         # 28 Oct 2024 00:00 IST
        self.assertEqual(n["valid_to"], "2024-10-28T22:00:00Z")           # 29 Oct 2024 03:30 IST (block end)
        self.assertEqual(n["published_at"], "2024-10-27T18:30:00Z")
        self.assertIn("3 train(s) fully cancelled", n["summary"])
        self.assertIn("1 diverted", n["summary"])
        self.assertIn("Avadi", raw["stations"])
        self.assertTrue(n["title"].startswith("Changes in Pattern of Train Services (28 Oct - 29 Oct 2024)"))

    def test_line_block_with_express_diversion(self):
        n = normalize(parse_press_release(fx("sr_2024_07_06.txt"), "u"), SRC)
        self.assertEqual((n["valid_from"], n["valid_to"]), ("2024-07-14T02:15:00Z", "2024-07-14T14:15:00Z"))   # 07:45-19:45 IST
        self.assertIn("12651", n["routes"])

    def test_open_ended_restoration(self):
        n = normalize(parse_press_release(fx("sr_2023_12_06.txt"), "u"), SRC)
        self.assertEqual(n["type"], "TIMETABLE_CHANGE")
        self.assertIsNone(n["valid_to"])                                 # "until further advice"

    def test_withdrawal_release(self):
        raw = parse_press_release(fx("sr_2024_07_09.txt"), "u")
        self.assertTrue(raw["skip_notice"])
        self.assertEqual(raw["withdraws"], "PUB/MAS/2024/07/06")

    def test_website_detail_page_text(self):
        raw = parse_press_release(fx("sr_detail_2026_08_21.txt"), "https://sr.example/view_detail.jsp?dcd=1")
        n = normalize(raw, SRC)
        self.assertEqual(raw["ref"], "326/2026-27")
        self.assertEqual(n["title"], "Regulation of Train Service")
        self.assertEqual(n["type"], "TIMETABLE_CHANGE")
        self.assertIn("16127", n["routes"])
        self.assertEqual((n["valid_from"], n["valid_to"]), ("2026-08-19T18:30:00Z", "2026-08-21T18:29:00Z"))   # 20 Aug 00:00 -> 21 Aug 23:59 IST
        self.assertEqual(n["published_at"], "2026-08-20T18:30:00Z")
        self.assertTrue(n["summary"].startswith("Line Block and Power Block"))

    def test_ceremony_is_not_a_service_notice(self):
        self.assertIsNone(parse_press_release(fx("sr_2024_08_15_ceremony.txt"), "u"))

    def test_garbage_text(self):
        self.assertIsNone(parse_press_release("hello world", "u"))

    def test_real_pdf_text_extraction(self):
        data = (FIX / "sr_sample.pdf").read_bytes()
        text = extract_pdf_text(data)
        self.assertIn("PUB/MAS/2024/10/18", text.replace(" ", ""))
        raw = parse_press_release(text, "u")
        self.assertEqual(raw["ref"], "PUB/MAS/2024/10/18")
        self.assertEqual(raw["type"], "Cancellation")

    def test_unreadable_pdf(self):
        with self.assertRaises(Exception):
            extract_pdf_text(b"not a pdf")

    def test_link_discovery(self):
        html = ('<a href="/uploads/x-PUB-MAS-1.pdf">Press</a><a href="/about.html">About</a><a href="https://o.example/q.pdf">other</a>'
                '<a href="javascript:void(0)">js</a><a href="/f/PRESS%20RELEASE.pdf?v=2">PR</a>')
        links = RailwayPdfAdapter({}).list_pdf_links(html, "http://sr.test/press")
        self.assertEqual(links, ["http://sr.test/uploads/x-PUB-MAS-1.pdf", "http://sr.test/f/PRESS%20RELEASE.pdf?v=2"])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.app, self.web, self.clock, self.sleeps = make_env(kind="railway_pdf", url=LIST, extra_source={"withdraw_missing": False, "parser": {}})
        self.patch = mock.patch.object(railway_pdf, "extract_pdf_text", lambda b: b.decode("utf-8"))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.ref = self.app.extensions["refresher"]
        self.db = connect(self.app.config["DATABASE_PATH"])
        self.client = self.app.test_client()
        self.listing = lambda names: "<html>" + "".join(f'<a href="{P[n]}">release</a>' for n in names) + '<a href="/other.html">x</a></html>'
        self.web.set(P["a-PUB-MAS-2024-10-18"], fx("sr_2024_10_28.txt"), headers={"content-type": "application/pdf"})
        self.web.set(P["b-PUB-MAS-2024-07-06"], fx("sr_2024_07_06.txt"), headers={"content-type": "application/pdf"})
        self.web.set(P["c-PUB-MAS-2024-07-09"], fx("sr_2024_07_09.txt"), headers={"content-type": "application/pdf"})
        self.web.set(P["d-PUB-MAS-2024-08-05"], fx("sr_2024_08_15_ceremony.txt"), headers={"content-type": "application/pdf"})

    def tearDown(self):
        self.db.close()

    def notice(self, ref):
        return self.db.execute("SELECT * FROM notices WHERE external_ref=?", (ref,)).fetchone()

    def downloads(self):
        return [c[0] for c in self.web.calls if c[0].endswith(".pdf")]

    def test_documents_become_notices_and_cancellation_withdraws_earlier_release(self):
        self.web.set(LIST, self.listing(P))
        r = self.ref.refresh_all()[0]
        self.assertEqual(r["outcome"], "ok")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notices").fetchone()[0], 2)         # ceremony ignored, withdrawal is not a notice
        self.assertEqual(self.notice("PUB/MAS/2024/10/18")["status"], "expired")                   # 2024 window is long over
        w = self.notice("PUB/MAS/2024/07/06")
        self.assertEqual(w["status"], "withdrawn")
        self.assertIn("Withdrawn by press release PUB/MAS/2024/07/09", w["change_summary"])
        self.assertEqual(w["revision_id"], 2)
        out = {r[0]: r[1] for r in self.db.execute("SELECT url, outcome FROM processed_documents")}
        self.assertEqual(sorted(out.values()), ["non_service", "notice", "notice", "withdrawal"])

    def test_each_pdf_is_downloaded_only_once(self):
        self.web.set(LIST, self.listing(P))
        self.ref.refresh_all()
        first = len(self.downloads())
        self.clock.advance(minutes=15)
        r = self.ref.refresh_all()[0]
        self.assertEqual(len(self.downloads()), first)                                              # nothing re-downloaded
        self.assertEqual((r["outcome"], r["new"], r["revised"]), ("ok", 0, 0))
        self.assertEqual(self.notice("PUB/MAS/2024/10/18")["last_confirmed_at"], "2026-11-01T10:15:00Z")  # still listed -> re-confirmed

    def test_notice_stays_if_pdf_disappears_from_listing(self):
        self.web.set(LIST, self.listing(P))
        self.ref.refresh_all()
        self.web.set(LIST, self.listing(["a-PUB-MAS-2024-10-18"]))
        self.clock.advance(minutes=15)
        r = self.ref.refresh_all()[0]
        self.assertEqual(r["withdrawn"], 0)
        self.assertEqual(self.notice("PUB/MAS/2024/10/18")["status"], "expired")                    # documents are not retracted by disappearing

    def test_failed_download_is_retried_next_time(self):
        self.web.set(LIST, self.listing(["a-PUB-MAS-2024-10-18"]))
        self.web.set(P["a-PUB-MAS-2024-10-18"], "oops", 503)
        r = self.ref.refresh_all()[0]
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notices").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM processed_documents").fetchone()[0], 0)
        self.web.set(P["a-PUB-MAS-2024-10-18"], fx("sr_2024_10_28.txt"), headers={"content-type": "application/pdf"})
        self.clock.advance(minutes=15)
        self.ref.refresh_all()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notices").fetchone()[0], 1)

    def test_robots_applies_to_every_pdf(self):
        self.web.set(LIST, self.listing(["a-PUB-MAS-2024-10-18"]))
        self.web.set("http://sr.test/robots.txt", "User-agent: *\nDisallow: /uploads/\n")
        r = self.ref.refresh_all()[0]
        self.assertFalse(self.downloads())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notices").fetchone()[0], 0)

    def test_listing_with_no_links_is_flagged_when_data_existed(self):
        self.web.set(LIST, self.listing(P))
        self.ref.refresh_all()
        # an active notice must exist for the safety check to matter
        with self.db:
            self.db.execute("UPDATE notices SET status='active', valid_to=NULL WHERE external_ref='PUB/MAS/2024/10/18'")
        self.web.set(LIST, "<html>site redesigned</html>")
        self.clock.advance(minutes=15)
        r = self.ref.refresh_all()[0]
        self.assertEqual(r["outcome"], "suspicious_empty")


class ConfigTests(unittest.TestCase):
    def test_unresolved_placeholder_means_not_configured(self):
        import json, tempfile
        from pathlib import Path
        from app.sources import load_sources
        f = Path(tempfile.mkdtemp()) / "s.json"
        f.write_text(json.dumps([{"source_id": "x", "name": "n", "agency": "a", "kind": "html", "url": "${NOT_SET_ANYWHERE}"}]))
        s = load_sources(f)[0]
        self.assertFalse(s["enabled"])
        self.assertTrue(s["not_configured"])

    def test_empty_env_value_means_not_configured(self):
        import json, os, tempfile
        from pathlib import Path
        from app.sources import load_sources
        f = Path(tempfile.mkdtemp()) / "s.json"
        f.write_text(json.dumps([{"source_id": "x", "name": "n", "agency": "a", "kind": "html", "url": "${EMPTY_VAR_FOR_TEST}"}]))
        os.environ["EMPTY_VAR_FOR_TEST"] = ""          # exactly what 'SR_PRESS_URL=' in .env produces
        try:
            s = load_sources(f)[0]
        finally:
            del os.environ["EMPTY_VAR_FOR_TEST"]
        self.assertTrue(s["not_configured"])
        self.assertFalse(s["enabled"])

    def test_shipped_sources_file_is_valid_and_has_no_demo_source(self):
        from app.sources import load_sources
        from app.config import BASE_DIR
        ids = [s["source_id"] for s in load_sources(BASE_DIR / "sources.json")]
        self.assertEqual(ids, ["sr-manual-import", "sr-chennai-press-archive", "cmrl-announcements"])
        self.assertFalse(any("demo" in i for i in ids))


if __name__ == "__main__":
    unittest.main()


class InspectTests(unittest.TestCase):
    def test_inspect_reports_pdfs_and_respects_robots(self):
        from app.inspect_page import inspect
        app, web, clock, sleeps = make_env()
        web.set("http://s.test/p", '<title>Press</title><a href="/a/PUB-1.pdf">x</a><div class="n"></div><div class="n"></div><div class="n"></div>')
        lines = []
        self.assertTrue(inspect("http://s.test/p", app.extensions["fetcher"], lines.append))
        text = "\n".join(lines)
        self.assertIn("PDF links found: 1", text)
        self.assertIn("div.n", text)
        web.set("http://blocked.test/robots.txt", "User-agent: *\nDisallow: /\n")
        lines.clear()
        self.assertFalse(inspect("http://blocked.test/x", app.extensions["fetcher"], lines.append))
        self.assertIn("DISALLOWS", "\n".join(lines))


class InboxAndFailureTests(unittest.TestCase):
    def setUp(self):
        self.app, self.web, self.clock, self.sleeps = make_env(kind="railway_pdf", url="http://sr.test/", extra_source={
            "source_id": "sr-manual-import", "listing": False, "use_inbox": True, "withdraw_missing": False, "parser": {},
            "pdf_urls": ["http://sr.test/uploads/gone-PUB.pdf"]})
        self.db = connect(self.app.config["DATABASE_PATH"])
        self.ref = self.app.extensions["refresher"]
        self.inbox = __import__("pathlib").Path(self.app.config["DATA_DIR"]) / "inbox"
        self.inbox.mkdir(exist_ok=True)

    def tearDown(self):
        self.db.close()

    def test_all_downloads_failing_is_a_clear_source_error_with_reason(self):
        r = self.ref.refresh_all()[0]                                  # 404 for the only PDF
        self.assertEqual(r["outcome"], "error")
        self.assertIn("HTTP 404", r["error"])
        self.assertEqual(self.client_sources()[0]["health"]["level"], "error")

    def client_sources(self):
        return self.app.test_client().get("/api/sources").get_json()

    def test_inbox_text_and_pdf_files_are_imported_once(self):
        (self.inbox / "a.txt").write_text(fx("sr_2024_10_28.txt"), encoding="utf-8")
        (self.inbox / "b.pdf").write_bytes((FIX / "sr_sample.pdf").read_bytes())      # same release as a PDF -> same notice id
        (self.inbox / "ignore.docx").write_bytes(b"x")
        r = self.ref.refresh_all()[0]
        self.assertEqual(r["outcome"], "ok")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notices").fetchone()[0], 1)    # same PUB number -> one notice
        again = self.ref.refresh_all()[0]
        self.assertEqual((again["new"], again["revised"]), (0, 0))

    def test_admin_import_upload(self):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "t"; s["is_admin"] = True
        import io
        bad = c.post("/admin/import", data={"csrf_token": "t", "file": (io.BytesIO(b"not pdf"), "x.pdf")}, content_type="multipart/form-data")
        self.assertEqual(bad.status_code, 302)
        self.assertEqual(list(self.inbox.glob("*")), [])                          # fake PDF rejected, nothing saved
        ok = c.post("/admin/import", data={"csrf_token": "t", "file": (io.BytesIO((FIX / "sr_sample.pdf").read_bytes()), "../../evil name.pdf")},
                    content_type="multipart/form-data")
        self.assertEqual(ok.status_code, 302)
        self.assertEqual([f.name for f in self.inbox.glob("*")], ["evil_name.pdf"])    # filename sanitised, no path traversal
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notices").fetchone()[0], 1)


class PasteImportTests(unittest.TestCase):
    def test_paste_text_import_and_validation(self):
        app, web, clock, sleeps = make_env(kind="railway_pdf", url="http://sr.test/", extra_source={
            "source_id": "sr-manual-import", "listing": False, "use_inbox": True, "withdraw_missing": False, "parser": {}})
        c = app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "t"; s["is_admin"] = True
        text = fx("sr_detail_2026_08_21.txt")
        short = c.post("/admin/import-text", data={"csrf_token": "t", "text": "too short"})
        self.assertEqual(short.status_code, 302)
        badurl = c.post("/admin/import-text", data={"csrf_token": "t", "text": text, "url": "javascript:alert(1)"})
        self.assertEqual(badurl.status_code, 302)
        ok = c.post("/admin/import-text", data={"csrf_token": "t", "text": text, "url": "https://sr.indianrailways.gov.in/view_detail.jsp?dcd=20378"})
        self.assertEqual(ok.status_code, 302)
        api = c.get("/api/notices?status=all").get_json()["notices"]
        self.assertEqual(len(api), 1)
        self.assertEqual(api[0]["source_url"], "https://sr.indianrailways.gov.in/view_detail.jsp?dcd=20378")   # provenance link kept
        self.assertEqual(api[0]["status"], "expired")                                                       # Aug 2026 window is over
        c.post("/admin/import-text", data={"csrf_token": "t", "text": text})                                # same text again -> no duplicate
        self.assertEqual(len(c.get("/api/notices?status=all").get_json()["notices"]), 1)
        self.assertEqual(c.post("/admin/import-text", data={"text": text}).status_code, 400)                # CSRF enforced
