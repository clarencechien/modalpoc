"""Google Street View panoramas via the Map Tiles API (Street View Tiles SKU).

Needs the Map Tiles API enabled on the key. Session -> panoIds -> metadata -> z/x/y tiles,
stitched into equirectangular JPEGs (heading = compass bearing of the image centre).

Output: <out_dir>/panos.json  [{"panoId","lat","lng","heading","tilt","roll","date","copyright","file","width","height"}]
        <out_dir>/<panoId>.jpg

Policy note: Street View imagery must show the copyright from metadata, and Map Tiles content
must not be stored or used offline beyond cache headers. Treat the outputs as a local experiment.
"""
from __future__ import annotations

import io
import json
import math
import sys
import time
from pathlib import Path

import requests
from PIL import Image

BASE = "https://tile.googleapis.com/v1"


def create_session(key: str) -> str:
    r = requests.post(f"{BASE}/createSession?key={key}", json={"mapType": "streetview", "language": "zh-TW", "region": "TW"}, timeout=30)
    r.raise_for_status()
    return r.json()["session"]


def pano_ids(key: str, session: str, locations: list[tuple[float, float]], radius: float = 50) -> list[str]:
    body = {"locations": [{"lat": la, "lng": lo} for la, lo in locations], "radius": radius}
    r = requests.post(f"{BASE}/streetview/panoIds?session={session}&key={key}", json=body, timeout=30)
    r.raise_for_status()
    return [p for p in dict.fromkeys(r.json().get("panoIds", [])) if p]


def metadata(key: str, session: str, pano_id: str) -> dict:
    r = requests.get(f"{BASE}/streetview/metadata?session={session}&key={key}&panoId={pano_id}", timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_pano(key: str, session: str, meta: dict, zoom: int, out: Path, sleep=0.03) -> tuple[int, int]:
    """Stitch all tiles of one panorama at `zoom` (0..5). Tile grid = image size / tile size at that zoom."""
    full_w, full_h = meta["imageWidth"], meta["imageHeight"]
    tw, th = meta.get("tileWidth", 512), meta.get("tileHeight", 512)
    # zoom 5 = full resolution for 16384-wide panos; each lower zoom halves. Panos with smaller
    # native size top out earlier: level count = log2(full_w / 512) + 1.
    max_z = int(round(math.log2(full_w / tw)))
    z = min(zoom, max_z)
    w, h = full_w >> (max_z - z), full_h >> (max_z - z)
    cols, rows = math.ceil(w / tw), math.ceil(h / th)
    img = Image.new("RGB", (cols * tw, rows * th))
    sess = requests.Session()
    for y in range(rows):
        for x in range(cols):
            url = f"{BASE}/streetview/tiles/{z}/{x}/{y}?session={session}&key={key}&panoId={meta['panoId']}"
            for attempt in range(4):
                r = sess.get(url, timeout=30)
                if r.status_code == 200:
                    img.paste(Image.open(io.BytesIO(r.content)).convert("RGB"), (x * tw, y * th))
                    break
                time.sleep(0.5 * (attempt + 1))
            else:
                print(f"[sv] tile {z}/{x}/{y} failed for {meta['panoId']}: HTTP {r.status_code}", file=sys.stderr)
            time.sleep(sleep)
    img = img.crop((0, 0, w, h))
    img.save(out, quality=90)
    return w, h


def fetch_area(key: str, locations: list[tuple[float, float]], out_dir: Path, zoom: int = 3, radius: float = 50,
               only_google: bool = False) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    session = create_session(key)
    ids = pano_ids(key, session, locations, radius)
    panos = []
    for pid in ids:
        m = metadata(key, session, pid)
        if only_google and "Google" not in (m.get("copyright") or ""):
            continue
        f = out_dir / f"{pid}.jpg"
        w, h = fetch_pano(key, session, m, zoom, f)
        rec = {k: m.get(k) for k in ("panoId", "lat", "lng", "heading", "tilt", "roll", "date", "copyright")}
        rec.update(file=f.name, width=w, height=h)
        panos.append(rec)
        print(f"[sv] {pid[:14]} {m.get('date')} heading={m.get('heading'):.1f} -> {f.name} {w}x{h}")
    (out_dir / "panos.json").write_text(json.dumps(panos, ensure_ascii=False, indent=1), encoding="utf-8")
    return panos


if __name__ == "__main__":
    import argparse, os
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("google_map"))
    ap.add_argument("--lat", type=float, default=25.07413)
    ap.add_argument("--lon", type=float, default=121.57772)
    ap.add_argument("--ring", type=float, default=70.0, help="sample points on a ring of this radius (m) + centre")
    ap.add_argument("--zoom", type=int, default=3)
    ap.add_argument("--radius", type=float, default=50)
    ap.add_argument("--out", type=Path, default=Path("build/streetview"))
    a = ap.parse_args()
    mlat, mlon = 111320.0, 111320.0 * math.cos(math.radians(a.lat))
    locs = [(a.lat, a.lon)] + [(a.lat + a.ring * math.cos(t) / mlat, a.lon + a.ring * math.sin(t) / mlon) for t in [i * math.pi / 6 for i in range(12)]]
    fetch_area(a.key, locs, a.out, a.zoom, a.radius)
