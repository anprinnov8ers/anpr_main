"""
Start the platform. No Docker required.

    python run.py            # everything
    python run.py --setup    # rebuild database and seed data only
    python run.py --no-cams  # dashboard and API only

Ctrl-C stops everything.
"""

import os
import sys
import time
import signal
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
procs = []


def spawn(name, cmd):
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    log = open(os.path.join(HERE, "logs", f"{name}.log"), "w")
    p = subprocess.Popen(cmd, cwd=HERE, stdout=log, stderr=subprocess.STDOUT)
    procs.append((name, p))
    print(f"    started {name}  (log: logs/{name}.log)")
    return p


def shutdown(*_):
    print("\nStopping...")
    for name, p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    time.sleep(1)
    for _, p in procs:
        if p.poll() is None:
            p.kill()
    print("All stopped.")
    sys.exit(0)


def main():
    setup_only = "--setup" in sys.argv
    no_cams = "--no-cams" in sys.argv

    if setup_only or not os.path.exists(os.path.join(HERE, "anpr.db")):
        print("==> building database + seed data")
        subprocess.run([PY, "seed.py", "all"], cwd=HERE, check=True)
        if setup_only:
            print("\nDone. Start it with: python run.py")
            return

    signal.signal(signal.SIGINT, shutdown)

    print("==> api (also handles ingestion)")
    spawn("api", [PY, "-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", "8000"])
    time.sleep(4)          # let the API bind before cameras start posting

    if not no_cams:
        print("==> camera feeds")
        found = 0
        for i in ("01", "02", "03", "04"):
            video = os.path.join(HERE, "videos", f"cam_{i}.mp4")
            if os.path.exists(video):
                spawn(f"cam_{i}", [PY, "cam_worker.py", f"CAM_{i}", video])
                found += 1
                time.sleep(3)      # stagger model loading, avoids a RAM spike
            else:
                print(f"    skipping CAM_{i} - videos/cam_{i}.mp4 not found")
        if found == 0:
            print("    no videos yet - dashboard shows seeded history only")

    print("\n" + "=" * 52)
    print("  Dashboard:  http://localhost:8000")
    print("  Logs:       logs/")
    print("  Stop:       press Ctrl-C here")
    print("=" * 52 + "\n")

    try:
        while True:
            time.sleep(1)
            for name, p in list(procs):
                if p.poll() is not None:
                    print(f"WARNING: {name} exited. Check logs/{name}.log")
                    procs.remove((name, p))
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
