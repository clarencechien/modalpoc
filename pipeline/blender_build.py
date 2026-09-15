"""Build, light-bake, decimate and export the site with Blender (bpy).

Runs either inside Blender (`blender -b -P blender_build.py -- ...`) or with the
`bpy` wheel (`python blender_build.py ...`). Two modes:

  opendata : LOD1 buildings extruded from footprints + orthophoto ground, lit with a
             sun + Nishita sky, COMBINED-baked to texture atlases (so the web viewer
             renders it unlit with real shadows / AO), then decimated + exported.
  google3d : photogrammetry GLB tiles (from fetch_google3d.py) merged, clipped to the
             site square, decimated to a target triangle budget, and the original
             photo textures re-baked (DIFFUSE colour, high-poly -> low-poly) into one
             atlas. Lighting is already in the photos, so only colour is baked.

Outputs (in --out):
  scene.glb, bake_*.jpg (debug copies), stats.json, preview_*.png
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import bpy
import bmesh
from mathutils import Matrix, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geo import lla_to_enu, tile_to_lonlat  # noqa: E402

T0 = time.time()


def log(msg):
    print(f"[blender +{time.time() - T0:6.1f}s] {msg}", flush=True)


# ----------------------------------------------------------------------------- scene


def reset_scene():
    bpy.ops.wm.read_homefile(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.unit_settings.system = "METRIC"
    sc.view_settings.view_transform = "Standard"
    sc.view_settings.look = "None"
    sc.view_settings.exposure = 0.0
    return sc


def enable_gpu() -> str:
    """Use OptiX/CUDA when Blender can see a GPU (Modal), otherwise stay on CPU."""
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
    except KeyError:
        return "CPU"
    for dtype in ("OPTIX", "CUDA"):
        try:
            prefs.compute_device_type = dtype
        except TypeError:
            continue
        prefs.get_devices()
        gpus = [d for d in prefs.devices if d.type == dtype]
        if gpus:
            for d in prefs.devices:
                d.use = d.type in (dtype, "CPU")
            bpy.context.scene.cycles.device = "GPU"
            log(f"cycles device: {dtype} ({', '.join(d.name for d in gpus)})")
            return dtype
    log("cycles device: CPU (no GPU found)")
    return "CPU"


def link(obj):
    bpy.context.scene.collection.objects.link(obj)
    return obj


def select_only(objs):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]


def tri_count(obj) -> int:
    return sum(len(p.vertices) - 2 for p in obj.data.polygons)


# ----------------------------------------------------------------------------- lighting


def setup_lighting(sun_elevation_deg=55.0, sun_azimuth_deg=140.0, sun_strength=1.6):
    """Afternoon sun from the south-west (Taipei, ~15:00). Azimuth is compass degrees."""
    world = bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    nt = world.node_tree
    nt.nodes.clear()
    sky = nt.nodes.new("ShaderNodeTexSky")
    sky.sky_type = "NISHITA"
    sky.sun_disc = False
    sky.sun_elevation = math.radians(sun_elevation_deg)
    sky.sun_rotation = math.radians(sun_azimuth_deg)
    sky.altitude = 20
    sky.air_density = 1.2
    sky.dust_density = 1.5
    bg = nt.nodes.new("ShaderNodeBackground")
    bg.inputs["Strength"].default_value = 0.22
    out = nt.nodes.new("ShaderNodeOutputWorld")
    nt.links.new(sky.outputs[0], bg.inputs["Color"])
    nt.links.new(bg.outputs[0], out.inputs["Surface"])

    sun = bpy.data.lights.new("Sun", "SUN")
    sun.energy = sun_strength
    sun.angle = math.radians(1.5)
    sun.color = (1.0, 0.96, 0.9)
    so = link(bpy.data.objects.new("Sun", sun))
    # Blender sun points along its local -Z. Compass azimuth -> rotation about Z.
    el, az = math.radians(sun_elevation_deg), math.radians(sun_azimuth_deg)
    direction = Vector((math.sin(az) * math.cos(el), math.cos(az) * math.cos(el), math.sin(el)))  # towards the sun
    so.rotation_euler = (-direction).to_track_quat("-Z", "Y").to_euler()
    return so


# ----------------------------------------------------------------------------- materials


def image_material(name, image, roughness=0.85):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = image
    tex.interpolation = "Cubic"
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["Specular IOR Level"].default_value = 0.2
    return m


def _along_facade(nt, geo, sep):
    """World-space distance along the wall: t = -ny*x + nx*y (n = face normal)."""
    nsep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(geo.outputs["True Normal"], nsep.inputs[0])
    m1 = nt.nodes.new("ShaderNodeMath"); m1.operation = "MULTIPLY"
    m2 = nt.nodes.new("ShaderNodeMath"); m2.operation = "MULTIPLY"
    nt.links.new(sep.outputs["X"], m1.inputs[0]); nt.links.new(nsep.outputs["Y"], m1.inputs[1])
    nt.links.new(sep.outputs["Y"], m2.inputs[0]); nt.links.new(nsep.outputs["X"], m2.inputs[1])
    sub = nt.nodes.new("ShaderNodeMath"); sub.operation = "SUBTRACT"
    nt.links.new(m2.outputs[0], sub.inputs[0]); nt.links.new(m1.outputs[0], sub.inputs[1])
    return sub.outputs[0]


def facade_material(name, seed_variation=True):
    """Procedural curtain-wall facade: window bands in world Z, mullions in world XY."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(geo.outputs["Position"], sep.inputs[0])

    def band(inp, period, duty):
        div = nt.nodes.new("ShaderNodeMath"); div.operation = "DIVIDE"; div.inputs[1].default_value = period
        frac = nt.nodes.new("ShaderNodeMath"); frac.operation = "FRACT"
        lt = nt.nodes.new("ShaderNodeMath"); lt.operation = "LESS_THAN"; lt.inputs[1].default_value = duty
        nt.links.new(inp, div.inputs[0]); nt.links.new(div.outputs[0], frac.inputs[0]); nt.links.new(frac.outputs[0], lt.inputs[0])
        return lt.outputs[0]

    z_band = band(sep.outputs["Z"], 3.3, 0.62)           # window height per floor
    xy_band = band(_along_facade(nt, geo, sep), 2.4, 0.78)  # window columns along the wall
    win = nt.nodes.new("ShaderNodeMath"); win.operation = "MULTIPLY"
    nt.links.new(z_band, win.inputs[0]); nt.links.new(xy_band, win.inputs[1])

    # per-object colour variation for the wall
    oi = nt.nodes.new("ShaderNodeObjectInfo")
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (0.36, 0.34, 0.32, 1)
    ramp.color_ramp.elements[1].color = (0.60, 0.58, 0.54, 1)
    e = ramp.color_ramp.elements.new(0.5); e.color = (0.48, 0.45, 0.41, 1)
    nt.links.new(oi.outputs["Random"], ramp.inputs["Fac"])
    glass = nt.nodes.new("ShaderNodeRGB"); glass.outputs[0].default_value = (0.10, 0.16, 0.22, 1)
    mix = nt.nodes.new("ShaderNodeMix"); mix.data_type = "RGBA"
    nt.links.new(win.outputs[0], mix.inputs["Factor"])
    nt.links.new(ramp.outputs["Color"], mix.inputs[6])
    nt.links.new(glass.outputs[0], mix.inputs[7])
    nt.links.new(mix.outputs[2], bsdf.inputs["Base Color"])
    rough = nt.nodes.new("ShaderNodeMix"); rough.data_type = "FLOAT"
    rough.inputs[2].default_value = 0.8; rough.inputs[3].default_value = 0.15
    nt.links.new(win.outputs[0], rough.inputs["Factor"])
    nt.links.new(rough.outputs[0], bsdf.inputs["Roughness"])
    met = nt.nodes.new("ShaderNodeMix"); met.data_type = "FLOAT"
    met.inputs[2].default_value = 0.0; met.inputs[3].default_value = 0.4
    nt.links.new(win.outputs[0], met.inputs["Factor"])
    nt.links.new(met.outputs[0], bsdf.inputs["Metallic"])
    return m


