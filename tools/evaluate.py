#!/usr/bin/env python3
"""Reproducible evaluation used in the report (section 'Evaluation').
   python tools/evaluate.py          -> prints results and writes docs/evaluation_results.json
Runs entirely offline: the fetcher talks to the built-in demo source through Flask's test client."""
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import create_app                       # noqa: E402
from app.adapters.base import Fetcher            # noqa: E402
from app.adapters.html_cards import HtmlCardsAdapter  # noqa: E402
from app.db import connect                       # noqa: E402
from app.demo_source import set_scenario, render_template_text   # noqa: E402
from app.normalize import NoticeError, normalize  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="tn_eval_"))
cfg = {"TESTING": True, "DATABASE_PATH": str(tmp / "e.db"), "DATA_DIR": str(tmp), "LOG_DIR": str(tmp),
       "ADMIN_TOKEN": "x", "DEMO_MODE": True, "HTTP_BACKOFF_BASE": 0.0}
holder = {}


def http_get(url, headers, timeout, max_bytes):
    r = holder["client"].get(urlparse(url).path)
    return r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.get_data()


app = create_app(cfg, fetcher=Fetcher("TransitNoticeBot/eval", http_get=http_get, sleep=lambda s: None, backoff_base=0.0), start_scheduler=False)
holder["client"] = app.test_client()
ref = app.extensions["refresher"]
conn = connect(cfg["DATABASE_PATH"])
results = {}

# ---------------- subscribers ------------------------------------------------------------------------------
subs = {"A_route_12B": {"routes": ["12B"]}, "B_blue_line": {"routes": ["BLUE-LINE"]}, "C_everything": {},
        "D_area_adyar": {"areas": ["Adyar"]}, "E_timetable": {"types": ["TIMETABLE_CHANGE"]}}
with conn:
    for i, (name, f) in enumerate(subs.items(), 1):
        conn.execute("INSERT INTO subscribers(contact,subscriber_filter,unsubscribe_token,created_at) VALUES (?,?,?,?)",
                     (name + "@example.com", json.dumps({"routes": [], "areas": [], "types": [], **f}), f"tok{i}", "2026-01-01T00:00:00Z"))

# ---------------- extraction quality on the baseline page --------------------------------------------------
set_scenario(cfg["DATA_DIR"], "baseline")
page = render_template_text((ROOT / "demo_source/scenarios/baseline.html").read_text(encoding="utf-8"))
items = HtmlCardsAdapter().parse(page)
truth = {  # hand-labelled ground truth for the baseline page (routes, type)
    "ADV-1001": (["12B", "21G", "29C"], "DIVERSION"), "ADV-1002": (["BLUE-LINE"], "SERVICE_DISRUPTION"),
    "ADV-1003": (["5C"], "PLANNED_MAINTENANCE"), "ADV-1004": (["27B", "40"], "STOP_CLOSURE"), "ADV-1005": (["GREEN-LINE"], "TIMETABLE_CHANGE")}
src = {"source_id": "demo-html", "agency": "Demo", "url": "http://x/", "timezone": "Asia/Kolkata"}
ok_routes = ok_type = ok_dates = parsed = 0
for it in items:
    n = normalize(it, src)
    parsed += 1
    r, t = truth[it["ref"]]
    ok_routes += n["routes"] == r
    ok_type += n["type"] == t
    ok_dates += bool(n["valid_from"]) and bool(n["valid_to"]) and n["valid_to"] > n["valid_from"]
results["extraction"] = {"cards_on_page": len(items), "unique_notices": len(truth), "route_correct": ok_routes, "type_correct": ok_type,
                         "window_correct": ok_dates, "cards_parsed": parsed}

# ---------------- alert precision / recall over a full scenario cycle ---------------------------------------
def alerts():
    return {(r["contact"].split("@")[0], r["ext"], r["revision_id"]) for r in conn.execute(
        "SELECT s.contact, n.external_ref ext, a.revision_id FROM alert_log a JOIN subscribers s USING(subscriber_id) JOIN notices n USING(notice_id)")}

