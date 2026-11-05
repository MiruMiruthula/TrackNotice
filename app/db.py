import sqlite3
from pathlib import Path

from .timeutil import to_iso, utcnow


def connect(path):
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


def migrate(conn, migrations_dir):
    """Apply every NNN_*.sql file exactly once, in order (re-running is a no-op)."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                 "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.commit()
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    ran = []
    for f in sorted(Path(migrations_dir).glob("*.sql")):
        if f.name in applied:
            continue
        conn.executescript(f.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?,?)",
                     (f.name, to_iso(utcnow())))
        conn.commit()
        ran.append(f.name)
    return ran


def sync_sources(conn, sources):
    """Mirror sources.json into the sources table (idempotent; keeps health/state columns)."""
    with conn:
        for s in sources:
            conn.execute(
                """INSERT INTO sources(source_id,name,agency,url,kind,enabled,timezone)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET name=excluded.name, agency=excluded.agency,
                   url=excluded.url, kind=excluded.kind, enabled=excluded.enabled, timezone=excluded.timezone""",
                (s["source_id"], s["name"], s["agency"], s["url"], s["kind"],
                 1 if s.get("enabled", True) else 0, s.get("timezone", "Asia/Kolkata")))
