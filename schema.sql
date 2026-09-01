-- SQLite schema. Applied by run.py --setup.

DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS blacklist;
DROP TABLE IF EXISTS cameras;

CREATE TABLE cameras (
  id       TEXT PRIMARY KEY,
  name     TEXT NOT NULL,
  lat      REAL NOT NULL,
  lng      REAL NOT NULL,
  is_live  INTEGER DEFAULT 0
);

CREATE TABLE events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  plate_raw   TEXT NOT NULL,      -- exactly what OCR said, misreads and all
  plate_norm  TEXT NOT NULL,      -- stripped + uppercased
  plate_len   INTEGER NOT NULL,   -- indexed pre-filter for fuzzy search
  confidence  REAL,
  camera_id   TEXT REFERENCES cameras(id),
  ts          REAL NOT NULL,      -- unix epoch seconds
  synthetic   INTEGER DEFAULT 0   -- honest flag: real feed vs load replay
);

CREATE INDEX idx_events_norm ON events (plate_norm);
CREATE INDEX idx_events_ts   ON events (ts DESC);
CREATE INDEX idx_events_len  ON events (plate_len, ts DESC);
CREATE INDEX idx_events_cam  ON events (camera_id, ts DESC);

CREATE TABLE blacklist (
  plate_norm TEXT PRIMARY KEY,
  reason     TEXT,
  added_at   REAL DEFAULT (strftime('%s','now'))
);
