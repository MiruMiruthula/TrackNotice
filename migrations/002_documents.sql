-- Document-style sources (PDF press releases): remember which documents were already processed so each is
-- downloaded and parsed exactly once (polite + idempotent).
CREATE TABLE processed_documents (
    source_id    TEXT NOT NULL REFERENCES sources(source_id),
    url          TEXT NOT NULL,
    ref          TEXT,
    outcome      TEXT NOT NULL,          -- notice | withdrawal | non_service | unparseable
    processed_at TEXT NOT NULL,
    PRIMARY KEY (source_id, url)
);
