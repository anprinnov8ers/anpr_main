"""
Rashtriya ANPR Grid — Traffic Police Control Room (TPCR) backend
==================================================================

    python -m uvicorn api:app --port 8000

One process: ingestion, automation, and the TPCR dashboard API.

DESIGN, UNCHANGED FROM THE ORIGINAL AND WORTH KEEPING
-------------------------------------------------------
Camera workers POST reads to /api/ingest. That endpoint only enqueues and
returns — it never touches the database on the request path. A background
task (`drain`) batches, normalises, and writes. That decoupling is why
camera load can spike without stalling writes; it was true before this
redesign and it's still true now.

WHAT CHANGED
------------
1. The watchlist check that used to just flag a fuzzy-matched plate now
   classifies *why* it matched (stolen / FIR / blacklist / NCRB) and scores
   a priority, so P1 alerts (stolen vehicle) reach TPCR before a P3
   (expired-permit) notice does. This is the "smart automation" ask: the
   system decides urgency instead of dumping every match in one queue.
2. Speed-based e-Challan generation is a first-class feature, not a side
   effect of the trajectory endpoint. Two consecutive reads of the same
   plate on cameras with a known corridor distance and posted limit auto-
   generate a Motor Vehicles Act challan — this is what a real state
   traffic department control room actually wants from an ANPR grid.
3. Habitual-offender detection: N challans/alerts for the same plate inside
   a rolling window auto-flags the vehicle for manual TPCR review, instead
   of silently accumulating rows nobody looks at.
4. Code is organised by concern (config / domain enums / automation
   services / routers) instead of one flat script, so each piece can be
   tested and swapped independently — e.g. `db.py` can move from SQLite to
   PostGIS+TimescaleDB (per the original architecture doc) without
   touching the automation logic below.

WHAT DELIBERATELY DIDN'T CHANGE
--------------------------------
`db` and `normalise` are treated as existing modules with the same
interface as before (`db.connect()`, `db.rows(conn, sql, params)`,
`normalise()`, `is_plausible()`, `fuzzy_match()`, `match_score()`). New
tables this file assumes are documented in SCHEMA_ADDITIONS below rather
than guessed into a db.py rewrite, since that file wasn't provided.
"""

import time
import math
import asyncio
import contextlib
import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

import db
from normalise import normalise, is_plausible, fuzzy_match, match_score

log = logging.getLogger("tpcr")

# ---------------------------------------------------------------- schema
SCHEMA_ADDITIONS = """
-- Run once against the existing database. Additive only; nothing above
-- this file's original tables (events, cameras, blacklist) needs to change.

ALTER TABLE cameras ADD COLUMN jurisdiction TEXT;        -- e.g. "PS Rajpur Road"
ALTER TABLE cameras ADD COLUMN corridor_next TEXT;        -- next camera id downstream
ALTER TABLE cameras ADD COLUMN corridor_km REAL;          -- distance to corridor_next
ALTER TABLE cameras ADD COLUMN speed_limit_kmh REAL DEFAULT 60;

ALTER TABLE blacklist ADD COLUMN category TEXT DEFAULT 'FLAGGED';  -- STOLEN | FIR | NCRB | FLAGGED

CREATE TABLE IF NOT EXISTS challans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_norm TEXT, camera_id TEXT, ts REAL,
    kmh REAL, limit_kmh REAL, fine_inr INTEGER,
    section TEXT DEFAULT 'MVA Sec.183', status TEXT DEFAULT 'PENDING_REVIEW'
);

CREATE TABLE IF NOT EXISTS offender_flags (
    plate_norm TEXT PRIMARY KEY, incident_count INTEGER, first_seen REAL,
    last_seen REAL, status TEXT DEFAULT 'OPEN'
);
"""

# ---------------------------------------------------------------- config
IMPLAUSIBLE_KMH = 120.0          # above this, treat the leg as a probable plate mis-read, not a real trip
DEFAULT_SPEED_LIMIT_KMH = 60.0   # fallback when a camera pair has no configured corridor limit
HABITUAL_OFFENDER_THRESHOLD = 3  # incidents inside the rolling window below
HABITUAL_OFFENDER_WINDOW_S = 30 * 24 * 3600   # 30 days
FINE_BASE_INR = 1000
FINE_PER_KMH_OVER_INR = 100
QUEUE_MAX = 200_000


class AlertPriority(IntEnum):
    """Lower number = more urgent. Ordering matters: TPCR triages by this."""
    P1_CRITICAL = 1   # stolen vehicle / active FIR
    P2_HIGH = 2        # NCRB watchlist
    P3_STANDARD = 3    # locally flagged plate


