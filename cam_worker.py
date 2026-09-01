"""
One camera. Reads a video file on a loop, runs ALPR, pushes reads to Redis.

    python cam_worker.py CAM_01 videos/cam_01.mp4

Run one per camera, in its own terminal. Four terminals for four feeds.

The first run downloads the ONNX models (~50MB). DO THIS BEFORE DEMO DAY -
there is no internet on stage.
"""

import sys
import time

import cv2
import httpx
from fast_alpr import ALPR

from normalise import normalise, is_plausible

API = "http://127.0.0.1:8000/api/ingest"

FRAME_SKIP = 5          # process every Nth frame - 5 is a good speed/recall trade
DEDUPE_WINDOW = 10.0    # seconds; same plate at same camera counts once
MIN_CONFIDENCE = 0.55   # below this the read is noise, not a plate


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    cam_id, video = sys.argv[1], sys.argv[2]
    client = httpx.Client(timeout=5.0)

    print(f"[{cam_id}] loading models...")
    alpr = ALPR(
        detector_model="yolo-v9-t-384-license-plate-end2end",
        # 'global' handles Indian plates far better than the european model
        ocr_model="global-plates-mobile-vit-v2-model",
    )
    print(f"[{cam_id}] ready, streaming {video}")

    recent: dict[str, float] = {}
    pushed = 0

    while True:
        cap = cv2.VideoCapture(video)
        if not cap.isOpened():
            print(f"[{cam_id}] cannot open {video}")
            sys.exit(1)

        frame_no = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame_no += 1
            if frame_no % FRAME_SKIP:
                continue

            for res in alpr.predict(frame):
                if not res.ocr:
                    continue
                conf = float(res.ocr.confidence)
                if conf < MIN_CONFIDENCE:
                    continue

                norm = normalise(res.ocr.text)
                if not is_plausible(norm):
                    continue

                now = time.time()
                if now - recent.get(norm, 0) < DEDUPE_WINDOW:
                    continue          # still the same car in frame
                recent[norm] = now

                try:
                    client.post(API, json={"reads": [{
                        "plate_raw": res.ocr.text,
                        "confidence": conf,
                        "camera_id": cam_id,
                        "ts": now,
                        "synthetic": False,
                    }]})
                except Exception as e:
                    # API not up yet, or restarting. Keep reading frames.
                    print(f"[{cam_id}] post failed: {e}")
                    continue
                pushed += 1
                print(f"[{cam_id}] {norm}  conf={conf:.2f}  (total {pushed})")

            time.sleep(0.02)          # keep playback near real-time

        cap.release()
        recent.clear()                # fresh pass, let the same cars re-trigger


if __name__ == "__main__":
    main()
