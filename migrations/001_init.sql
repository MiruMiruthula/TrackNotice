-- TransitNotice schema, migration 001
CREATE TABLE sources (
    source_id            TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    agency               TEXT NOT NULL,
    url                  TEXT NOT NULL,
    kind                 TEXT NOT NULL CHECK (kind IN ('html','rss','railway_pdf')),
    enabled              INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    timezone             TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    etag                 TEXT,
    last_modified        TEXT,
    last_attempt_at      TEXT,
    last_success_at      TEXT,
    last_error           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE notices (
    notice_id         TEXT PRIMARY KEY,
    source_id         TEXT NOT NULL REFERENCES sources(source_id),
    external_ref      TEXT,
    agency            TEXT NOT NULL,
    route_id          TEXT NOT NULL,                 -- comma separated display copy; filtering uses notice_routes
    area              TEXT,
    source_url        TEXT NOT NULL,
    type              TEXT NOT NULL CHECK (type IN
                      ('DIVERSION','SERVICE_DISRUPTION','PLANNED_MAINTENANCE',
                       'TIMETABLE_CHANGE','STOP_CLOSURE','CANCELLATION','OTHER')),
    title             TEXT NOT NULL,
    summary           TEXT NOT NULL,
    published_at      TEXT,                          -- UTC ISO-8601, NULL when the source does not publish it
    valid_from        TEXT NOT NULL,                 -- UTC ISO-8601
    valid_to          TEXT,                          -- NULL = end not announced (open-ended)
    status            TEXT NOT NULL CHECK (status IN ('active','scheduled','expired','withdrawn')),
    observed_at       TEXT NOT NULL,                 -- first time we saw it
    last_confirmed_at TEXT NOT NULL,                 -- last successful fetch in which it was listed
    content_hash      TEXT NOT NULL,
    revision_id       INTEGER NOT NULL DEFAULT 1,
    change_summary    TEXT NOT NULL DEFAULT 'New advisory',
    alert_status      TEXT NOT NULL DEFAULT 'none'
                      CHECK (alert_status IN ('none','no_subscribers','pending','delivered','demo_logged','failed')),
    alerted_at        TEXT,
    updated_at        TEXT NOT NULL,
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
CREATE INDEX idx_notices_status   ON notices(status);
CREATE INDEX idx_notices_window   ON notices(valid_from, valid_to);
CREATE INDEX idx_notices_source   ON notices(source_id);
CREATE INDEX idx_notices_updated  ON notices(updated_at);

CREATE TABLE notice_routes (
    notice_id TEXT NOT NULL REFERENCES notices(notice_id) ON DELETE CASCADE,
    route_id  TEXT NOT NULL,
    PRIMARY KEY (notice_id, route_id)
);
CREATE INDEX idx_notice_routes_route ON notice_routes(route_id);

CREATE TABLE notice_stations (
    notice_id TEXT NOT NULL REFERENCES notices(notice_id) ON DELETE CASCADE,
    station   TEXT NOT NULL,
    PRIMARY KEY (notice_id, station)
);

CREATE TABLE notice_revisions (
    notice_id      TEXT NOT NULL REFERENCES notices(notice_id) ON DELETE CASCADE,
    revision_id    INTEGER NOT NULL,
    content_hash   TEXT NOT NULL,
    snapshot_json  TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    PRIMARY KEY (notice_id, revision_id)
);

CREATE TABLE subscribers (
    subscriber_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    contact           TEXT NOT NULL,                 -- e-mail only (data minimisation)
    subscriber_filter TEXT NOT NULL DEFAULT '{}',    -- JSON: {"routes":[],"areas":[],"types":[]}
    unsubscribe_token TEXT NOT NULL UNIQUE,
    active            INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at        TEXT NOT NULL,
    UNIQUE (contact, subscriber_filter)
);

CREATE TABLE alert_log (
    alert_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id     TEXT NOT NULL REFERENCES notices(notice_id) ON DELETE CASCADE,
    revision_id   INTEGER NOT NULL,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(subscriber_id) ON DELETE CASCADE,
    alert_status  TEXT NOT NULL DEFAULT 'pending'
                  CHECK (alert_status IN ('pending','sent','demo_logged','failed')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    alerted_at    TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (notice_id, revision_id, subscriber_id)   -- one alert per revision per subscriber
);

CREATE TABLE refresh_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL,
    duration_ms  INTEGER NOT NULL,
    outcome      TEXT NOT NULL,                      -- ok | not_modified | error | suspicious_empty
    found        INTEGER NOT NULL DEFAULT 0,
    new_count    INTEGER NOT NULL DEFAULT 0,
    revised      INTEGER NOT NULL DEFAULT 0,
    withdrawn    INTEGER NOT NULL DEFAULT 0,
    rejected     INTEGER NOT NULL DEFAULT 0,
    duplicates   INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    detail       TEXT
);
CREATE INDEX idx_refresh_log_source ON refresh_log(source_id, id DESC);