WATCHLIST_PRIORITY = {
    "STOLEN": AlertPriority.P1_CRITICAL,
    "FIR": AlertPriority.P1_CRITICAL,
    "NCRB": AlertPriority.P2_HIGH,
    "FLAGGED": AlertPriority.P3_STANDARD,
}

app = FastAPI(title="Rashtriya ANPR Grid — TPCR Backend")

conn = db.connect()
queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
clients: set[WebSocket] = set()

_watchlist: dict[str, dict] = {}     # plate_norm -> {reason, category}
_camera_index: dict[str, dict] = {}  # camera_id -> {jurisdiction, corridor_next, corridor_km, speed_limit_kmh}
_offender_window: dict[str, list[float]] = {}   # plate_norm -> recent incident timestamps (in-memory hot cache)
_stats = {"ingested": 0, "dropped": 0, "challans_issued": 0, "window": []}


# ---------------------------------------------------------------- helpers
def haversine_km(lat1, lng1, lat2, lng2) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def refresh_watchlist():
    """Pull STOLEN / FIR / NCRB / FLAGGED entries. Runs on a 2s timer plus
    immediately after any manual add/remove, so TPCR edits take effect
    without waiting for the timer."""
    global _watchlist
    _watchlist = {
        r["plate_norm"]: {"reason": r["reason"], "category": r.get("category", "FLAGGED")}
        for r in db.rows(conn, "SELECT plate_norm, reason, category FROM blacklist")
    }


def refresh_camera_index():
    global _camera_index
    _camera_index = {
        r["id"]: r for r in db.rows(
            conn,
            "SELECT id, jurisdiction, corridor_next, corridor_km, speed_limit_kmh FROM cameras",
        )
    }


async def broadcast(msg: dict):
    dead = []
    for ws in list(clients):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


# ---------------------------------------------------------------- smart automation
def score_watchlist_hit(norm: str) -> Optional[dict]:
    """OCR-noise-tolerant fuzzy match against the watchlist, returning a
    priority-scored hit or None. This is the "smart" part of the alert
    pipeline: a stolen-vehicle match and an expired-permit match no longer
    look identical to the operator on the other end."""
    best = None
    for plate, info in _watchlist.items():
        if fuzzy_match(norm, plate):
            category = info["category"]
            priority = WATCHLIST_PRIORITY.get(category, AlertPriority.P3_STANDARD)
            hit = {"matched": plate, "reason": info["reason"], "category": category,
                   "priority": int(priority), "score": round(match_score(norm, plate), 3)}
            if best is None or hit["priority"] < best["priority"]:
                best = hit
    return best


def record_incident_and_check_habitual(plate_norm: str, now: float) -> bool:
    """Track incidents (alerts + challans) per plate in a rolling window and
    auto-flag habitual offenders for TPCR review instead of leaving the
    pattern buried across individual rows. Returns True if this incident
    just crossed the threshold."""
    window = _offender_window.setdefault(plate_norm, [])
    window.append(now)
    cutoff = now - HABITUAL_OFFENDER_WINDOW_S
    window[:] = [t for t in window if t > cutoff]

    if len(window) < HABITUAL_OFFENDER_THRESHOLD:
        return False

    conn.execute(
        """INSERT INTO offender_flags (plate_norm, incident_count, first_seen, last_seen, status)
           VALUES (?,?,?,?, 'OPEN')
           ON CONFLICT(plate_norm) DO UPDATE SET
             incident_count = excluded.incident_count,
             last_seen = excluded.last_seen,
             status = CASE WHEN offender_flags.status = 'DISMISSED' THEN 'DISMISSED' ELSE 'OPEN' END""",
        (plate_norm, len(window), window[0], now),
    )
    return True


@dataclass
class SpeedCheck:
    kmh: float
    limit_kmh: float
    over_limit: bool
    fine_inr: int


def check_speed_violation(prev_cam: str, kmh: float) -> Optional[SpeedCheck]:
    """Compare a computed leg speed against the corridor's posted limit and
    auto-price a Motor Vehicles Act fine. Kept separate from the plausibility
    check in `drain`/`trajectory`: implausible (>120km/h) means "bad OCR
    match, discard the leg"; a *plausible* speed above the posted limit
    means "real violation, issue a challan"."""
    limit = _camera_index.get(prev_cam, {}).get("speed_limit_kmh") or DEFAULT_SPEED_LIMIT_KMH
    if kmh <= limit or kmh > IMPLAUSIBLE_KMH:
        return None
    fine = FINE_BASE_INR + int((kmh - limit) * FINE_PER_KMH_OVER_INR)
    return SpeedCheck(kmh=kmh, limit_kmh=limit, over_limit=True, fine_inr=fine)