def facade_glass_material(name):
    """Office tower: dark glass with a lighter spandrel band per floor and thin mullions."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    geo = nt.nodes.new("ShaderNodeNewGeometry"); sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(geo.outputs["Position"], sep.inputs[0])
    d = nt.nodes.new("ShaderNodeMath"); d.operation = "DIVIDE"; d.inputs[1].default_value = 3.3
    f = nt.nodes.new("ShaderNodeMath"); f.operation = "FRACT"
    band_z = nt.nodes.new("ShaderNodeMath"); band_z.operation = "LESS_THAN"; band_z.inputs[1].default_value = 0.28
    nt.links.new(sep.outputs["Z"], d.inputs[0]); nt.links.new(d.outputs[0], f.inputs[0]); nt.links.new(f.outputs[0], band_z.inputs[0])
    d2 = nt.nodes.new("ShaderNodeMath"); d2.operation = "DIVIDE"; d2.inputs[1].default_value = 1.6
    f2 = nt.nodes.new("ShaderNodeMath"); f2.operation = "FRACT"
    mull = nt.nodes.new("ShaderNodeMath"); mull.operation = "LESS_THAN"; mull.inputs[1].default_value = 0.06
    nt.links.new(_along_facade(nt, geo, sep), d2.inputs[0]); nt.links.new(d2.outputs[0], f2.inputs[0]); nt.links.new(f2.outputs[0], mull.inputs[0])
    band = nt.nodes.new("ShaderNodeMath"); band.operation = "MAXIMUM"
    nt.links.new(band_z.outputs[0], band.inputs[0]); nt.links.new(mull.outputs[0], band.inputs[1])
    oi = nt.nodes.new("ShaderNodeObjectInfo")
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (0.05, 0.09, 0.13, 1)
    ramp.color_ramp.elements[1].color = (0.10, 0.16, 0.16, 1)
    nt.links.new(oi.outputs["Random"], ramp.inputs["Fac"])
    span = nt.nodes.new("ShaderNodeRGB"); span.outputs[0].default_value = (0.42, 0.44, 0.46, 1)
    mix = nt.nodes.new("ShaderNodeMix"); mix.data_type = "RGBA"
    nt.links.new(band.outputs[0], mix.inputs["Factor"]); nt.links.new(ramp.outputs["Color"], mix.inputs[6]); nt.links.new(span.outputs[0], mix.inputs[7])
    nt.links.new(mix.outputs[2], bsdf.inputs["Base Color"])
    rough = nt.nodes.new("ShaderNodeMix"); rough.data_type = "FLOAT"; rough.inputs[2].default_value = 0.12; rough.inputs[3].default_value = 0.6
    nt.links.new(band.outputs[0], rough.inputs["Factor"]); nt.links.new(rough.outputs[0], bsdf.inputs["Roughness"])
    bsdf.inputs["Metallic"].default_value = 0.3
    return m


def facade_apartment_material(name):
    """Taiwanese apartment block: warm tile, recessed windows, balcony railing bands."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    geo = nt.nodes.new("ShaderNodeNewGeometry"); sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(geo.outputs["Position"], sep.inputs[0])

    def band(inp, period, lo, hi):
        d = nt.nodes.new("ShaderNodeMath"); d.operation = "DIVIDE"; d.inputs[1].default_value = period
        f = nt.nodes.new("ShaderNodeMath"); f.operation = "FRACT"
        gt = nt.nodes.new("ShaderNodeMath"); gt.operation = "GREATER_THAN"; gt.inputs[1].default_value = lo
        lt = nt.nodes.new("ShaderNodeMath"); lt.operation = "LESS_THAN"; lt.inputs[1].default_value = hi
        mul = nt.nodes.new("ShaderNodeMath"); mul.operation = "MULTIPLY"
        nt.links.new(inp, d.inputs[0]); nt.links.new(d.outputs[0], f.inputs[0])
        nt.links.new(f.outputs[0], gt.inputs[0]); nt.links.new(f.outputs[0], lt.inputs[0])
        nt.links.new(gt.outputs[0], mul.inputs[0]); nt.links.new(lt.outputs[0], mul.inputs[1])
        return mul.outputs[0]

    win_z = band(sep.outputs["Z"], 3.1, 0.35, 0.8)
    win_xy = band(_along_facade(nt, geo, sep), 3.2, 0.15, 0.6)
    win = nt.nodes.new("ShaderNodeMath"); win.operation = "MULTIPLY"
    nt.links.new(win_z, win.inputs[0]); nt.links.new(win_xy, win.inputs[1])
    rail = band(sep.outputs["Z"], 3.1, 0.05, 0.3)
    oi = nt.nodes.new("ShaderNodeObjectInfo")
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (0.62, 0.55, 0.46, 1)
    ramp.color_ramp.elements[1].color = (0.74, 0.70, 0.64, 1)
    e = ramp.color_ramp.elements.new(0.5); e.color = (0.66, 0.58, 0.55, 1)
    nt.links.new(oi.outputs["Random"], ramp.inputs["Fac"])
    dark = nt.nodes.new("ShaderNodeRGB"); dark.outputs[0].default_value = (0.08, 0.09, 0.10, 1)
    mix1 = nt.nodes.new("ShaderNodeMix"); mix1.data_type = "RGBA"
    nt.links.new(win.outputs[0], mix1.inputs["Factor"]); nt.links.new(ramp.outputs["Color"], mix1.inputs[6]); nt.links.new(dark.outputs[0], mix1.inputs[7])
    railc = nt.nodes.new("ShaderNodeRGB"); railc.outputs[0].default_value = (0.5, 0.5, 0.5, 1)
    mix2 = nt.nodes.new("ShaderNodeMix"); mix2.data_type = "RGBA"
    nt.links.new(rail, mix2.inputs["Factor"]); nt.links.new(mix1.outputs[2], mix2.inputs[6]); nt.links.new(railc.outputs[0], mix2.inputs[7])
    nt.links.new(mix2.outputs[2], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.85
    return m


def pick_style(tags: dict, rnd: float) -> str:
    t = tags.get("building", "yes")
    if t in ("apartments", "residential", "house", "detached", "dormitory"):
        return "apartment"
    if t in ("commercial", "office", "hotel"):
        return "glass" if rnd < 0.5 else "office"
    if t in ("industrial", "warehouse", "public", "school", "hospital", "retail"):
        return "office" if rnd < 0.7 else "glass"
    return "glass" if rnd < 0.3 else ("apartment" if rnd < 0.62 else "office")


def roof_material(name):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    noise = nt.nodes.new("ShaderNodeTexNoise"); noise.inputs["Scale"].default_value = 0.6
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (0.32, 0.32, 0.31, 1)
    ramp.color_ramp.elements[1].color = (0.52, 0.50, 0.47, 1)
    nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.9
    return m


# ----------------------------------------------------------------------------- geometry (opendata)


def build_ground(site: dict, site_dir: Path, grid=48):
    g = site["ground"]
    img = bpy.data.images.load(str(site_dir / g["image"]))
    img.colorspace_settings.name = "sRGB"
    tx0, ty0, tx1, ty1 = g["tile_bounds"]
    origin = tuple(site["origin"])
    bm = bmesh.new()
    uv_layer = bm.loops.layers.uv.new("UVMap")
    verts = []
    for j in range(grid + 1):
        row = []
        for i in range(grid + 1):
            fu, fv = i / grid, j / grid
            lon, lat = tile_to_lonlat(tx0 + fu * (tx1 - tx0), ty0 + fv * (ty1 - ty0), g["zoom"])
            x, y, _ = lla_to_enu(lat, lon, 0.0, origin)
            row.append(bm.verts.new((x, y, 0.0)))
        verts.append(row)
    for j in range(grid):
        for i in range(grid):
            # tile row j grows southwards, so go (i,j) -> (i,j+1) -> (i+1,j+1) -> (i+1,j) for an up-facing normal
            f = bm.faces.new((verts[j][i], verts[j + 1][i], verts[j + 1][i + 1], verts[j][i + 1]))
            uvs = [(i / grid, 1 - j / grid), (i / grid, 1 - (j + 1) / grid),
                   ((i + 1) / grid, 1 - (j + 1) / grid), ((i + 1) / grid, 1 - j / grid)]
            for loop, uv in zip(f.loops, uvs):
                loop[uv_layer].uv = uv
    bm.normal_update()
    me = bpy.data.meshes.new("Ground")
    bm.to_mesh(me)
    bm.free()
    ob = link(bpy.data.objects.new("Ground", me))
    ob.data.materials.append(image_material("GroundOrtho", img, roughness=0.9))
    return ob


def point_in_poly(x, y, poly) -> bool:
    c = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]; x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def find_hero(site: dict, lat: float, lon: float):
    """Building containing (or nearest within 60 m to) the hero point."""
    from geo import lla_to_enu
    x, y, _ = lla_to_enu(lat, lon, 0.0, tuple(site["origin"]))
    best, best_d = None, 60.0
    for b in site["buildings"]:
        if point_in_poly(x, y, b["outer"]):
            return b
        cx = sum(p[0] for p in b["outer"]) / len(b["outer"]); cy = sum(p[1] for p in b["outer"]) / len(b["outer"])
        d = math.hypot(cx - x, cy - y)
        if d < best_d:
            best, best_d = b, d
    return best


