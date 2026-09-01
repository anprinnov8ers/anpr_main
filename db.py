"""
SQLite storage layer. No Docker, no server to install.

Everything writes through a single connection owned by the API's drain task,
so we never hit SQLite's single-writer limit. WAL mode lets readers run
concurrently with that writer.
"""

import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("ANPR_DB", os.path.join(HERE, "anpr.db"))


def connect(path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # concurrent readers
    conn.execute("PRAGMA synchronous=NORMAL")    # fast enough, still crash-safe
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def rows(conn, sql, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def init_schema(conn):
    with open(os.path.join(HERE, "schema.sql")) as f:
        conn.executescript(f.read())
    conn.commit()