def issue_challan(plate_norm: str, camera_id: str, ts: float, check: SpeedCheck):
    conn.execute(
        """INSERT INTO challans (plate_norm, camera_id, ts, kmh, limit_kmh, fine_inr)
           VALUES (?,?,?,?,?,?)""",
        (plate_norm, camera_id, ts, check.kmh, check.limit_kmh, check.fine_inr),
    )
    _stats["challans_issued"] += 1
    record_incident_and_check_habitual(plate_norm, ts)


# ---------------------------------------------------------------- drain task
async def drain():
    """Ingestion path. Batches writes so the store keeps up under load.
    Watchlist scoring happens here, on ingest — not as a batch job over the
    events table — so a stolen-vehicle hit reaches TPCR within the batch
    window, not on the next analytics run."""
    _last_seen: dict[str, tuple[str, float, float, float]] = {}  # plate -> (camera_id, ts, lat, lng), hot cache for speed checks

    while True:
        batch = []
        try:
            batch.append(await asyncio.wait_for(queue.get(), timeout=0.5))
        except asyncio.TimeoutError:
            continue

        while len(batch) < 1000:
            try:
                batch.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        rows, alerts, live = [], [], []
        for ev in batch:
            norm = normalise(ev["plate_raw"])
            if not is_plausible(norm):
                _stats["dropped"] += 1
                continue

            rows.append((ev["plate_raw"], norm, len(norm), ev.get("confidence"),
                         ev["camera_id"], ev["ts"], int(ev.get("synthetic", False))))

            payload = {"type": "event", "plate_raw": ev["plate_raw"],
                       "plate_norm": norm, "confidence": ev.get("confidence"),
                       "camera_id": ev["camera_id"], "ts": ev["ts"]}

            if not ev.get("synthetic"):
                live.append(payload)

            hit = score_watchlist_hit(norm)
            if hit:
                jurisdiction = _camera_index.get(ev["camera_id"], {}).get("jurisdiction", "Unassigned PS")
                alerts.append({**payload, "type": "alert", "jurisdiction": jurisdiction, **hit})
                record_incident_and_check_habitual(norm, ev["ts"])

            # --- corridor speed check against the previous sighting ---
            prev = _last_seen.get(norm)
            if prev:
                prev_cam, prev_ts, prev_lat, prev_lng = prev
                cam_info = _camera_index.get(prev_cam)
                if cam_info and cam_info.get("corridor_next") == ev["camera_id"] and cam_info.get("corridor_km"):
                    secs = ev["ts"] - prev_ts
                    kmh = (cam_info["corridor_km"] / secs * 3600) if secs > 0 else 0.0
                    check = check_speed_violation(prev_cam, kmh)
                    if check:
                        issue_challan(norm, prev_cam, ev["ts"], check)
                        alerts.append({**payload, "type": "challan", "camera_id": prev_cam,
                                       "kmh": check.kmh, "limit_kmh": check.limit_kmh,
                                       "fine_inr": check.fine_inr})
            _last_seen[norm] = (ev["camera_id"], ev["ts"], None, None)

        if rows:
            conn.executemany(
                """INSERT INTO events (plate_raw, plate_norm, plate_len,
                   confidence, camera_id, ts, synthetic)
                   VALUES (?,?,?,?,?,?,?)""", rows)
            conn.commit()
            _stats["ingested"] += len(rows)
            _stats["window"].append((time.time(), len(rows)))

        for m in live[:40]:
            await broadcast(m)
        for m in alerts:
            await broadcast(m)


async def watchlist_and_camera_refresher():
    while True:
        refresh_watchlist()
        refresh_camera_index()
        await asyncio.sleep(2)


@app.on_event("startup")
async def startup():
    for stmt in SCHEMA_ADDITIONS.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            with contextlib.suppress(Exception):
                conn.execute(stmt)
    conn.commit()
    refresh_watchlist()
    refresh_camera_index()
    app.state.tasks = [asyncio.create_task(drain()),
                       asyncio.create_task(watchlist_and_camera_refresher())]


@app.on_event("shutdown")
async def shutdown():
    for t in app.state.tasks:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t


# ---------------------------------------------------------------- ingest
class Read(BaseModel):
    plate_raw: str
    camera_id: str
    ts: float
    confidence: Optional[float] = None
    synthetic: bool = False


