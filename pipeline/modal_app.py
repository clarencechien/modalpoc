"""Run the whole pipeline on Modal with a portable Blender (official Linux tarball).

    # once: export MODAL_TOKEN_ID=... MODAL_TOKEN_SECRET=...   (or `modal token set`)
    modal run pipeline/modal_app.py --lat 25.0740 --lon 121.5775 --half-size 150
    modal run pipeline/modal_app.py --gpu none --samples 32          # CPU only
    modal run pipeline/modal_app.py --gpu l40s --samples 256         # bigger GPU (l4 | a10g | l40s | none)
    modal run pipeline/modal_app.py --mode google3d                  # needs Map Tiles API

Outputs land in the Modal Volume `modalpoc-terrain-out` and are downloaded to
web/assets/ (scene.glb, stats.json, preview_*.png) so `web/index.html` can show them.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import sys

import modal

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

BLENDER_VERSION = "4.2.23"  # 4.2 LTS
BLENDER_URL = f"https://download.blender.org/release/Blender4.2/blender-{BLENDER_VERSION}-linux-x64.tar.xz"
PIPELINE_DIR = Path(__file__).resolve().parent
REMOTE_PIPELINE = "/root/pipeline"

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "curl", "xz-utils", "libx11-6", "libxi6", "libxxf86vm1", "libxfixes3", "libxrender1",
        "libxkbcommon0", "libgl1", "libglu1-mesa", "libegl1", "libsm6", "libice6", "libgomp1",
        "libxcursor1", "libxinerama1", "libxrandr2", "libwayland-client0", "libdbus-1-3",
    )
    .run_commands(
        f"curl -fsSL {BLENDER_URL} -o /tmp/blender.tar.xz",
        "mkdir -p /opt/blender && tar -xJf /tmp/blender.tar.xz -C /opt/blender --strip-components=1 && rm /tmp/blender.tar.xz",
        "/opt/blender/blender --version",
    )
    .pip_install("requests==2.32.3", "pillow==10.4.0")
    .add_local_dir(str(PIPELINE_DIR), REMOTE_PIPELINE)
)

app = modal.App("modalpoc-terrain", image=image)
out_volume = modal.Volume.from_name("modalpoc-terrain-out", create_if_missing=True)
cache_volume = modal.Volume.from_name("modalpoc-terrain-cache", create_if_missing=True)

GOOGLE_SECRET = "google-maps-key"  # optional Modal secret with GOOGLE_MAPS_API_KEY (google3d mode)


def _run_blender(args: list[str], cwd: str) -> str:
    cmd = ["/opt/blender/blender", "-b", "--python-exit-code", "1",
           "--python", f"{REMOTE_PIPELINE}/blender_build.py", "--", *args]
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    keep = [l for l in proc.stdout.splitlines() if not l.startswith(("Fra:", "Read prefs", "Blender "))]
    print("\n".join(keep[-60:]), flush=True)
    if proc.returncode != 0:
        print(proc.stderr[-4000:], flush=True)
        raise RuntimeError(f"blender exited with {proc.returncode}")
    return proc.stdout


def _build_impl(cfg: dict, site_files: dict | None = None) -> dict:
    """site_files: {"site.json": bytes, "ground.jpg": bytes} fetched on the caller's machine.
    If absent, the open data is fetched from inside the container instead."""
    import sys
    sys.path.insert(0, REMOTE_PIPELINE)

    run_id = cfg.get("run_id") or time.strftime("%Y%m%d-%H%M%S")
    work = Path("/out") / run_id
    site_dir = work / "site"
    out_dir = work / "out"
    site_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    if site_files:
        for name, data in site_files.items():
            (site_dir / name).write_bytes(data)
        print(f"[modal] using {len(site_files)} uploaded site files ({sum(map(len, site_files.values())) / 1e6:.1f} MB)")
    else:
        from fetch_opendata import build_site
        manual = Path(REMOTE_PIPELINE) / "data" / "manual_footprints.geojson"
        build_site(cfg["lat"], cfg["lon"], cfg["half_size"], site_dir, cfg["zoom"],
                   manual if manual.exists() else None, Path("/cache/tiles"))
        cache_volume.commit()
    t_fetch = time.time() - t0

    args = ["--site", str(site_dir / "site.json"), "--out", str(out_dir), "--mode", cfg["mode"],
            "--bake-res", str(cfg["bake_res"]), "--ground-res", str(cfg["ground_res"]),
            "--samples", str(cfg["samples"]), "--target-tris", str(cfg["target_tris"])]
    if cfg.get("use_gpu"):
        args.append("--gpu")
    if cfg["mode"] == "google3d":
        from fetch_google3d import fetch_tiles
        key = os.environ.get("GOOGLE_MAPS_API_KEY") or cfg.get("google_key")
        if not key:
            raise RuntimeError("google3d mode needs GOOGLE_MAPS_API_KEY (Modal secret 'google-maps-key')")
        tiles_dir = work / "tiles"
        fetch_tiles(key, cfg["lat"], cfg["lon"], cfg["half_size"], tiles_dir)
        args += ["--tiles", str(tiles_dir)]
    _run_blender(args, str(work))
    out_volume.commit()

    stats = json.loads((out_dir / "stats.json").read_text())
    stats["run_id"] = run_id
    stats["seconds_fetch"] = round(t_fetch, 1)
    stats["seconds_total"] = round(time.time() - t0, 1)
    files = {p.name: p.read_bytes() for p in out_dir.iterdir()
             if p.suffix in (".glb", ".json", ".png") or p.name.startswith("bake_")}
    return {"stats": stats, "files": files}


common = dict(image=image, volumes={"/out": out_volume, "/cache": cache_volume}, timeout=3 * 3600, cpu=4.0, memory=16384)


@app.function(gpu="L4", **common)
def build_gpu(cfg: dict, site_files: dict | None = None) -> dict:
    return _build_impl(cfg, site_files)


@app.function(gpu="A10G", **common)
def build_gpu_a10g(cfg: dict, site_files: dict | None = None) -> dict:
    return _build_impl(cfg, site_files)


@app.function(gpu="L40S", **common)
def build_gpu_l40s(cfg: dict, site_files: dict | None = None) -> dict:
    return _build_impl(cfg, site_files)


@app.function(**common)
def build_cpu(cfg: dict, site_files: dict | None = None) -> dict:
    return _build_impl(cfg, site_files)


@app.local_entrypoint()
def main(lat: float = 25.0740, lon: float = 121.5775, half_size: float = 150.0, zoom: int = 20,
         mode: str = "opendata", bake_res: int = 4096, ground_res: int = 4096, samples: int = 96,
         target_tris: int = 80000, gpu: str = "L4", out: str = "web/assets", remote_fetch: bool = False):
    cfg = dict(lat=lat, lon=lon, half_size=half_size, zoom=zoom, mode=mode, bake_res=bake_res,
               ground_res=ground_res, samples=samples, target_tris=target_tris, use_gpu=gpu.lower() != "none")
    fn = {"none": build_cpu, "l4": build_gpu, "a10g": build_gpu_a10g, "l40s": build_gpu_l40s}[gpu.lower()]
    site_files = None
    if not remote_fetch:
        # Overpass mirrors often refuse cloud IPs: fetch the (small) open data here, ship it to Modal.
        from fetch_opendata import build_site
        site_dir = Path("build/site")
        manual = PIPELINE_DIR / "data" / "manual_footprints.geojson"
        build_site(lat, lon, half_size, site_dir, zoom, manual if manual.exists() else None, Path("build/tilecache"))
        site_files = {p.name: p.read_bytes() for p in site_dir.iterdir() if p.name in ("site.json", "ground.jpg")}
    print(f"[modal] building {cfg} on {fn.object_id if hasattr(fn, 'object_id') else gpu}")
    res = fn.remote(cfg, site_files)
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, data in res["files"].items():
        (out_dir / name).write_bytes(data)
    (out_dir / "stats.json").write_text(json.dumps(res["stats"], ensure_ascii=False, indent=2), encoding="utf-8")
    s = res["stats"]
    print(f"[modal] done in {s['seconds_total']}s on {s.get('device')} | tris {s.get('tris_before')} -> {s.get('tris_after')} | "
          f"glb {s['glb_bytes'] / 1e6:.1f} MB -> {out_dir}/scene.glb")
