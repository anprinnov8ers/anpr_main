"""
Synthetic event replay. Proves the ingestion path holds at city volume.

    python loadgen.py            # 2000 events/sec
    python loadgen.py 5000       # push harder

Every event goes through the SAME Redis queue, the SAME normaliser and the
SAME blacklist check as a real camera read. That is the honest claim to make
on stage - not "we tested 1000 cameras", but "the ingestion path sustains
N events/sec, and 1000 cameras at 2 reads/sec is 2000 events/sec".

Events are tagged synthetic=True so they never pollute the live ticker.
"""

import sys
import time
import random

import httpx

API = "http://127.0.0.1:8000/api/ingest"

CAMERAS = [f"CAM_{i:02d}" for i in range(1, 13)]
STATES = ["KA", "MH", "DL", "TN", "AP", "TS", "KL", "UP", "GJ", "RJ"]
SERIES = ["AB", "MJ", "CD", "HK", "PQ", "XY", "BN", "TR"]

# A fixed pool so plates recur across cameras - that makes the OD analytics
# and density numbers look like real traffic, not white noise.
POOL = [f"{random.choice(STATES)}{random.randint(1,59):02d}"
        f"{random.choice(SERIES)}{random.randint(1000,9999)}"
        for _ in range(4000)]


def main():
    rate = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    client = httpx.Client(timeout=10.0)
    batch = 500
    delay = batch / rate

    print(f"Pushing ~{rate} events/sec at {API}. Ctrl-C to stop.")
    sent = 0
    t0 = time.time()

    while True:
        now = time.time()
        reads = [{"plate_raw": random.choice(POOL),
                  "confidence": round(random.uniform(0.7, 0.99), 3),
                  "camera_id": random.choice(CAMERAS),
                  "ts": now, "synthetic": True} for _ in range(batch)]
        try:
            r = client.post(API, json={"reads": reads}).json()
        except Exception as e:
            print(f"  post failed: {e}"); time.sleep(1); continue
        sent += batch

        elapsed = time.time() - t0
        if sent % 5000 == 0:
            print(f"  sent {sent:,}  ({sent/elapsed:.0f}/s)  queue depth {r.get('queue_depth')}")
        time.sleep(max(0, delay))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