class Batch(BaseModel):
    reads: list[Read]


@app.post("/api/ingest")
async def ingest(batch: Batch):
    """Enqueue and return. Never touches the DB on the request path."""
    accepted = 0
    for r in batch.reads:
        try:
            queue.put_nowait(r.model_dump())
            accepted += 1
        except asyncio.QueueFull:
            _stats["dropped"] += 1
    return {"accepted": accepted, "queue_depth": queue.qsize()}


# ---------------------------------------------------------------- reads
@app.get("/api/cameras")
def cameras():
    return db.rows(conn, """SELECT id, name, lat, lng, is_live, jurisdiction,
                             speed_limit_kmh FROM cameras ORDER BY id""")


@app.get("/api/events/recent")
def recent(limit: int = 50):
    return db.rows(conn, """
        SELECT e.id, e.plate_raw, e.plate_norm, e.confidence, e.ts,
               e.camera_id, c.name AS camera_name, c.jurisdiction
        FROM events e JOIN cameras c ON c.id = e.camera_id
        WHERE e.synthetic = 0
        ORDER BY e.ts DESC LIMIT ?""", (limit,))


@app.get("/api/stats")
def stats():
    now = time.time()
    _stats["window"] = [(t, n) for t, n in _stats["window"] if now - t < 10]
    per_sec = sum(n for _, n in _stats["window"]) / 10.0

    r = db.rows(conn, """
        SELECT (SELECT count(*) FROM events)                     AS total_events,
               (SELECT count(DISTINCT plate_norm) FROM events)   AS unique_plates,
               (SELECT count(*) FROM cameras)                    AS cameras,
               (SELECT count(*) FROM blacklist)                  AS watchlisted,
               (SELECT count(*) FROM challans)                   AS challans_total,
               (SELECT count(*) FROM offender_flags WHERE status='OPEN') AS open_offender_flags""")[0]
    r["events_per_sec"] = round(per_sec, 1)
    r["queue_depth"] = queue.qsize()
    return r


# ---------------------------------------------------------------- trajectory
@app.get("/api/trajectory")
def trajectory(plate: str, hours: int = 24, max_dist: int = 2):
    """
    Two-stage search, unchanged from the original because it already scales:
      1. SQL pre-filter on the indexed plate_len column + time window.
      2. Fuzzy match in canonical space on that shortlist only.
    Edit distance never runs over the full table.
    """
    q = normalise(plate)
    if len(q) < 6:
        return {"error": "Enter at least 6 characters of a plate."}

    cutoff = time.time() - hours * 3600
    candidates = db.rows(conn, """
        SELECT e.id, e.plate_raw, e.plate_norm, e.confidence, e.ts,
               e.camera_id, c.name AS camera_name, c.jurisdiction, c.lat, c.lng
        FROM events e JOIN cameras c ON c.id = e.camera_id
        WHERE e.ts > ? AND e.plate_len = ?
        ORDER BY e.ts ASC""", (cutoff, len(q)))

    stops = []
    for c in candidates:
        if fuzzy_match(q, c["plate_norm"], max_dist):
            c["match_score"] = round(match_score(q, c["plate_norm"]), 3)
            c["exact"] = c["plate_norm"] == q
            stops.append(c)

    total_km = 0.0
    for i in range(1, len(stops)):
        a, b = stops[i - 1], stops[i]
        km = haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])
        secs = b["ts"] - a["ts"]
        kmh = (km / secs * 3600) if secs > 0 else 0.0
        b["leg_km"], b["leg_kmh"] = round(km, 2), round(kmh, 1)
        b["implausible"] = kmh > IMPLAUSIBLE_KMH
        total_km += km

    corrected = sum(1 for s in stops if not s["exact"])
    offender = db.rows(conn, "SELECT incident_count, status FROM offender_flags WHERE plate_norm = ?", (q,))
    return {"query": q, "stops": stops, "stop_count": len(stops),
            "total_km": round(total_km, 2), "corrected_stops": corrected,
            "exact_match_would_have_found": len(stops) - corrected,
            "offender_flag": offender[0] if offender else None}


# ---------------------------------------------------------------- analytics
@app.get("/api/analytics/heatmap")
def heatmap(hours: int = 24):
    return db.rows(conn, """
        SELECT c.id, c.name, c.lat, c.lng, c.jurisdiction,
               (SELECT count(*) FROM events e
                WHERE e.camera_id = c.id AND e.ts > ?) AS reads
        FROM cameras c ORDER BY reads DESC""", (time.time() - hours * 3600,))


