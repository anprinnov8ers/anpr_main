"""
Download and cache the ANPR models, then prove they work offline.

    python cache_models.py

Run once with internet. Then TURN YOUR WIFI OFF and run it again. If it
succeeds the second time, your demo will survive a venue with no network.
That second run is the whole point - do not skip it.
"""

import time
import numpy as np


def main():
    print("Loading models (first run downloads ~50MB)...")
    t0 = time.time()

    from fast_alpr import ALPR

    alpr = ALPR(
        detector_model="yolo-v9-t-384-license-plate-end2end",
        ocr_model="global-plates-mobile-vit-v2-model",
    )
    print(f"Models ready in {time.time() - t0:.1f}s")

    # Run one inference on a blank frame. We do not care about the result -
    # we only care that every weight file is present and loads.
    blank = np.zeros((480, 640, 3), dtype=np.uint8)
    t1 = time.time()
    alpr.predict(blank)
    print(f"Inference path works ({(time.time() - t1) * 1000:.0f}ms on a blank frame)")

    print("\nMODELS CACHED.")
    print("Now turn wifi OFF and run this again. If it still works, you are safe.")


if __name__ == "__main__":
    main()
