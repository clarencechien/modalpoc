"""Experiment: texture the hero building from Street View panoramas, bake, export, validate.

    python pipeline/build_photo_hero.py --site build/site/site.json --sv build/streetview --out build/out_photo

Steps: build ground + procedural hero -> rewire hero materials to the pano projection ->
bake DIFFUSE colour (4096) -> export scene_photo.glb (hero + LOD1 block + ground) ->
render (a) preset previews and (b) an equirectangular render from the first pano's position,
which should line up with the source panorama if the heading/orientation maths is right.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blender_build as bb  # noqa: E402
from hero import build_hero  # noqa: E402
from hero_photo import apply_to_hero, load_panos  # noqa: E402


def render_equirect_from(pos, out_path: Path, size=1600, samples=8, heading_deg=0.0):
    sc = bpy.context.scene
    cam = bpy.data.cameras.new("PanoCam")
    cam.type = "PANO"
    cam.panorama_type = "EQUIRECTANGULAR"
    ob = bpy.data.objects.new("PanoCam", cam)
    sc.collection.objects.link(ob)
    ob.location = pos
    # Cycles equirect camera: image centre looks along the camera's local -Z? No: for PANO cameras
    # the view direction is the camera's local -Z mapped to the equirect centre, up = local +Y.
    # Point local -Z west (-X) so that the image matches Blender's environment convention
    # (centre = -X, u increases counter-clockwise), i.e. camera rotation: look at -X, up +Z.
    # look along the pano's compass heading so the render can be compared 1:1 with the source
    h = math.radians(heading_deg)
    ob.rotation_euler = Vector((math.sin(h), math.cos(h), 0)).to_track_quat("-Z", "Y").to_euler()
    sc.camera = ob
    sc.render.engine = "CYCLES"
    sc.cycles.samples = samples
    sc.render.resolution_x, sc.render.resolution_y = size, size // 2
    sc.render.resolution_percentage = 100
    sc.render.image_settings.file_format = "JPEG"
    sc.render.filepath = str(out_path)
    bpy.ops.render.render(write_still=True)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", type=Path, required=True)
    ap.add_argument("--sv", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--hero-res", type=int, default=4096)
    ap.add_argument("--bake-res", type=int, default=2048)
    ap.add_argument("--samples", type=int, default=32)
    ap.add_argument("--max-panos", type=int, default=8)
    ap.add_argument("--hero-lat", type=float, default=25.07413)
    ap.add_argument("--hero-lon", type=float, default=121.57772)
    ap.add_argument("--validate", action="store_true", help="also render an equirect view from pano #0")
    ap.add_argument("--gpu", action="store_true")
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    site = json.loads(a.site.read_text(encoding="utf-8"))
    bb.reset_scene()
    if a.gpu:
        bb.enable_gpu()
    bb.setup_lighting()
    ground = bb.build_ground(site, a.site.parent)
    hero_b = bb.find_hero(site, a.hero_lat, a.hero_lon)
    facades = {"office": bb.facade_material("FacadeOffice"), "glass": bb.facade_glass_material("FacadeGlass"),
               "apartment": bb.facade_apartment_material("FacadeApartment")}
    bobjs = bb.build_buildings(site, facades, bb.roof_material("Roof"), skip_ids=(hero_b["id"],))
    buildings = bb.join_objects(bobjs, "Buildings")
    hero = build_hero(hero_b["outer"], hero_b["height"])
    cx = sum(p[0] for p in hero_b["outer"]) / len(hero_b["outer"]); cy = sum(p[1] for p in hero_b["outer"]) / len(hero_b["outer"])
    panos = load_panos(a.sv, site["origin"], a.max_panos, centre=(cx, cy))
    bb.log(f"panos used: {[(p['panoId'][:8], p['src'], round(p['dist']), p['date']) for p in panos]}")
    apply_to_hero(hero, panos, a.sv)

    if a.validate and panos:
        p = panos[0]
        # validation: the hero (photo-projected) + others, seen from the pano position
        render_equirect_from((p["x"], p["y"], p["z"]), a.out / f"validate_{p['panoId'][:8]}.jpg", heading_deg=p["heading"])
        bb.log(f"validation render from pano {p['panoId'][:8]} (compare with {p['file']})")

    # bake: hero DIFFUSE colour (photos already carry lighting) + LOD1/ground COMBINED as usual
    bb.smart_uv(hero, angle=60.0, margin=0.002)
    img_h = bb.new_bake_image("bake_hero_photo", a.hero_res)
    bb.bake(hero, img_h, "DIFFUSE", a.samples)
    bb.smart_uv(buildings)
    img_b = bb.new_bake_image("bake_buildings", a.bake_res)
    bb.bake(buildings, img_b, "COMBINED", a.samples)
    img_g = bb.new_bake_image("bake_ground", a.bake_res)
    bb.bake(ground, img_g, "COMBINED", a.samples)
    bb.save_image(img_h, a.out / "bake_hero_photo.jpg")
    bb.replace_materials(hero, bb.baked_material("HeroPhotoBaked", img_h))
    bb.replace_materials(buildings, bb.baked_material("BuildingsBaked", img_b))
    bb.replace_materials(ground, bb.baked_material("GroundBaked", img_g))
    cams = bb.add_preset_cameras(float(site["half_size_m"]))
    # extra close-up cameras on the hero
    for name, off in (("View_HeroN", (0, 120, 45)), ("View_HeroSW", (-110, -80, 40)), ("View_HeroPlaza", (-60, 30, 4))):
        cam = bpy.data.cameras.new(name); cam.lens = 30
        ob = bpy.data.objects.new(name, cam); bpy.context.scene.collection.objects.link(ob)
        ob.location = (cx + off[0], cy + off[1], off[2])
        ob.rotation_euler = (Vector((cx, cy, 14)) - Vector(ob.location)).to_track_quat("-Z", "Y").to_euler()
        cams.append(ob)
    bb.select_only([hero, buildings, ground] + cams)
    glb = a.out / "scene_photo.glb"
    bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB", use_selection=True, export_apply=True,
                              export_yup=True, export_cameras=True, export_lights=False, export_image_format="JPEG",
                              export_jpeg_quality=86)
    bb.log(f"exported {glb} ({glb.stat().st_size / 1e6:.1f} MB)")
    (a.out / "panos_used.json").write_text(json.dumps(panos, ensure_ascii=False, indent=1), encoding="utf-8")
    bb.render_previews([c for c in cams if c.name in ("View_SW", "View_HeroN", "View_HeroSW", "View_HeroPlaza")], a.out)


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    main(argv)