@app.get("/api/analytics/density")
def density(hours: int = 6):
    return db.rows(conn, """
        SELECT CAST(ts / 300 AS INTEGER) * 300 AS bucket, count(*) AS reads
        FROM events WHERE ts > ?
        GROUP BY bucket ORDER BY bucket""", (time.time() - hours * 3600,))


@app.get("/api/analytics/od")
def origin_destination(hours: int = 24, limit: int = 10):
    """Top origin-destination pairs from consecutive reads of the same plate."""
    return db.rows(conn, """
        WITH hops AS (
          SELECT plate_norm, camera_id,
                 lead(camera_id) OVER (PARTITION BY plate_norm ORDER BY ts) AS next_cam
          FROM events WHERE ts > ?
        )
        SELECT h.camera_id AS origin, co.name AS origin_name,
               h.next_cam AS dest, cd.name AS dest_name, count(*) AS trips
        FROM hops h
        JOIN cameras co ON co.id = h.camera_id
        JOIN cameras cd ON cd.id = h.next_cam
        WHERE h.next_cam IS NOT NULL AND h.next_cam <> h.camera_id
        GROUP BY 1,2,3,4 ORDER BY trips DESC LIMIT ?""",
        (time.time() - hours * 3600, limit))


# ---------------------------------------------------------------- watchlist (NCRB / stolen / FIR / local)
class WatchlistIn(BaseModel):
    plate: str
    reason: str = "Flagged by operator"
    category: str = "FLAGGED"   # STOLEN | FIR | NCRB | FLAGGED


@app.get("/api/watchlist")
def get_watchlist():
    return db.rows(conn, """SELECT plate_norm, reason, category, added_at
                             FROM blacklist ORDER BY added_at DESC""")


@app.post("/api/watchlist")
def add_watchlist(item: WatchlistIn):
    p = normalise(item.plate)
    conn.execute("""INSERT OR REPLACE INTO blacklist (plate_norm, reason, category)
                     VALUES (?,?,?)""", (p, item.reason, item.category))
    conn.commit()
    refresh_watchlist()          # take effect immediately, not on the 2s timer
    return {"added": p, "reason": item.reason, "category": item.category,
            "priority": int(WATCHLIST_PRIORITY.get(item.category, AlertPriority.P3_STANDARD))}


@app.delete("/api/watchlist/{plate}")
def remove_watchlist(plate: str):
    p = normalise(plate)
    conn.execute("DELETE FROM blacklist WHERE plate_norm = ?", (p,))
    conn.commit()
    refresh_watchlist()
    return {"removed": p}


# ---------------------------------------------------------------- e-challans
@app.get("/api/challans")
def list_challans(status: Optional[str] = None, limit: int = 100):
    if status:
        return db.rows(conn, """SELECT * FROM challans WHERE status = ?
                                 ORDER BY ts DESC LIMIT ?""", (status, limit))
    return db.rows(conn, "SELECT * FROM challans ORDER BY ts DESC LIMIT ?", (limit,))


@app.post("/api/challans/{challan_id}/confirm")
def confirm_challan(challan_id: int):
    """TPCR operator confirms an auto-generated challan before it's sent
    onward (e.g. to the Vahan/e-Challan portal in a full deployment)."""
    conn.execute("UPDATE challans SET status = 'CONFIRMED' WHERE id = ?", (challan_id,))
    conn.commit()
    return {"id": challan_id, "status": "CONFIRMED"}


@app.post("/api/challans/{challan_id}/dismiss")
def dismiss_challan(challan_id: int):
    conn.execute("UPDATE challans SET status = 'DISMISSED' WHERE id = ?", (challan_id,))
    conn.commit()
    return {"id": challan_id, "status": "DISMISSED"}


# ---------------------------------------------------------------- habitual offenders
@app.get("/api/offenders")
def offenders(status: str = "OPEN"):
    return db.rows(conn, """SELECT * FROM offender_flags WHERE status = ?
                             ORDER BY last_seen DESC""", (status,))


@app.post("/api/offenders/{plate}/dismiss")
def dismiss_offender(plate: str):
    p = normalise(plate)
    conn.execute("UPDATE offender_flags SET status = 'DISMISSED' WHERE plate_norm = ?", (p,))
    conn.commit()
    return {"plate": p, "status": "DISMISSED"}


# ---------------------------------------------------------------- websocket
@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    clients.add(sock)
    try:
        while True:
            await sock.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        clients.discard(sock)


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")