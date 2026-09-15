"""Generic OGC 3D Tiles (1.0/1.1) downloader for a small site: used for the NLSC 全國三維建物
(https://3dtiles.nlsc.gov.tw/tiles3d/service) and reusable for any tileset.json.

* Walks the tileset (external tilesets, REPLACE/ADD refinement), composing `transform`s.
* Keeps tiles whose bounding volume (region / box / sphere) intersects the site sphere.
* Downloads leaf content; b3dm is unwrapped to GLB (feature-table RTC_CENTER recorded).
* Writes <out>/manifest.json compatible with blender_build.import_google_tiles():
    tiles[i] = {"file", "geometric_error", "rtc_center", "transform" (row-major 4x4 | null), "bytes"}

NLSC servers omit the TWCA intermediate certificate; pass --ca-bundle from tools/nlsc_probe.make_bundle()
or set verify to a bundle that includes it (done automatically on Modal via `make_bundle`).
"""
from __future__ import annotations

import json
import math
import struct
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geo import lla_to_ecef  # noqa: E402

IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]  # column-major


def mat_mul(a, b):
    """Column-major 4x4 multiply a*b (both flat lists)."""
    r = [0.0] * 16
    for c in range(4):
        for rr in range(4):
            r[c * 4 + rr] = sum(a[k * 4 + rr] * b[c * 4 + k] for k in range(4))
    return r


def apply(m, p):
    return (m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12],
            m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13],
            m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14])


def bounding_sphere(bv: dict, m: list) -> tuple[tuple[float, float, float], float]:
    if "region" in bv:  # regions are always WGS84, unaffected by transforms
        w, s, e, n, h0, h1 = bv["region"]
        corners = [lla_to_ecef(math.degrees(la), math.degrees(lo), h) for la in (s, n) for lo in (w, e) for h in (h0, h1)]
        c = tuple(sum(p[i] for p in corners) / 8 for i in range(3))
        return c, max(math.dist(c, p) for p in corners)
    if "box" in bv:
        b = bv["box"]
        c = apply(m, b[0:3])
        scale = max(math.sqrt(sum(m[i * 4 + j] ** 2 for j in range(3))) for i in range(3))
        r = sum(math.sqrt(b[i] ** 2 + b[i + 1] ** 2 + b[i + 2] ** 2) for i in (3, 6, 9)) * scale
        return c, r
    if "sphere" in bv:
        sp = bv["sphere"]
        return apply(m, sp[0:3]), sp[3] * max(math.sqrt(sum(m[i * 4 + j] ** 2 for j in range(3))) for i in range(3))
    raise ValueError(f"unknown boundingVolume {bv}")


def unwrap_b3dm(data: bytes) -> tuple[bytes, list | None, dict]:
    magic = data[:4]
    if magic == b"glTF":
        return data, None, {}
    if magic != b"b3dm":
        raise ValueError(f"unsupported content magic {magic!r}")
    _, version, length, ft_json_len, ft_bin_len, bt_json_len, bt_bin_len = struct.unpack_from("<4sIIIIII", data, 0)
    off = 28
    ft = json.loads(data[off:off + ft_json_len].decode("utf-8")) if ft_json_len else {}
    off += ft_json_len + ft_bin_len
    bt = json.loads(data[off:off + bt_json_len].decode("utf-8")) if bt_json_len else {}
    off += bt_json_len + bt_bin_len
    glb = data[off:length]
    rtc = ft.get("RTC_CENTER")
    if isinstance(rtc, dict):  # binary-body reference; rare in practice
        rtc = None
    return glb, rtc, {"feature_table": {k: v for k, v in ft.items() if k != "RTC_CENTER"}, "batch_table_keys": list(bt.keys())[:20]}


