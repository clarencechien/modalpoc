"""Download NLSC 3D Tiles for the site from Modal (the sandbox proxy cannot reach nlsc.gov.tw)."""
import sys
from pathlib import Path

import modal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

app = modal.App("modalpoc-nlsc-fetch")
img = (modal.Image.debian_slim(python_version="3.11").pip_install("requests", "cryptography")
       .add_local_dir(str(Path(__file__).resolve().parents[1] / "pipeline"), "/root/pipeline"))


@app.function(image=img, timeout=1800)
def fetch(url: str, lat: float, lon: float, half: float, min_ge: float | None) -> dict:
    import sys, json
    sys.path.insert(0, "/root/pipeline")
    from tls_tw import make_bundle
    from fetch_tiles3d import fetch_tiles
    bundle = make_bundle()
    import requests
    root = requests.get(url, timeout=60, verify=bundle).json()
    rt = root["root"]
    info = {"asset": root.get("asset"), "geometricError": root.get("geometricError"), "bv": rt.get("boundingVolume"),
            "refine": rt.get("refine"), "transform": rt.get("transform"), "children": len(rt.get("children", [])),
            "content": rt.get("content"), "child0": json.dumps(rt.get("children", [{}])[0])[:700]}
    out = Path("/tmp/tiles")
    logs = []
    man = fetch_tiles(url, lat, lon, half, out, verify=bundle, min_geometric_error=min_ge, log=lambda m: logs.append(m))
    files = {p.name: p.read_bytes() for p in out.iterdir()}
    return {"info": info, "manifest": man, "logs": logs[-20:], "files": files}


@app.local_entrypoint()
def main(url: str = "https://3dtiles.nlsc.gov.tw/building/tiles3d/30/tileset.json", lat: float = 25.0740, lon: float = 121.5775,
         half: float = 150.0, out: str = "build/tiles_nlsc", min_ge: float = -1.0):
    import json
    res = fetch.remote(url, lat, lon, half, None if min_ge < 0 else min_ge)
    print("ROOT:", json.dumps(res["info"], ensure_ascii=False)[:1500])
    print("\n".join(res["logs"]))
    o = Path(out); o.mkdir(parents=True, exist_ok=True)
    for name, data in res["files"].items():
        (o / name).write_bytes(data)
    m = res["manifest"]
    print(f"saved {len(m['tiles'])} tiles ({m['bytes'] / 1e6:.1f} MB) to {o}; copyrights={m['copyrights']}")
    if m["tiles"]:
        print("sample:", json.dumps(m["tiles"][0], ensure_ascii=False)[:600])
