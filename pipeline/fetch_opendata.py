"""Fetch licence-clean source data for the site (no Google key needed).

* Building footprints + heights: OpenStreetMap via Overpass (ODbL).
* Ground orthophoto: 內政部國土測繪中心 (NLSC) 通用版電子地圖 WMTS, layer PHOTO2
  (政府資料開放授權 / Open Government Data License).
* Optional hand-traced footprints: data/manual_footprints.geojson (WGS84 polygons,
  properties.height in metres) for buildings missing from OSM.

Everything is written to <out_dir>/ as:
    site.json   - {"origin": [lat, lon, 0], "bbox": [...], "buildings": [...], "ground": {...}}
    ground.jpg  - stitched orthophoto covering ground.mercator_bounds
"""
from __future__ import annotations

import io
import json
import math
import os
import sys
import time
from pathlib import Path

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geo import bbox_around, lla_to_enu, lonlat_to_tile, tile_to_lonlat  # noqa: E402

OVERPASS_MIRRORS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
NLSC_WMTS = "https://wmts.nlsc.gov.tw/wmts/{layer}/default/EPSG:3857/{z}/{y}/{x}"
UA = "modalpoc-terrain/0.1 (+https://github.com/clarencechien/modalpoc)"

DEFAULT_HEIGHT_BY_TYPE = {
    "apartments": 18.0, "residential": 12.0, "house": 7.0, "detached": 7.0,
    "commercial": 22.0, "office": 28.0, "industrial": 14.0, "warehouse": 10.0,
    "retail": 8.0, "school": 12.0, "public": 12.0, "hospital": 20.0, "hotel": 30.0,
    "garage": 3.5, "garages": 3.5, "roof": 4.0, "shed": 3.0, "yes": 10.0,
}
LEVEL_HEIGHT = 3.3


def _retry(fn, tries=6, delay=2.0):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # network flakiness through proxies is common
            last = e
            time.sleep(delay * (1.5 ** i))
    raise last


def overpass(query: str, cache_dir: Path | None = None) -> dict:
    import hashlib
    cache = cache_dir / f"overpass_{hashlib.sha1(query.encode()).hexdigest()[:12]}.json" if cache_dir else None
    if cache and cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    for url in OVERPASS_MIRRORS:
        try:
            r = _retry(lambda: requests.post(url, data={"data": query}, headers={"User-Agent": UA}, timeout=120), tries=3)
            if r.status_code == 200:
                data = r.json()
                if cache:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps(data), encoding="utf-8")
                return data
            print(f"[overpass] {url} -> HTTP {r.status_code}", file=sys.stderr)
        except Exception as e:
            print(f"[overpass] {url} failed: {e}", file=sys.stderr)
    raise RuntimeError("all Overpass mirrors failed")


def parse_height(tags: dict) -> float:
    h = tags.get("height") or tags.get("building:height")
    if h:
        try:
            return float(str(h).lower().replace("m", "").strip())
        except ValueError:
            pass
    lv = tags.get("building:levels")
    if lv:
        try:
            return float(lv) * LEVEL_HEIGHT + 1.0
        except ValueError:
            pass
    return DEFAULT_HEIGHT_BY_TYPE.get(tags.get("building", "yes"), 10.0)


def fetch_buildings(bbox, origin, cache_dir: Path | None = None) -> list[dict]:
    s, w, n, e = bbox
    q = f"""[out:json][timeout:90];
(way["building"]({s},{w},{n},{e});relation["building"]["type"="multipolygon"]({s},{w},{n},{e}););
out body;>;out skel qt;"""
    data = overpass(q, cache_dir)
    nodes = {el["id"]: (el["lat"], el["lon"]) for el in data["elements"] if el["type"] == "node"}
    ways = {el["id"]: el for el in data["elements"] if el["type"] == "way"}
    out = []

    def ring_enu(node_ids):
        pts = [nodes[i] for i in node_ids if i in nodes]
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        return [list(lla_to_enu(la, lo, 0.0, origin)[:2]) for la, lo in pts]

    used_in_relation = set()
    for el in data["elements"]:
        if el["type"] != "relation":
            continue
        outers, inners = [], []
        for m in el.get("members", []):
            if m["type"] != "way" or m["ref"] not in ways:
                continue
            used_in_relation.add(m["ref"])
            (outers if m.get("role") != "inner" else inners).append(ring_enu(ways[m["ref"]]["nodes"]))
        for k, o in enumerate(outers):
            if len(o) >= 3:
                out.append({"id": f"r{el['id']}_{k}", "tags": el.get("tags", {}), "outer": o,
                            "inners": [i for i in inners if len(i) >= 3], "height": parse_height(el.get("tags", {}))})
    for wid, w in ways.items():
        if wid in used_in_relation or "building" not in w.get("tags", {}):
            continue
        ring = ring_enu(w["nodes"])
        if len(ring) >= 3:
            out.append({"id": f"w{wid}", "tags": w["tags"], "outer": ring, "inners": [], "height": parse_height(w["tags"])})
    return out