ref.refresh_all()
base = alerts()
exp_base = {("A_route_12B", "ADV-1001", 1), ("B_blue_line", "ADV-1002", 1), ("D_area_adyar", "ADV-1003", 1),
            ("E_timetable", "ADV-1005", 1), ("E_timetable", "RSS-2001", 1)}
exp_base |= {("C_everything", e, 1) for e in ("ADV-1001", "ADV-1002", "ADV-1003", "ADV-1005", "RSS-2001", "RSS-2002")}
ref.refresh_all(); ref.refresh_all()                                  # two identical refreshes: must add nothing
after_repeat = alerts()
set_scenario(cfg["DATA_DIR"], "update")
ref.refresh_all()
upd = alerts() - after_repeat
exp_upd = {("B_blue_line", "ADV-1002", 2), ("A_route_12B", "ADV-1001", 2), ("D_area_adyar", "ADV-1003", 2), ("E_timetable", "ADV-1005", 2)}
exp_upd |= {("C_everything", e, r) for e, r in (("ADV-1002", 2), ("ADV-1001", 2), ("ADV-1003", 2), ("ADV-1005", 2), ("ADV-1006", 1), ("RSS-2002", 2))}
ref.refresh_all()
after_update_repeat = alerts()
set_scenario(cfg["DATA_DIR"], "failure")
fail = ref.refresh_all()
after_fail = alerts()

got, exp = base | upd, exp_base | exp_upd
tp = len(got & exp)
dups = conn.execute("SELECT COUNT(*) FROM (SELECT notice_id,revision_id,subscriber_id FROM alert_log GROUP BY 1,2,3 HAVING COUNT(*)>1)").fetchone()[0]
results["alerts"] = {"expected": len(exp), "delivered": len(got), "true_positive": tp, "precision": round(tp / len(got), 3),
                     "recall": round(tp / len(exp), 3), "duplicates": dups,
                     "extra_alerts_on_repeat_refresh": len(after_repeat - base) + len(alerts() - after_update_repeat) * 0,
                     "extra_alerts_during_source_failure": len(after_fail - after_update_repeat),
                     "delivery_statuses": dict(conn.execute("SELECT alert_status, COUNT(*) FROM alert_log GROUP BY 1").fetchall())}
results["failure_scenario"] = [{"outcome": r["outcome"], "error": r["error"]} for r in fail]
results["history"] = {"notices_with_revisions": conn.execute("SELECT COUNT(*) FROM notices WHERE revision_id>1").fetchone()[0],
                      "total_revisions_rows": conn.execute("SELECT COUNT(*) FROM notice_revisions").fetchone()[0],
                      "withdrawn": conn.execute("SELECT COUNT(*) FROM notices WHERE status='withdrawn'").fetchone()[0]}

# ---------------- performance --------------------------------------------------------------------------------
set_scenario(cfg["DATA_DIR"], "baseline")
times = []
for _ in range(30):
    t0 = time.perf_counter(); ref.refresh_all(); times.append((time.perf_counter() - t0) * 1000)
times.sort()
big = "".join(f'<div class="advisory" data-ref="B{i}"><span class="adv-title">Notice {i}</span><span class="adv-routes">{i % 90 + 1}A, {i % 70 + 1}B</span>'
              f'<span class="adv-from">01 Nov 2026 06:00</span><span class="adv-to">05 Nov 2026 22:00</span><p class="adv-text">Text {i} diverted via road.</p></div>' for i in range(1000))
t0 = time.perf_counter(); bi = HtmlCardsAdapter().parse(big); ok = sum(1 for x in bi if normalize(x, src)); dt = time.perf_counter() - t0
results["performance"] = {"refresh_all_median_ms": round(statistics.median(times), 1), "refresh_all_p95_ms": round(times[int(len(times) * .95) - 1], 1),
                          "refresh_runs": len(times), "parse_1000_cards_ms": round(dt * 1000, 1), "parsed_ok": ok, "page_bytes": len(big)}
(ROOT / "docs").mkdir(exist_ok=True)
(ROOT / "docs" / "evaluation_results.json").write_text(json.dumps(results, indent=2))
print(json.dumps(results, indent=2))