def self_intersecting(pts) -> bool:
    """True if any two non-adjacent edges of the ring cross (bow-tie outlines from OSM)."""
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for j in range(i + 2, n):
            if (j + 1) % n == i:
                continue
            c, d = pts[j], pts[(j + 1) % n]
            if ccw(a, c, d) != ccw(b, c, d) and ccw(a, b, c) != ccw(a, b, d):
                return True
    return False


def ring_area(pts):
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))


def build_buildings(site: dict, facade_mats: dict, roof_mat, min_height=2.5, skip_ids=()):
    """LOD1 'game-style' blocks: extruded footprint, parapet ring, an occasional rooftop box,
    and a facade style (glass / office / apartment) picked from the OSM building tag."""
    import random
    objs = []
    rng = random.Random(1)
    # OSM outlines overlap now and then (building + building:part, two outers of one relation).
    # Coplanar overlapping roofs bake black where one hides the other and z-fight in the viewer,
    # so drop the smaller of an equal-height overlapping pair by 0.35 m.
    blds = [b for b in site["buildings"] if b["id"] not in skip_ids]
    nudge = {}
    for i in range(len(blds)):
        for j in range(i + 1, len(blds)):
            a, c = blds[i], blds[j]
            if abs(float(a["height"]) - float(c["height"])) > 0.5:
                continue
            if any(point_in_poly(x, y, c["outer"]) for x, y in a["outer"]) or any(point_in_poly(x, y, a["outer"]) for x, y in c["outer"]):
                small = a if abs(ring_area(a["outer"])) < abs(ring_area(c["outer"])) else c
                nudge[small["id"]] = nudge.get(small["id"], 0.0) - 0.35
    for b in blds:
        outer = [p for p in b["outer"]]
        if len(outer) < 3:
            continue
        if ring_area(outer) < 0:
            outer.reverse()
        # drop consecutive duplicates
        clean = [outer[0]]
        for p in outer[1:]:
            if math.hypot(p[0] - clean[-1][0], p[1] - clean[-1][1]) > 0.05:
                clean.append(p)
        if math.hypot(clean[0][0] - clean[-1][0], clean[0][1] - clean[-1][1]) < 0.05:
            clean.pop()
        if len(clean) < 3 or abs(ring_area(clean)) < 4.0:
            continue
        if self_intersecting(clean):
            log(f"skip {b['id']}: self-intersecting outline ({len(clean)} verts) - bakes black")
            continue
        h = max(float(b["height"]) + nudge.get(b["id"], 0.0), min_height)
        bm = bmesh.new()
        vs = [bm.verts.new((x, y, 0.0)) for x, y in clean]
        try:
            base = bm.faces.new(vs)
        except ValueError:
            bm.free()
            continue
        res = bmesh.ops.extrude_face_region(bm, geom=[base])
        top = [g for g in res["geom"] if isinstance(g, bmesh.types.BMFace)]
        for f in top:
            for v in f.verts:
                v.co.z = h
        bmesh.ops.delete(bm, geom=[base], context="FACES_ONLY")
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        for f in bm.faces:
            f.material_index = 1 if f.normal.z > 0.9 else 0
        area = abs(ring_area(clean))
        # parapet: inset the roof and raise the ring
        top_faces = [f for f in bm.faces if f.normal.z > 0.9]
        if area > 60 and len(top_faces) == 1:
            r = bmesh.ops.inset_region(bm, faces=top_faces, thickness=0.5, depth=0.0, use_even_offset=True)
            ring = r["faces"]
            res = bmesh.ops.extrude_face_region(bm, geom=ring)
            bmesh.ops.translate(bm, verts=[g for g in res["geom"] if isinstance(g, bmesh.types.BMVert)], vec=(0, 0, 0.8))
            for f in bm.faces:
                if f not in top_faces and f.normal.z > 0.9:
                    f.material_index = 1
            # rooftop box (water tank / stair head) on bigger roofs
            if area > 250 and rng.random() < 0.8:
                cx = sum(p[0] for p in clean) / len(clean); cy = sum(p[1] for p in clean) / len(clean)
                sx, sy, sz = rng.uniform(3, 6), rng.uniform(2.5, 4), rng.uniform(2, 3)
                if all(point_in_poly(cx + dx, cy + dy, clean) for dx in (-sx, sx) for dy in (-sy, sy)):
                    vs = [bm.verts.new((cx + x, cy + y, h)) for x, y in ((-sx / 2, -sy / 2), (sx / 2, -sy / 2), (sx / 2, sy / 2), (-sx / 2, sy / 2))]
                    bf = bm.faces.new(vs)
                    res = bmesh.ops.extrude_face_region(bm, geom=[bf])
                    bmesh.ops.translate(bm, verts=[g for g in res["geom"] if isinstance(g, bmesh.types.BMVert)], vec=(0, 0, sz))
                    bmesh.ops.delete(bm, geom=[bf], context="FACES_ONLY")
                    for f in bm.faces:
                        if f.material_index == 0 and f.normal.z > 0.9:
                            f.material_index = 1
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        # concave roof n-gons must be triangulated before baking: Cycles fans n-gons,
        # which leaves black slivers outside concave outlines
        bmesh.ops.triangulate(bm, faces=[f for f in bm.faces if len(f.verts) > 4], ngon_method="EAR_CLIP")
        flipped = [f for f in bm.faces if f.normal.z < -0.9 and all(v.co.z > 0.5 for v in f.verts)]
        if flipped:
            bmesh.ops.reverse_faces(bm, faces=flipped)
        # slight triangulation-safe cleanup of the top n-gon
        me = bpy.data.meshes.new(f"B_{b['id']}")
        bm.to_mesh(me)
        bm.free()
        ob = link(bpy.data.objects.new(f"B_{b['id']}", me))
        ob.data.materials.append(facade_mats[pick_style(b.get("tags", {}), rng.random())])
        ob.data.materials.append(roof_mat)
        ob["osm_id"] = b["id"]
        ob["name_tag"] = b.get("tags", {}).get("name", "")
        objs.append(ob)
    log(f"buildings created: {len(objs)}")
    return objs


