"""Download Google Photorealistic 3D Tiles (Map Tiles API) covering a small site.

Requires the *Map Tiles API* to be enabled on the key's GCP project. One root
tileset request (`root.json`) is the billable event (SKU "Photorealistic 3D Tiles",
Enterprise tier: 1,000 free/month, then US$6 per 1,000). All child tileset and
GLB fetches ride on the session token returned by that root request and are
not billed separately.

Output: <out_dir>/manifest.json + <out_dir>/*.glb
  manifest.tiles[i] = {"file", "geometric_error", "rtc_center", "transform"(row-major 4x4 or null)}
  manifest.copyrights = sorted list of asset.copyright strings (must be displayed).

NOTE on terms: Google's Map Tiles API policies forbid storing/caching tiles beyond
HTTP cache headers, "geodata extraction" and offline use. Baking these tiles into a
self-hosted GLB is therefore outside the policy; see docs/google-api-handbook.md.
Use web/live3dtiles.html for the policy-compliant, streamed alternative.
"""
from __future__ import annotations

import json
import math
import struct
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geo import lla_to_ecef  # noqa: E402

BASE = "https://tile.googleapis.com"
UA = "modalpoc-terrain/0.1"


def with_params(url: str, key: str, session: str | None) -> str:
    u = urlparse(url if url.startswith("http") else BASE + url)
    q = parse_qs(u.query)
    q["key"] = [key]
    if session and "session" not in q:
        q["session"] = [session]
    return urlunparse(u._replace(query=urlencode({k: v[0] for k, v in q.items()})))


def bounding_sphere(bv: dict, transform: list | None) -> tuple[tuple[float, float, float], float]:
    """Conservative ECEF sphere for a 3D Tiles bounding volume (box or region or sphere)."""
    if "box" in bv:
        b = bv["box"]
        c = b[0:3]
        r = sum(math.sqrt(b[i] ** 2 + b[i + 1] ** 2 + b[i + 2] ** 2) for i in (3, 6, 9))
    elif "sphere" in bv:
        c, r = bv["sphere"][0:3], bv["sphere"][3]
    elif "region" in bv:
        w, s, e, n, h0, h1 = bv["region"]
        corners = [lla_to_ecef(math.degrees(la), math.degrees(lo), h) for la in (s, n) for lo in (w, e) for h in (h0, h1)]
        c = [sum(p[i] for p in corners) / 8 for i in range(3)]
        r = max(math.dist(c, p) for p in corners)
        return tuple(c), r
    else:
        raise ValueError(f"unknown bounding volume {bv}")
    if transform:  # column-major 16 floats
        m = transform
        c = [m[0] * c[0] + m[4] * c[1] + m[8] * c[2] + m[12],
             m[1] * c[0] + m[5] * c[1] + m[9] * c[2] + m[13],
             m[2] * c[0] + m[6] * c[1] + m[10] * c[2] + m[14]]
    return tuple(c), r


def glb_metadata(data: bytes) -> dict:
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    assert magic == b"glTF", "not a GLB"
    chunk_len, chunk_type = struct.unpack_from("<II", data, 12)
    js = json.loads(data[20:20 + chunk_len].decode("utf-8"))
    rtc = js.get("extensions", {}).get("CESIUM_RTC", {}).get("center")
    return {"rtc_center": rtc, "copyright": js.get("asset", {}).get("copyright", ""),
            "draco": "KHR_draco_mesh_compression" in js.get("extensionsUsed", [])}


def fetch_tiles(key: str, lat: float, lon: float, half_size_m: float, out_dir: Path,
                max_tiles: int = 400, sleep: float = 0.05) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    sess = requests.Session()
    sess.headers["User-Agent"] = UA
    site_c = lla_to_ecef(lat, lon, 30.0)
    site_r = half_size_m * math.sqrt(2) + 60.0

    root_url = with_params("/v1/3dtiles/root.json", key, None)
    r = sess.get(root_url, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"root.json -> HTTP {r.status_code}: {r.text[:300]}")
    root = r.json()
    session = None
    queue = deque([(root["root"], None, root.get("asset", {}))])
    tiles, copyrights, n_json = [], set(), 1
    t0 = time.time()

    def intersects(tile, parent_tf):
        tf = tile.get("transform") or parent_tf
        c, rad = bounding_sphere(tile["boundingVolume"], tf)
        return math.dist(c, site_c) <= rad + site_r

    while queue and len(tiles) < max_tiles:
        tile, parent_tf, _ = queue.popleft()
        tf = tile.get("transform") or parent_tf
        if not intersects(tile, parent_tf):
            continue
        content = tile.get("content") or {}
        uri = content.get("uri") or content.get("url")
        children = tile.get("children") or []
        if uri and (".json" in uri.split("?")[0]):
            # external tileset: fetch and continue traversal from its root
            url = with_params(uri, key, session)
            if session is None and "session=" in url:
                session = parse_qs(urlparse(url).query)["session"][0]
            rj = sess.get(url, timeout=60)
            n_json += 1
            if rj.status_code != 200:
                print(f"[google3d] child tileset HTTP {rj.status_code}: {uri[:80]}", file=sys.stderr)
                continue
            sub = rj.json()
            if session is None:
                for k, v in parse_qs(urlparse(url).query).items():
                    if k == "session":
                        session = v[0]
            queue.append((sub["root"], tf, {}))
            continue
        if children:
            for ch in children:
                queue.append((ch, tf, {}))
            continue
        if uri:  # leaf with GLB content
            url = with_params(uri, key, session)
            if session is None and "session=" in url:
                session = parse_qs(urlparse(url).query)["session"][0]
            rg = sess.get(url, timeout=120)
            if rg.status_code != 200 or not rg.content.startswith(b"glTF"):
                print(f"[google3d] tile HTTP {rg.status_code}: {uri[:80]}", file=sys.stderr)
                continue
            meta = glb_metadata(rg.content)
            name = f"tile_{len(tiles):04d}.glb"
            (out_dir / name).write_bytes(rg.content)
            if meta["copyright"]:
                copyrights.update(x.strip() for x in meta["copyright"].split(";") if x.strip())
            m = None
            if tf:
                m = [[tf[0], tf[4], tf[8], tf[12]], [tf[1], tf[5], tf[9], tf[13]],
                     [tf[2], tf[6], tf[10], tf[14]], [tf[3], tf[7], tf[11], tf[15]]]
            tiles.append({"file": name, "geometric_error": tile.get("geometricError"), "rtc_center": meta["rtc_center"],
                          "transform": m, "draco": meta["draco"], "bytes": len(rg.content)})
            time.sleep(sleep)

    manifest = {"site": {"lat": lat, "lon": lon, "half_size_m": half_size_m}, "tiles": tiles,
                "copyrights": sorted(copyrights), "tileset_requests": n_json,
                "bytes": sum(t["bytes"] for t in tiles), "seconds": round(time.time() - t0, 1)}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"[google3d] {len(tiles)} leaf tiles, {manifest['bytes'] / 1e6:.1f} MB, {n_json} tileset requests (1 root = 1 billable), "
          f"copyrights: {manifest['copyrights']}")
    return manifest


if __name__ == "__main__":
    import argparse, os
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("google_map"))
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--half-size", type=float, default=150.0)
    ap.add_argument("--out", type=Path, default=Path("build/tiles"))
    ap.add_argument("--max-tiles", type=int, default=400)
    a = ap.parse_args()
    if not a.key:
        sys.exit("need --key or GOOGLE_MAPS_API_KEY")
    fetch_tiles(a.key, a.lat, a.lon, a.half_size, a.out, a.max_tiles)
