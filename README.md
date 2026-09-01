# City-Wide ANPR Intelligence Platform

Team **Innvo8ors** · SIH 2026 · PS 260127 · Bharat Electronics Limited

Prototype: real-time plate reading across a camera network, OCR-noise-tolerant
trajectory reconstruction, ingestion-time watchlist alerts, and macro traffic
analytics.

---

## What's here

| File | Job |
|---|---|
| `schema.sql` | Tables and indexes |
| `db.py` | SQLite connection helpers |
| `normalise.py` | **The differentiator.** Plate normalisation + fuzzy matching |
| `cam_worker.py` | One per camera: video → ALPR → POST to /api/ingest |
| `api.py` | Ingestion queue, watchlist check, search, analytics, WebSocket |
| `static/index.html` | The control-room dashboard |
| `seed.py` | Cameras, backfilled history, planted demo trajectory |
| `loadgen.py` | Synthetic replay for the scale claim |
| `run.py` | One-command start and stop, all platforms |
| `vendor.py` | Download Leaflet locally (no CDN on stage) |
| `cache_models.py` | Cache the ONNX models and verify they load offline |

---

## Setup (do this today, on wifi)

Everything runs through Python, so the commands are identical on Windows,
Mac and Linux. There are no `.sh` scripts - PowerShell cannot run those.

```
pip install -r requirements.txt
python vendor.py
python cache_models.py
```

Then **turn your wifi off and run `python cache_models.py` again.** If it still
succeeds, your demo will survive a venue with no network. If it fails, the
models did not cache and you must fix it now, not on stage. Skipping this
check is the single most common way SIH prototypes die.

**No Docker, no database server to install.** Storage is SQLite, which ships
with Python. The whole platform is Python processes and one `anpr.db` file.

---

## Get your camera footage

You need **the same vehicle at multiple cameras, in time order.** Random
traffic footage will not give you this, and without it there is no trajectory
to reconstruct.

Film it yourself, in about two hours:

1. Pick four spots — a society gate, a parking exit, a street corner, a junction.
2. Have a friend drive one car past all four in sequence.
3. Record each pass on your phone, 15–20 seconds, plate clearly visible.
4. Repeat with two or three vehicles.
5. Save as `videos/cam_01.mp4` … `videos/cam_04.mp4`.
6. Put the real lat/lng of those four spots into `CAMERAS` in `seed.py`.

Shoot slightly downward, plate roughly filling a tenth of the frame. Bright
daylight for the primary clips. If you can, grab one dusk clip too — it gives
you an honest answer when a judge asks about low light.

---

## Run it

```
python run.py
```

Then open http://localhost:8000. Ctrl-C in that window stops everything,
Docker included.

Useful variants:

```
python run.py --setup      # rebuild schema and seed data only
python run.py --no-cams    # dashboard and API, no video processing
```

If a worker dies, `run.py` prints a warning naming the log file. All logs live
in `logs/`.

To run pieces by hand instead, one terminal each:

```
python run.py --setup
python -m uvicorn api:app --port 8000
python cam_worker.py CAM_01 videos/cam_01.mp4
```

Start the API first - the camera workers POST to it.

For the scale moment during the demo: `python loadgen.py 2000`

---

## Verify the differentiator works

```bash
python normalise.py
```

All seven cases must pass. If they don't, stop and fix it — the fuzzy matcher
is the only part of this build that is actually yours.

---

## Architecture

```
cam_worker.py x4                                    browser
(video -> ALPR)                                        |
      |  HTTP POST /api/ingest                  WebSocket /ws
      v                                                |
  asyncio.Queue  ->  drain task  ->  SQLite  ->  FastAPI
                     normalise
                     watchlist check
```

`POST /api/ingest` enqueues and returns. It never touches the database on the
request path. The drain task batches up to 1,000 events per write.

## How the fuzzy matching works

Two representations of every plate:

- **normalised** — separators stripped, uppercased. This is what we store and
  what we show the judge.
- **canonical** — visually-confusable characters collapsed into one class
  (`O D Q → 0`, `I L → 1`, `B → 8`, `S → 5`, `G → 6`…). Never stored, never
  displayed. Comparison only.

Two reads are the same vehicle when their **lengths are equal** and their
canonical forms are within **edit distance 2**.

The equal-length constraint matters. OCR on plates produces character
substitutions, not insertions or deletions. Requiring equal length cuts the
false-positive rate hard, and we would rather miss a truncated read than
return the wrong vehicle to a police officer.

Search runs in two stages so it stays fast at city scale:

1. **SQL pre-filter** on the indexed `plate_len` column plus a time window.
2. **Fuzzy match in Python** on that shortlist only.

Edit distance never runs over the full events table. That is your answer when
a judge asks whether fuzzy search scales.

**Known limitation — say this before they find it.** Two genuinely different
plates one digit apart (`KA01AB1111` vs `KA01AB1112`) will match. We mitigate
by flagging physically impossible hops: if consecutive stops imply over
120 km/h, the UI marks that leg as a probable plate collision rather than
silently including it. A production system would add vehicle colour and type
from the detector as a second signal.

---

## Demo-day checklist

Print this. Tick every box before you walk in.

- [ ] `python cache_models.py` succeeds with wifi OFF
- [ ] `python vendor.py` finished clean; `static/vendor/` has 6 files
- [ ] `python normalise.py` — all seven cases pass
- [ ] `python seed.py all` run this morning; dashboard is not empty
- [ ] Demo plate written on a sticky note: `KA05MJ2345`
- [ ] Watchlist plate on the same note: `KA01AB1111`
- [ ] Misread search tested — the corrupted string returns the same vehicle
- [ ] Watchlist alert fired end to end at least once
- [ ] **Screen recording of the full working demo saved to the desktop**
- [ ] Laptop on mains power, sleep disabled, notifications off
- [ ] Browser zoom set so the projector can read the plate text
- [ ] Rehearsed out loud, timed, three times

The screen recording is not optional. Record it the moment everything works.

---

## Measured numbers

Run on the development machine, so quote them as such:

- **Ingestion: ~36,000 events/sec accepted, queue fully drained.**
  1,000 cameras at 2 reads/sec each is 2,000/sec, so there is roughly 18x
  headroom on the ingestion path.
- Trajectory search over 185,000 events: sub-second.
- Model load: ~2s from cache. Inference ~60-70ms per frame on CPU, no GPU.

Reproduce the first one live during the demo with `python loadgen.py 2000`.

## Honest scope

Built in three days. Deliberately not built:

- **Custom OCR model.** Pre-trained ONNX weights via `fast-alpr`. The pipeline
  is model-agnostic — swapping in a fine-tuned Indian-plate model changes one
  class and nothing else.
- **Postgres, PostGIS and TimescaleDB.** SQLite with the same indexes. The
  distance maths is Haversine in Python, which is what the PostGIS column was
  doing anyway at this scale. Postgres is the production path when the event
  table outgrows a single file.
- **Celery and Redis.** An in-process `asyncio.Queue` drained by a background
  task. The decoupling property is identical: a camera POST returns without
  ever touching the database, so camera load cannot stall on writes.

Say all three plainly if asked. Judges respect a team that knows exactly what
it did and did not build far more than one that oversells.

Measure your real OCR accuracy before the demo. Hand-label 50–100 plate crops
from your own clips, compute exact-match and character-level accuracy, and
quote both numbers. Do not repeat the ">90%" figure from the deck unless you
have measured it.