def join_objects(objs, name):
    select_only(objs)
    bpy.ops.object.join()
    ob = bpy.context.view_layer.objects.active
    ob.name = name
    ob.data.name = name
    return ob


# ----------------------------------------------------------------------------- UV + bake


def smart_uv(ob, angle=66.0, margin=0.004):
    select_only([ob])
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=math.radians(angle), island_margin=margin, scale_to_bounds=False)
    bpy.ops.uv.pack_islands(margin=margin, rotate=True)
    bpy.ops.object.mode_set(mode="OBJECT")


def new_bake_image(name, res, srgb=True):
    img = bpy.data.images.new(name, res, res, alpha=False)
    img.colorspace_settings.name = "sRGB" if srgb else "Non-Color"
    return img


def add_bake_target(mat, image):
    nt = mat.node_tree
    node = nt.nodes.new("ShaderNodeTexImage")
    node.name = "BAKE_TARGET"
    node.image = image
    node.select = True
    nt.nodes.active = node
    return node


def bake(ob, image, bake_type="COMBINED", samples=64, margin=12, source=None, cage_extrusion=0.6, ray_distance=3.0):
    sc = bpy.context.scene
    sc.cycles.samples = samples
    sc.cycles.use_denoising = False
    sc.render.bake.margin = margin
    sc.render.bake.margin_type = "EXTEND"
    sc.render.bake.use_clear = True
    for m in ob.data.materials:
        add_bake_target(m, image)
    if source is not None:
        select_only([source, ob])
        bpy.context.view_layer.objects.active = ob
        sc.render.bake.use_selected_to_active = True
        sc.render.bake.cage_extrusion = cage_extrusion
        sc.render.bake.max_ray_distance = ray_distance
    else:
        select_only([ob])
        sc.render.bake.use_selected_to_active = False
    if bake_type == "DIFFUSE":
        sc.render.bake.use_pass_direct = False
        sc.render.bake.use_pass_indirect = False
        sc.render.bake.use_pass_color = True
    else:
        sc.render.bake.use_pass_direct = True
        sc.render.bake.use_pass_indirect = True
        sc.render.bake.use_pass_color = True
    log(f"bake {ob.name} -> {image.name} ({image.size[0]}px, {bake_type}, {samples} spp, selected_to_active={source is not None})")
    bpy.ops.object.bake(type=bake_type)
    for m in ob.data.materials:
        n = m.node_tree.nodes.get("BAKE_TARGET")
        if n:
            m.node_tree.nodes.remove(n)


