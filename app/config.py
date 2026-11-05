"""Configuration. Secrets come from environment variables / .env, never from source code."""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path):
    """Tiny .env reader (KEY=VALUE, '#' comments) so no extra dependency is needed."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split("  #")[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _bool(v, default=False):
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_config(overrides=None):
    load_dotenv(BASE_DIR / ".env")
    e = os.environ
    interval = int(e.get("REFRESH_INTERVAL_MINUTES", "15"))
    cfg = {
        "DATABASE_PATH": e.get("DATABASE_PATH", str(BASE_DIR / "data" / "transitnotice.db")),
        "SOURCES_FILE": e.get("SOURCES_FILE", str(BASE_DIR / "sources.json")),
        "MIGRATIONS_DIR": str(BASE_DIR / "migrations"),
        "DATA_DIR": e.get("DATA_DIR", str(BASE_DIR / "data")),
        "LOG_DIR": e.get("LOG_DIR", str(BASE_DIR / "logs")),
        "SECRET_KEY": e.get("SECRET_KEY") or secrets.token_hex(32),
        "ADMIN_TOKEN": e.get("ADMIN_TOKEN") or "",
        "REFRESH_INTERVAL_MINUTES": interval,
        "STALE_AFTER_MINUTES": int(e.get("STALE_AFTER_MINUTES", str(interval * 2 + 1))),
        "SCHEDULER_ENABLED": _bool(e.get("SCHEDULER_ENABLED"), True),
        "DEMO_MODE": _bool(e.get("DEMO_MODE"), False),
        "DEMO_BASE_URL": e.get("DEMO_BASE_URL", "http://127.0.0.1:5000"),
        "DISPLAY_TZ": e.get("DISPLAY_TZ", "Asia/Kolkata"),
        "ALERT_MODE": e.get("ALERT_MODE", "demo"),
        "ALERT_MAX_ATTEMPTS": int(e.get("ALERT_MAX_ATTEMPTS", "3")),
        "SMTP_HOST": e.get("SMTP_HOST", ""),
        "SMTP_PORT": int(e.get("SMTP_PORT", "587")),
        "SMTP_USER": e.get("SMTP_USER", ""),
        "SMTP_PASSWORD": e.get("SMTP_PASSWORD", ""),
        "SMTP_FROM": e.get("SMTP_FROM", "alerts@transitnotice.local"),
        "BOT_CONTACT": e.get("BOT_CONTACT", "contact-not-set"),
        "HTTP_TIMEOUT": float(e.get("HTTP_TIMEOUT", "10")),
        "HTTP_MAX_RETRIES": int(e.get("HTTP_MAX_RETRIES", "3")),
        "HTTP_BACKOFF_BASE": float(e.get("HTTP_BACKOFF_BASE", "1.0")),
        "HTTP_MAX_BYTES": int(e.get("HTTP_MAX_BYTES", "2000000")),
        "PAGE_SIZE": 25,
    }
    if overrides:
        cfg.update(overrides)
    return cfg
