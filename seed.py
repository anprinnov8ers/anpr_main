"""
Seed the database.

  python seed.py cameras     - load the camera network
  python seed.py backfill    - generate ~4h of history so the dashboard isn't empty
  python seed.py demo        - plant the exact rows your demo depends on
  python seed.py all         - all three, in order

Run `all` once the morning of the demo, then leave it alone.
"""

import sys
import time
import random

import db
from normalise import normalise

# --- Camera network -------------------------------------------------------
# EDIT THESE. Right-click a junction in Google Maps -> copy the lat,lng.
# The first four are the ones you filmed; the rest fill out the network so the
# map looks like a city rather than four dots.
CAMERAS = [
    # id,      name,                          lat,       lng,       is_live
    ("CAM_01", "Silk Board Junction",         12.91719, 77.62267, True),
    ("CAM_02", "Bommanahalli Signal",         12.90420, 77.62080, True),
    ("CAM_03", "Begur Road Junction",         12.87330, 77.61490, True),
    ("CAM_04", "Kudlu Gate",                  12.88790, 77.64090, True),
    ("CAM_05", "Hosur Road Flyover",          12.92620, 77.62690, False),
    ("CAM_06", "HSR Layout 27th Main",        12.91180, 77.64510, False),
    ("CAM_07", "Electronic City Toll",        12.84520, 77.66010, False),
    ("CAM_08", "Koramangala 80 Ft Road",      12.93520, 77.62450, False),
    ("CAM_09", "Madiwala Market",             12.92230, 77.61780, False),
    ("CAM_10", "Sarjapur Signal",             12.90980, 77.68020, False),
    ("CAM_11", "BTM 16th Main",               12.91640, 77.60980, False),
    ("CAM_12", "Bannerghatta Road Junction",  12.89010, 77.59760, False),
]

# --- Demo script constants ------------------------------------------------
# These are the plates you will type on stage. Write them on a sticky note.
DEMO_PLATE = "KA05MJ2345"        # the vehicle you trace
DEMO_MISREAD = "KA05MJ2345"      # will be corrupted at CAM_03 by seed demo
BLACKLIST_PLATE = "KA01AB1111"   # the one you add live on stage

STATES = ["KA", "MH", "DL", "TN", "AP", "TS", "KL", "UP", "GJ", "RJ"]
SERIES = ["AB", "MJ", "CD", "HK", "PQ", "XY", "BN", "TR"]


def random_plate() -> str:
    return (random.choice(STATES) + f"{random.randint(1, 59):02d}"
            + random.choice(SERIES) + f"{random.randint(1000, 9999)}")


def load_cameras(conn):
    conn.executemany(
        "INSERT OR REPLACE INTO cameras (id, name, lat, lng, is_live) VALUES (?,?,?,?,?)",
        [(cid, name, lat, lng, int(live)) for cid, name, lat, lng, live in CAMERAS])
    conn.commit()
    print(f"Loaded {len(CAMERAS)} cameras.")


def backfill(conn, hours=4, vehicles=350):
    """Realistic-looking history. Each vehicle takes a short plausible route."""
    now = time.time()
    ids = [c[0] for c in CAMERAS]
    rows = []
    for _ in range(vehicles):
        plate = random_plate()
        route = random.sample(ids, random.randint(2, 5))
        t = now - random.randint(60, hours * 3600)
        for cam in route:
            t += random.randint(90, 600)
            if t > now:
                break
            rows.append((plate, plate, len(plate),
                         round(random.uniform(0.72, 0.99), 3), cam, t, 1))
    conn.executemany("""INSERT INTO events (plate_raw, plate_norm, plate_len,
                        confidence, camera_id, ts, synthetic) VALUES (?,?,?,?,?,?,?)""", rows)
    conn.commit()
    print(f"Backfilled {len(rows)} events across {hours}h.")


def plant_demo(conn):
    """
    Plant the trajectory you will search on stage, INCLUDING a deliberate
    OCR misread at CAM_03. That misread is the whole point of the demo:
    exact matching loses that stop, fuzzy matching keeps it.
    """
    now = time.time()
    plate = normalise(DEMO_PLATE)
    corrupted = plate.replace("5", "S", 1)       # KA05MJ2345 -> KAOSMJ2345-ish

    stops = [
        ("CAM_01", plate,     18, 0.94),
        ("CAM_02", plate,     14, 0.91),
        ("CAM_03", corrupted,  9, 0.68),         # <- the misread
        ("CAM_04", plate,      4, 0.96),
    ]
    conn.executemany("""INSERT INTO events (plate_raw, plate_norm, plate_len,
                        confidence, camera_id, ts, synthetic) VALUES (?,?,?,?,?,?,0)""",
        [(raw, normalise(raw), len(normalise(raw)), conf, cam, now - mins * 60)
         for cam, raw, mins, conf in stops])
    conn.commit()

    print(f"Planted demo trajectory for {plate}")
    print(f"  CAM_03 stores the misread '{corrupted}' - fuzzy search must rescue it.")
    print(f"\nOn stage, search:  {plate}")
    print(f"Then search the misread:  {corrupted}   (should return the same vehicle)")
    print(f"Blacklist plate to add live:  {BLACKLIST_PLATE}")


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    conn = db.connect()
    if what in ("schema", "all"):
        db.init_schema(conn)
        print("Schema applied.")
    if what in ("cameras", "all"):
        load_cameras(conn)
    if what in ("backfill", "all"):
        backfill(conn)
    if what in ("demo", "all"):
        plant_demo(conn)
    conn.close()


if __name__ == "__main__":
    main()