def load_manual_footprints(path: Path, origin) -> list[dict]:
    if not path.exists():
        return []
    gj = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for i, f in enumerate(gj.get("features", [])):
        g = f.get("geometry", {})
        if g.get("type") != "Polygon":
            continue
        rings = [[list(lla_to_enu(lat, lon, 0.0, origin)[:2]) for lon, lat in ring] for ring in g["coordinates"]]
        for r in rings:
            if len(r) > 1 and r[0] == r[-1]:
                r.pop()
        props = f.get("properties", {})
        out.append({"id": f"m{i}", "tags": {"building": props.get("building", "yes"), "name": props.get("name", "")},
                    "outer": rings[0], "inners": rings[1:], "height": float(props.get("height", 10.0)),
                    "manual": True})
    return out


def clip_buildings(buildings: list[dict], half_size: float) -> list[dict]:
    """Keep buildings whose centroid lies inside the site square."""
    keep = []
    for b in buildings:
        xs = [p[0] for p in b["outer"]]
        ys = [p[1] for p in b["outer"]]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        if abs(cx) <= half_size and abs(cy) <= half_size:
            keep.append(b)
    return keep


def fetch_ortho(bbox, zoom: int, out_path: Path, layer: str = "PHOTO2", cache_dir: Path | None = None) -> dict:
    s, w, n, e = bbox
    x0, y0 = lonlat_to_tile(w, n, zoom)
    x1, y1 = lonlat_to_tile(e, s, zoom)
    tx0, ty0, tx1, ty1 = int(math.floor(x0)), int(math.floor(y0)), int(math.floor(x1)), int(math.floor(y1))
    cols, rows = tx1 - tx0 + 1, ty1 - ty0 + 1
    mosaic = Image.new("RGB", (256 * cols, 256 * rows))
    sess = requests.Session()
    sess.headers["User-Agent"] = UA
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            cache = cache_dir / f"{layer}_{zoom}_{tx}_{ty}.jpg" if cache_dir else None
            if cache and cache.exists():
                img = Image.open(cache)
            else:
                url = NLSC_WMTS.format(layer=layer, z=zoom, y=ty, x=tx)
                r = _retry(lambda: sess.get(url, timeout=30))
                r.raise_for_status()
                img = Image.open(io.BytesIO(r.content)).convert("RGB")
                if cache:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    img.save(cache, quality=92)
            mosaic.paste(img, (256 * (tx - tx0), 256 * (ty - ty0)))
    # crop exactly to bbox (in tile-fraction space -> pixels)
    px0, py0 = int((x0 - tx0) * 256), int((y0 - ty0) * 256)
    px1, py1 = int(math.ceil((x1 - tx0) * 256)), int(math.ceil((y1 - ty0) * 256))
    crop = mosaic.crop((px0, py0, px1, py1))
    crop.save(out_path, quality=92)
    # exact lon/lat bounds of the crop, for UV mapping in Blender
    lon_w, lat_n = tile_to_lonlat(tx0 + px0 / 256, ty0 + py0 / 256, zoom)
    lon_e, lat_s = tile_to_lonlat(tx0 + px1 / 256, ty0 + py1 / 256, zoom)
    return {
        "image": out_path.name, "zoom": zoom, "layer": layer, "size": crop.size,
        "tile_bounds": [tx0 + px0 / 256, ty0 + py0 / 256, tx0 + px1 / 256, ty0 + py1 / 256],
        "lonlat_bounds": [lon_w, lat_s, lon_e, lat_n],
        "attribution": "正射影像 © 內政部國土測繪中心 (NLSC), 政府資料開放授權",
    }


def build_site(center_lat: float, center_lon: float, half_size_m: float, out_dir: Path,
               ortho_zoom: int = 20, manual: Path | None = None, cache_dir: Path | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    origin = (center_lat, center_lon, 0.0)
    # fetch slightly larger than the site so edge buildings are complete
    bbox = bbox_around(center_lat, center_lon, half_size_m * 1.25)
    print(f"[site] centre=({center_lat},{center_lon}) half={half_size_m} m bbox={bbox}")
    buildings = fetch_buildings(bbox, origin, cache_dir)
    print(f"[site] OSM buildings in bbox: {len(buildings)}")
    manual_b = load_manual_footprints(manual, origin) if manual else []
    if manual_b:
        print(f"[site] manual footprints: {len(manual_b)}")
    buildings = clip_buildings(buildings + manual_b, half_size_m * 1.15)
    print(f"[site] buildings kept: {len(buildings)}")
    ground_bbox = bbox_around(center_lat, center_lon, half_size_m)
    ground = fetch_ortho(ground_bbox, ortho_zoom, out_dir / "ground.jpg", cache_dir=cache_dir)
    print(f"[site] ortho {ground['size']} px at z{ortho_zoom}")
    site = {
        "origin": list(origin), "half_size_m": half_size_m, "bbox": list(ground_bbox),
        "buildings": buildings, "ground": ground,
        "attribution": ["建物輪廓 © OpenStreetMap contributors (ODbL)", ground["attribution"]],
    }
    (out_dir / "site.json").write_text(json.dumps(site, ensure_ascii=False), encoding="utf-8")
    return site


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--half-size", type=float, default=150.0, help="half side length of the site square, metres")
    ap.add_argument("--zoom", type=int, default=20)
    ap.add_argument("--out", type=Path, default=Path("build/site"))
    ap.add_argument("--manual", type=Path, default=Path("data/manual_footprints.geojson"))
    ap.add_argument("--cache", type=Path, default=Path("build/tilecache"))
    a = ap.parse_args()
    build_site(a.lat, a.lon, a.half_size, a.out, a.zoom, a.manual, a.cache)