def save_image(image, path: Path, fmt="JPEG", quality=90):
    image.filepath_raw = str(path)
    image.file_format = fmt
    sc = bpy.context.scene
    sc.render.image_settings.file_format = fmt
    sc.render.image_settings.quality = quality
    image.save_render(str(path))


def baked_material(name, image):
    """Emission-only material: the exporter writes it as emissive, the viewer shows it unlit."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    tex = nt.nodes.new("ShaderNodeTexImage"); tex.image = image
    emis = nt.nodes.new("ShaderNodeEmission"); emis.inputs["Strength"].default_value = 1.0
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(tex.outputs["Color"], emis.inputs["Color"])
    nt.links.new(emis.outputs[0], out.inputs["Surface"])
    return m


def replace_materials(ob, mat):
    ob.data.materials.clear()
    ob.data.materials.append(mat)
    for p in ob.data.polygons:
        p.material_index = 0


# ----------------------------------------------------------------------------- decimate / clip


def decimate_to(ob, target_tris: int, planar_first=True):
    before = tri_count(ob)
    select_only([ob])
    if planar_first:
        m = ob.modifiers.new("Planar", "DECIMATE")
        m.decimate_type = "DISSOLVE"
        m.angle_limit = math.radians(3.0)
        bpy.ops.object.modifier_apply(modifier=m.name)
    mid = tri_count(ob)
    if mid > target_tris:
        m = ob.modifiers.new("Collapse", "DECIMATE")
        m.decimate_type = "COLLAPSE"
        m.ratio = max(0.01, target_tris / mid)
        m.use_collapse_triangulate = True
        bpy.ops.object.modifier_apply(modifier=m.name)
    after = tri_count(ob)
    log(f"decimate {ob.name}: {before} -> {mid} -> {after} tris (target {target_tris})")
    return before, after


def clip_to_square(ob, half: float):
    """Cut away everything outside |x|,|y| <= half using 4 bisect planes."""
    select_only([ob])
    bpy.ops.object.mode_set(mode="EDIT")
    for no, co in (((1, 0, 0), (half, 0, 0)), ((-1, 0, 0), (-half, 0, 0)),
                   ((0, 1, 0), (0, half, 0)), ((0, -1, 0), (0, -half, 0))):
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.bisect(plane_co=co, plane_no=no, clear_outer=True, use_fill=False)
    bpy.ops.object.mode_set(mode="OBJECT")


# ----------------------------------------------------------------------------- cameras / preview


def add_preset_cameras(half: float, focus_z=12.0, hero_xy=(25.0, 0.0)):
    """Six named cameras exported with the GLB; the viewer offers them as preset angles."""
    cams = []
    r = half * 1.9
    presets = {
        "View_SW": (-r * 0.75, -r * 0.75, half * 1.1),
        "View_SE": (r * 0.75, -r * 0.75, half * 1.1),
        "View_NE": (r * 0.75, r * 0.75, half * 1.1),
        "View_NW": (-r * 0.75, r * 0.75, half * 1.1),
        "View_Top": (0.0, -1.0, half * 3.2),
        "View_Street": (-half * 0.35, -half * 0.9, 2.0),
        "View_HeroN": (hero_xy[0] + 10, hero_xy[1] + 110, 40.0),
        "View_HeroPlaza": (hero_xy[0] - 70, hero_xy[1] + 25, 3.0),
    }
    for name, loc in presets.items():
        cam = bpy.data.cameras.new(name)
        cam.lens = 32 if name != "View_Top" else 40
        cam.clip_end = 5000
        ob = link(bpy.data.objects.new(name, cam))
        ob.location = loc
        target = Vector((0, 0, focus_z if name != "View_Street" else 2.0))  # street: look horizontally (OrbitControls clamps polar angle)
        if name.startswith("View_Hero"):
            target = Vector((hero_xy[0], hero_xy[1], 14.0 if name == "View_HeroN" else 3.0))
        ob.rotation_euler = (target - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
        cams.append(ob)
    bpy.context.scene.camera = cams[0]
    return cams


def render_previews(cams, out_dir: Path, size=960, samples=8):
    """Cycles render of the baked (emission-only) scene: what the web viewer will show."""
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.samples = samples
    sc.cycles.use_denoising = False
    sc.render.resolution_x = size
    sc.render.resolution_y = int(size * 0.62)
    sc.render.resolution_percentage = 100
    sc.render.image_settings.file_format = "PNG"
    sc.view_settings.view_transform = "Standard"
    if sc.world:
        nt = sc.world.node_tree
        nt.nodes.clear()
        bg = nt.nodes.new("ShaderNodeBackground")
        bg.inputs["Color"].default_value = (0.72, 0.80, 0.90, 1)
        out = nt.nodes.new("ShaderNodeOutputWorld")
        nt.links.new(bg.outputs[0], out.inputs["Surface"])
    for o in bpy.data.objects:
        if o.type == "LIGHT":
            o.hide_render = True
    for cam in cams:
        sc.camera = cam
        sc.render.filepath = str(out_dir / f"preview_{cam.name}.png")
        bpy.ops.render.render(write_still=True)
        log(f"preview {cam.name}")


# ----------------------------------------------------------------------------- google3d import


def import_google_tiles(tiles_dir: Path, site: dict):
    """Import GLB tiles written by fetch_google3d.py and move them into the ENU frame."""
    from geo import enu_matrix
    manifest = json.loads((tiles_dir / "manifest.json").read_text())
    M_enu = Matrix(enu_matrix(*site["origin"]))
    # 3D Tiles glTF content is Y-up; tiles are in ECEF Z-up: rotate X by +90deg.
    yup_to_zup = Matrix.Rotation(math.radians(90.0), 4, "X")
    imported = []
    for t in manifest["tiles"]:
        before = set(bpy.data.objects)
        bpy.ops.import_scene.gltf(filepath=str(tiles_dir / t["file"]), merge_vertices=True)
        new = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
        rtc = Vector(t.get("rtc_center") or (0, 0, 0))
        T = Matrix(t["transform"]) if t.get("transform") else Matrix.Identity(4)
        for o in new:
            # importer already converted glTF Y-up to Blender Z-up, i.e. applied yup_to_zup.
            # World ECEF = T * (rtc + yup_to_zup * v_blender)   -> then ENU.
            o.matrix_world = M_enu @ T @ Matrix.Translation(rtc) @ o.matrix_world
            imported.append(o)
        for o in [o for o in bpy.data.objects if o not in before and o.type != "MESH"]:
            bpy.data.objects.remove(o)
    log(f"imported {len(imported)} meshes from {len(manifest['tiles'])} tiles")
    return imported


# ----------------------------------------------------------------------------- main


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", type=Path, required=True, help="site.json from fetch_opendata.py")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--mode", choices=["opendata", "google3d"], default="opendata")
    ap.add_argument("--tiles", type=Path, help="google3d: directory with manifest.json + *.glb")
    ap.add_argument("--bake-res", type=int, default=4096)
    ap.add_argument("--ground-res", type=int, default=4096)
    ap.add_argument("--samples", type=int, default=64)
    ap.add_argument("--target-tris", type=int, default=80000)
    ap.add_argument("--no-preview", action="store_true")
    ap.add_argument("--save-blend", action="store_true")
    ap.add_argument("--gpu", action="store_true", help="try OptiX/CUDA for baking")
    ap.add_argument("--hero-lat", type=float, default=25.07413, help="hero building point (default: Delta HQ POI)")
    ap.add_argument("--hero-lon", type=float, default=121.57772)
    ap.add_argument("--hero-res", type=int, default=4096)
    ap.add_argument("--no-hero", action="store_true")
    a = ap.parse_args(argv)

    site = json.loads(a.site.read_text(encoding="utf-8"))
    site_dir = a.site.parent
    a.out.mkdir(parents=True, exist_ok=True)
    half = float(site["half_size_m"])
    stats = {"mode": a.mode, "half_size_m": half, "origin": site["origin"]}

    reset_scene()
    stats["device"] = enable_gpu() if a.gpu else "CPU"
    setup_lighting()

    if a.mode == "opendata":
        ground = build_ground(site, site_dir)
        hero_b = None if a.no_hero else find_hero(site, a.hero_lat, a.hero_lon)
        facades = {"office": facade_material("FacadeOffice"), "glass": facade_glass_material("FacadeGlass"),
                   "apartment": facade_apartment_material("FacadeApartment")}
        roof = roof_material("Roof")
        bobjs = build_buildings(site, facades, roof, skip_ids=(hero_b["id"],) if hero_b else ())
        buildings = join_objects(bobjs, "Buildings")
        export_objs = [buildings, ground]
        hero = None
        if hero_b:
            from hero import build_hero
            hero = build_hero(hero_b["outer"], hero_b["height"])
            stats["hero"] = {"osm_id": hero_b["id"], "height_m": hero_b["height"], "tris": tri_count(hero),
                             "floors": hero["floors"], "pv_panels": hero["pv_panels"]}
            log(f"hero {hero_b['id']}: {tri_count(hero)} tris, {hero['floors']} floors, {hero['pv_panels']} PV panels")
            export_objs.insert(0, hero)
        stats["tris_before"] = sum(tri_count(o) for o in export_objs)
        # bake: hero (own atlas), LOD1 buildings (shared atlas), ground (ortho + shadows)
        smart_uv(buildings)
        img_b = new_bake_image("bake_buildings", a.bake_res)
        bake(buildings, img_b, "COMBINED", a.samples)
        if hero:
            smart_uv(hero, angle=60.0, margin=0.002)
            img_h = new_bake_image("bake_hero", a.hero_res)
            bake(hero, img_h, "COMBINED", a.samples)
        img_g = new_bake_image("bake_ground", a.ground_res)
        bake(ground, img_g, "COMBINED", a.samples)
        save_image(img_b, a.out / "bake_buildings.jpg")
        save_image(img_g, a.out / "bake_ground.jpg")
        replace_materials(buildings, baked_material("BuildingsBaked", img_b))
        replace_materials(ground, baked_material("GroundBaked", img_g))
        if hero:
            save_image(img_h, a.out / "bake_hero.jpg")
            replace_materials(hero, baked_material("HeroBaked", img_h))
            decimate_to(hero, a.target_tris // 2, planar_first=False)
        decimate_to(buildings, a.target_tris // 2, planar_first=False)
    else:
        parts = import_google_tiles(a.tiles, site)
        hi = join_objects(parts, "Tiles_hi")
        clip_to_square(hi, half * 1.02)
        stats["tris_before"] = tri_count(hi)
        # low-poly copy for the web, baked from the high-poly original textures
        select_only([hi])
        bpy.ops.object.duplicate()
        lo = bpy.context.view_layer.objects.active
        lo.name = "Terrain"
        decimate_to(lo, a.target_tris)
        smart_uv(lo, angle=60.0, margin=0.003)
        img = new_bake_image("bake_terrain", a.bake_res)
        bake(lo, img, "DIFFUSE", max(16, a.samples // 4), source=hi)
        save_image(img, a.out / "bake_terrain.jpg")
        replace_materials(lo, baked_material("TerrainBaked", img))
        hi.hide_render = True
        hi.hide_viewport = True
        bpy.data.objects.remove(hi)
        export_objs = [lo]

    hero_xy = (25.0, 0.0)
    if stats.get("hero"):
        hb = next(b for b in site["buildings"] if b["id"] == stats["hero"]["osm_id"])
        hero_xy = (sum(p[0] for p in hb["outer"]) / len(hb["outer"]), sum(p[1] for p in hb["outer"]) / len(hb["outer"]))
    cams = add_preset_cameras(half, hero_xy=hero_xy)
    stats["tris_after"] = sum(tri_count(o) for o in export_objs)
    stats["objects"] = [o.name for o in export_objs]

    select_only(export_objs + cams)
    glb = a.out / "scene.glb"
    bpy.ops.export_scene.gltf(
        filepath=str(glb), export_format="GLB", use_selection=True, export_apply=True,
        export_yup=True, export_cameras=True, export_lights=False, export_materials="EXPORT",
        export_image_format="JPEG", export_jpeg_quality=86, export_texcoords=True, export_normals=True,
        export_extras=True,
    )
    stats["glb_bytes"] = glb.stat().st_size
    stats["cameras"] = [c.name for c in cams]
    stats["views"] = []
    for c in cams:
        fwd = c.matrix_world.to_quaternion() @ Vector((0, 0, -1))
        pos = c.matrix_world.translation
        t = (-pos.z / fwd.z) if fwd.z < -0.05 else 120.0
        tgt = pos + fwd * min(t, 900.0)
        to_gltf = lambda v: [round(v.x, 2), round(v.z, 2), round(-v.y, 2)]  # Blender Z-up -> glTF Y-up
        stats["views"].append({"name": c.name.replace("View_", ""), "pos": to_gltf(pos), "target": to_gltf(tgt),
                               "fov": round(math.degrees(2 * math.atan(c.data.sensor_width / 2 / c.data.lens)), 1)})
    stats["attribution"] = site.get("attribution", [])
    stats["seconds"] = round(time.time() - T0, 1)
    (a.out / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"exported {glb} ({stats['glb_bytes'] / 1e6:.1f} MB), tris {stats.get('tris_before')} -> {stats['tris_after']}")
    if a.save_blend:
        bpy.ops.wm.save_as_mainfile(filepath=str(a.out / "scene.blend"))
    if not a.no_preview:
        render_previews(cams, a.out)
    return stats


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    main(argv)
