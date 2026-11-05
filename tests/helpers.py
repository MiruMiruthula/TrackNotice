import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app import create_app
from app.adapters.base import Fetcher

FIX = Path(__file__).parent / "fixtures"
URL = "http://fake.test/advisories"


def fx(name):
    return (FIX / name).read_text(encoding="utf-8")


class FakeWeb:
    """Stands in for the network: a mutable dict of url -> (status, headers, body)."""
    def __init__(self):
        self.routes = {}
        self.calls = []
        self.fail_network = False

    def set(self, url, body="", status=200, headers=None):
        self.routes[url] = (status, headers or {"content-type": "text/html; charset=utf-8"}, body if isinstance(body, bytes) else body.encode())

    def __call__(self, url, headers, timeout, max_bytes):
        self.calls.append((url, dict(headers)))
        if self.fail_network and not url.endswith("robots.txt"):
            raise TimeoutError("simulated timeout")
        if url in self.routes:
            return self.routes[url]
        if url.endswith("/robots.txt"):
            return 404, {}, b""
        return 404, {}, b"not found"


class Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt

    def advance(self, **kw):
        from datetime import timedelta
        self.dt += timedelta(**kw)


def make_env(kind="html", url=URL, extra_source=None):
    tmp = Path(tempfile.mkdtemp(prefix="tn_test_"))
    src = {"source_id": "t1", "name": "Test source", "agency": "Test Agency", "kind": kind, "url": url,
           "enabled": True, "timezone": "Asia/Kolkata", "min_delay_seconds": 0}
    src.update(extra_source or {})
    (tmp / "sources.json").write_text(json.dumps([src]))
    web = FakeWeb()
    sleeps = []
    fetcher = Fetcher("TransitNoticeBot/test", max_retries=3, backoff_base=1.0, http_get=web, sleep=sleeps.append)
    clock = Clock(datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc))      # 15:30 IST
    app = create_app({"TESTING": True, "DATABASE_PATH": str(tmp / "t.db"), "DATA_DIR": str(tmp), "LOG_DIR": str(tmp),
                      "SOURCES_FILE": str(tmp / "sources.json"), "SECRET_KEY": "s3cr3t-key-for-tests-only", "ADMIN_TOKEN": "adm-token-xyz",
                      "STALE_AFTER_MINUTES": 31, "DEMO_MODE": True},
                     fetcher=fetcher, now_fn=clock, start_scheduler=False)
    return app, web, clock, sleeps
