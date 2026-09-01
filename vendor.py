"""
Download frontend dependencies locally. Works on Windows, Mac and Linux.

    python vendor.py

Run this ONCE while you have internet. After this the dashboard loads Leaflet
from your own disk, so it works at a venue with no wifi.

Each file has two mirrors. If one CDN is blocked (college networks often block
unpkg), the second is tried automatically.
"""

import os
import sys
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (compatible; anpr-vendor/1.0)"}

L = "leaflet@1.9.4/dist"
H = "leaflet.heat@0.2.0/dist"

def pair(pkg_path):
    return ("https://unpkg.com/" + pkg_path,
            "https://cdn.jsdelivr.net/npm/" + pkg_path)

FILES = {
    "static/vendor/leaflet.css":                  pair(f"{L}/leaflet.css"),
    "static/vendor/leaflet.js":                   pair(f"{L}/leaflet.js"),
    "static/vendor/leaflet-heat.js":              pair(f"{H}/leaflet-heat.js"),
    "static/vendor/images/marker-icon.png":       pair(f"{L}/images/marker-icon.png"),
    "static/vendor/images/marker-icon-2x.png":    pair(f"{L}/images/marker-icon-2x.png"),
    "static/vendor/images/marker-shadow.png":     pair(f"{L}/images/marker-shadow.png"),
}


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def download(path: str, urls: tuple) -> bool:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    last = None
    for url in urls:
        try:
            data = fetch(url)
            if len(data) < 200:
                raise ValueError(f"suspiciously small ({len(data)} bytes)")
            with open(path, "wb") as f:
                f.write(data)
            print(f"  ok    {path}  ({len(data):,} bytes from {url.split('/')[2]})")
            return True
        except Exception as e:
            last = e
    print(f"  FAIL  {path}  -> {last}")
    return False


def main():
    for d in ("static/tiles", "videos", "logs"):
        os.makedirs(d, exist_ok=True)

    failed = [p for p, urls in FILES.items() if not download(p, urls)]

    if failed:
        print(f"\n{len(failed)} file(s) failed on both mirrors.")
        print("Usually a blocked network. Try a mobile hotspot, then run again.")
        print("\nManual fallback - open these in a browser and save by hand:")
        for p in failed:
            print(f"  {FILES[p][0]}")
            print(f"    save to: {p}")
        sys.exit(1)

    print("\nAll vendored. Next:  python cache_models.py")


if __name__ == "__main__":
    main()