def fetch_tiles(tileset_url: str, lat: float, lon: float, half_size_m: float, out_dir: Path,
                verify=True, max_tiles: int = 2000, min_geometric_error: float | None = None, sleep: float = 0.0,
                log=print) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    sess = requests.Session()
    sess.headers["User-Agent"] = "modalpoc-terrain/0.1"
    site_c = lla_to_ecef(lat, lon, 30.0)
    site_r = half_size_m * math.sqrt(2) + 40.0

    def get_json(url):
        r = sess.get(url, timeout=120, verify=verify)
        r.raise_for_status()
        return r.json()

    root_js = get_json(tileset_url)
    queue = deque([(root_js["root"], IDENTITY, tileset_url)])
    tiles, n_json, n_visited, t0 = [], 1, 0, time.time()
    copyrights = set()
    asset = root_js.get("asset", {})
    while queue and len(tiles) < max_tiles:
        tile, parent_m, base_url = queue.popleft()
        n_visited += 1
        m = mat_mul(parent_m, tile["transform"]) if tile.get("transform") else parent_m
        c, r = bounding_sphere(tile["boundingVolume"], m)
        if math.dist(c, site_c) > r + site_r:
            continue
        # 3D Tiles 1.1 may use `contents: [...]` (multiple contents); take every uri
        contents = ([tile["content"]] if tile.get("content") else []) + list(tile.get("contents") or [])
        uris = [c.get("uri") or c.get("url") for c in contents if c.get("uri") or c.get("url")]
        uri = uris[0] if uris else None
        children = tile.get("children") or []
        ge = tile.get("geometricError", 0.0)
        if uri and uri.split("?")[0].lower().endswith(".json"):
            sub_url = urljoin(base_url, uri)
            try:
                sub = get_json(sub_url)
                n_json += 1
            except Exception as e:
                log(f"[tiles3d] external tileset failed {sub_url}: {e}")
                continue
            queue.append((sub["root"], m, sub_url))
            continue
        fine_enough = min_geometric_error is not None and ge <= min_geometric_error
        if children and not fine_enough:
            for ch in children:
                queue.append((ch, m, base_url))
            if tile.get("refine", "REPLACE").upper() == "REPLACE" or not uri:
                continue
        if not uri:
            continue
        for uri in uris:
          url = urljoin(base_url, uri)
          try:
            r = sess.get(url, timeout=180, verify=verify)
            r.raise_for_status()
          except Exception as e:
            log(f"[tiles3d] content failed {url}: {e}")
            continue
          try:
            glb, rtc, meta = unwrap_b3dm(r.content)
          except Exception as e:
            log(f"[tiles3d] skip {url}: {e}")
            continue
          _save_tile(glb, rtc, meta, m, ge, url, out_dir, tiles, copyrights)
          if sleep:
            time.sleep(sleep)
    manifest = {"tileset": tileset_url, "asset": asset, "site": {"lat": lat, "lon": lon, "half_size_m": half_size_m},
                "tiles": tiles, "copyrights": sorted(copyrights), "tileset_requests": n_json, "visited": n_visited,
                "bytes": sum(t["bytes"] for t in tiles), "seconds": round(time.time() - t0, 1)}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"[tiles3d] {len(tiles)} tiles, {manifest['bytes'] / 1e6:.1f} MB, visited {n_visited}, {n_json} tileset requests, {manifest['seconds']}s")
    return manifest


def _save_tile(glb, rtc, meta, m, ge, url, out_dir, tiles, copyrights):
        name = f"tile_{len(tiles):05d}.glb"
        (out_dir / name).write_bytes(glb)
        # glb asset.copyright
        try:
            jl = struct.unpack_from("<I", glb, 12)[0]
            js = json.loads(glb[20:20 + jl])
            cp = js.get("asset", {}).get("copyright")
            if cp:
                copyrights.add(cp)
        except Exception:
            pass
        tm = None if m == IDENTITY else [[m[0], m[4], m[8], m[12]], [m[1], m[5], m[9], m[13]], [m[2], m[6], m[10], m[14]], [m[3], m[7], m[11], m[15]]]
        tiles.append({"file": name, "geometric_error": ge, "rtc_center": rtc, "transform": tm, "bytes": len(glb),
                      "source": url.split("?")[0][-80:], **({"meta": meta} if meta.get("batch_table_keys") else {})})


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--lat", type=float, default=25.0740)
    ap.add_argument("--lon", type=float, default=121.5775)
    ap.add_argument("--half-size", type=float, default=150.0)
    ap.add_argument("--out", type=Path, default=Path("build/tiles_nlsc"))
    ap.add_argument("--ca-bundle", default=None)
    ap.add_argument("--min-ge", type=float, default=None)
    a = ap.parse_args()
    fetch_tiles(a.url, a.lat, a.lon, a.half_size, a.out, verify=a.ca_bundle or True, min_geometric_error=a.min_ge)
