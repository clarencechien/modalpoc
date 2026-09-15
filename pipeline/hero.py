"""Hero building: a higher-detail procedural model of the Delta Electronics HQ
(瑞光路 186 號) built on its OSM footprint, plus the plaza pavilion.

Everything is metres in the site ENU frame. Detail is geometric (floor slabs,
parapet, rooftop PV arrays, plant rooms, green roof, glass pavilion) so that the
COMBINED bake picks up real shadows and reflections; the rest of the block stays LOD1.
"""
from __future__ import annotations

import math
import random

import bpy
import bmesh
from mathutils import Vector

FLOOR_H = 3.4
SLAB_H = 0.7
SLAB_OUT = 0.55


def point_in_poly(x, y, poly) -> bool:
    c = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def poly_centroid(poly):
    return (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))


# ----------------------------------------------------------------------------- materials


def _mat(name, base=(0.5, 0.5, 0.5), rough=0.6, metal=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*base, 1)
    b.inputs["Roughness"].default_value = rough
    b.inputs["Metallic"].default_value = metal
    return m


def glass_material(name="HeroGlass"):
    """Teal-blue curtain wall with vertical mullions every 1.5 m (world XY) and a
    horizontal transom per floor; slightly reflective so the sky bakes into it."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(geo.outputs["Position"], sep.inputs[0])

    def stripe(inp, period, width):
        d = nt.nodes.new("ShaderNodeMath"); d.operation = "DIVIDE"; d.inputs[1].default_value = period
        f = nt.nodes.new("ShaderNodeMath"); f.operation = "FRACT"
        lt = nt.nodes.new("ShaderNodeMath"); lt.operation = "LESS_THAN"; lt.inputs[1].default_value = width
        nt.links.new(inp, d.inputs[0]); nt.links.new(d.outputs[0], f.inputs[0]); nt.links.new(f.outputs[0], lt.inputs[0])
        return lt.outputs[0]

    nsep = nt.nodes.new("ShaderNodeSeparateXYZ"); nt.links.new(geo.outputs["True Normal"], nsep.inputs[0])
    m1 = nt.nodes.new("ShaderNodeMath"); m1.operation = "MULTIPLY"; nt.links.new(sep.outputs["X"], m1.inputs[0]); nt.links.new(nsep.outputs["Y"], m1.inputs[1])
    m2 = nt.nodes.new("ShaderNodeMath"); m2.operation = "MULTIPLY"; nt.links.new(sep.outputs["Y"], m2.inputs[0]); nt.links.new(nsep.outputs["X"], m2.inputs[1])
    along = nt.nodes.new("ShaderNodeMath"); along.operation = "SUBTRACT"; nt.links.new(m2.outputs[0], along.inputs[0]); nt.links.new(m1.outputs[0], along.inputs[1])
    mull = stripe(along.outputs[0], 1.5, 0.06)
    trans = stripe(sep.outputs["Z"], FLOOR_H, 0.08)
    frame = nt.nodes.new("ShaderNodeMath"); frame.operation = "MAXIMUM"
    nt.links.new(mull, frame.inputs[0]); nt.links.new(trans, frame.inputs[1])
    glass = nt.nodes.new("ShaderNodeRGB"); glass.outputs[0].default_value = (0.05, 0.14, 0.17, 1)
    alu = nt.nodes.new("ShaderNodeRGB"); alu.outputs[0].default_value = (0.55, 0.57, 0.58, 1)
    mix = nt.nodes.new("ShaderNodeMix"); mix.data_type = "RGBA"
    nt.links.new(frame.outputs[0], mix.inputs["Factor"]); nt.links.new(glass.outputs[0], mix.inputs[6]); nt.links.new(alu.outputs[0], mix.inputs[7])
    nt.links.new(mix.outputs[2], bsdf.inputs["Base Color"])
    rough = nt.nodes.new("ShaderNodeMix"); rough.data_type = "FLOAT"; rough.inputs[2].default_value = 0.08; rough.inputs[3].default_value = 0.5
    nt.links.new(frame.outputs[0], rough.inputs["Factor"]); nt.links.new(rough.outputs[0], bsdf.inputs["Roughness"])
    bsdf.inputs["Metallic"].default_value = 0.35
    bsdf.inputs["Specular IOR Level"].default_value = 0.8
    return m


def pv_material(name="HeroPV"):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(geo.outputs["Position"], sep.inputs[0])
    cells = []
    for axis, period in (("X", 0.16), ("Y", 0.16)):
        d = nt.nodes.new("ShaderNodeMath"); d.operation = "DIVIDE"; d.inputs[1].default_value = period
        f = nt.nodes.new("ShaderNodeMath"); f.operation = "FRACT"
        lt = nt.nodes.new("ShaderNodeMath"); lt.operation = "LESS_THAN"; lt.inputs[1].default_value = 0.1
        nt.links.new(sep.outputs[axis], d.inputs[0]); nt.links.new(d.outputs[0], f.inputs[0]); nt.links.new(f.outputs[0], lt.inputs[0])
        cells.append(lt.outputs[0])
    mx = nt.nodes.new("ShaderNodeMath"); mx.operation = "MAXIMUM"
    nt.links.new(cells[0], mx.inputs[0]); nt.links.new(cells[1], mx.inputs[1])
    dark = nt.nodes.new("ShaderNodeRGB"); dark.outputs[0].default_value = (0.02, 0.04, 0.10, 1)
    line = nt.nodes.new("ShaderNodeRGB"); line.outputs[0].default_value = (0.7, 0.7, 0.72, 1)
    mix = nt.nodes.new("ShaderNodeMix"); mix.data_type = "RGBA"
    nt.links.new(mx.outputs[0], mix.inputs["Factor"]); nt.links.new(dark.outputs[0], mix.inputs[6]); nt.links.new(line.outputs[0], mix.inputs[7])
    nt.links.new(mix.outputs[2], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.15
    bsdf.inputs["Metallic"].default_value = 0.2
    return m


def green_material(name="HeroGreenRoof"):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    noise = nt.nodes.new("ShaderNodeTexNoise"); noise.inputs["Scale"].default_value = 3.0; noise.inputs["Detail"].default_value = 6
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (0.10, 0.22, 0.07, 1)
    ramp.color_ramp.elements[1].color = (0.30, 0.42, 0.14, 1)
    nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"]); nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.95
    return m


def hero_materials():
    return [
        glass_material(),                                   # 0 curtain wall
        _mat("HeroSlab", (0.60, 0.59, 0.56), 0.75),          # 1 floor slabs / parapet
        _mat("HeroRoof", (0.36, 0.36, 0.35), 0.9),           # 2 roof deck
        pv_material(),                                      # 3 PV panels
        _mat("HeroMech", (0.62, 0.63, 0.64), 0.6, 0.3),      # 4 plant rooms / AHUs
        green_material(),                                   # 5 green roof
        _mat("HeroPlaza", (0.66, 0.65, 0.62), 0.6),          # 6 pavilion roof
    ]


# ----------------------------------------------------------------------------- geometry helpers


def _extrude_up(bm, faces, dz, mat=None):
    """Extrude faces by dz along +Z; returns the new (top + side) faces."""
    before = set(bm.faces)
    res = bmesh.ops.extrude_face_region(bm, geom=faces)
    verts = [g for g in res["geom"] if isinstance(g, bmesh.types.BMVert)]
    bmesh.ops.translate(bm, verts=verts, vec=(0, 0, dz))
    new = [f for f in bm.faces if f not in before]
    if mat is not None:
        for f in new:
            f.material_index = mat
    return new


def _add_prism(bm, poly, z0, h, mat, outset=0.0, ring=0.0):
    """Closed prism on polygon `poly` from z0 to z0+h.
    outset: grow the polygon outwards by this much first (floor slabs, canopies).
    ring:   keep only a ring of this width along the outline (parapets)."""
    vs = [bm.verts.new((x, y, z0)) for x, y in poly]
    f = bm.faces.new(vs)
    faces = [f]
    if outset > 0:
        r = bmesh.ops.inset_region(bm, faces=[f], thickness=outset, depth=0.0, use_outset=True, use_even_offset=True)
        faces = [f] + r["faces"]
    if ring > 0:
        r = bmesh.ops.inset_region(bm, faces=[f], thickness=ring, depth=0.0, use_even_offset=True)
        bmesh.ops.delete(bm, geom=[f], context="FACES_ONLY")
        faces = [g for g in faces if g is not f] + r["faces"]
    for g in faces:
        g.material_index = mat
    return _extrude_up(bm, faces, h, mat)


def _add_box(bm, cx, cy, z0, sx, sy, sz, mat, rot=0.0, tilt=0.0):
    """Axis box with optional yaw (rot) and pitch (tilt, about local X) — used for PV panels."""
    hx, hy = sx / 2, sy / 2
    corners = [(-hx, -hy, 0), (hx, -hy, 0), (hx, hy, 0), (-hx, hy, 0), (-hx, -hy, sz), (hx, -hy, sz), (hx, hy, sz), (-hx, hy, sz)]
    ct, st = math.cos(tilt), math.sin(tilt)
    cr, sr = math.cos(rot), math.sin(rot)
    vs = []
    for x, y, z in corners:
        y, z = y * ct - z * st, y * st + z * ct
        x, y = x * cr - y * sr, x * sr + y * cr
        vs.append(bm.verts.new((cx + x, cy + y, z0 + z)))
    idx = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    faces = []
    for a, b, c, d in idx:
        f = bm.faces.new((vs[a], vs[b], vs[c], vs[d]))
        f.material_index = mat
        faces.append(f)
    return faces


# ----------------------------------------------------------------------------- the hero


def build_hero(footprint: list[list[float]], height: float, name="Hero", seed=7, pavilion=(-5.0, 0.0, 9.0, 5.0)):
    rng = random.Random(seed)
    poly = [tuple(p) for p in footprint]
    n_floors = max(3, int(round(height / FLOOR_H)))
    H = n_floors * FLOOR_H
    bm = bmesh.new()

    # 1) glass mass (full height, no outset)
    _add_prism(bm, poly, 0.0, H, 0)
    # 2) floor slabs projecting past the glass, and the roof slab
    for k in range(1, n_floors + 1):
        _add_prism(bm, poly, k * FLOOR_H - SLAB_H, SLAB_H, 1, outset=SLAB_OUT)
    # 3) parapet ring on the roof (0.35 m outside the glass line, 0.4 m wide, 1.1 m high)
    _add_prism(bm, poly, H, 1.1, 1, outset=0.35, ring=0.75)
    # roof deck: fill the parapet interior
    deck = bm.faces.new([bm.verts.new((x, y, H + 0.02)) for x, y in poly])
    deck.material_index = 2

    # 4) rooftop programme placed on the deck: PV arrays, plant rooms, green roof
    cx, cy = poly_centroid(poly)
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)

    def on_deck(x, y, margin=2.0):
        # inside the footprint with a margin from the edge
        return point_in_poly(x, y, poly) and all(point_in_poly(x + dx, y + dy, poly) for dx, dy in ((margin, 0), (-margin, 0), (0, margin), (0, -margin)))

    # PV: rows running east-west, panels 1.0 x 2.0 tilted 15 deg towards the south, in the eastern half
    pv = 0
    pitch_y, pitch_x = 3.0, 1.05
    y = miny + 6
    while y < maxy - 4:
        x = cx + 2
        while x < maxx - 4:
            if on_deck(x, y, 2.5):
                _add_box(bm, x, y, H + 0.6, 1.0, 2.0, 0.06, 3, rot=0.0, tilt=math.radians(15))
                # support frame
                _add_box(bm, x, y - 0.9, H + 0.05, 0.08, 0.08, 0.55, 4)
                pv += 1
            x += pitch_x
        y += pitch_y
    # plant rooms / AHUs in the western half
    for _ in range(4):
        for _try in range(20):
            x, y = rng.uniform(minx + 6, cx - 3), rng.uniform(miny + 6, maxy - 6)
            if on_deck(x, y, 3.5):
                _add_box(bm, x, y, H + 0.02, rng.uniform(3, 6), rng.uniform(2.5, 4), rng.uniform(1.6, 3.0), 4, rot=0.0)
                break
    # stair / lift overrun
    if on_deck(cx - 8, cy, 4):
        _add_box(bm, cx - 8, cy, H + 0.02, 7.0, 5.0, 3.2, 1)
    # green roof strip along the south edge
    gy = miny + 3.5
    gpts = [(x, y) for x, y in ((minx + 8, gy), (cx - 2, gy), (cx - 2, gy + 4.0), (minx + 8, gy + 4.0)) if on_deck(x, y, 1.0)]
    if len(gpts) == 4:
        f = bm.faces.new([bm.verts.new((x, y, H + 0.08)) for x, y in gpts])
        f.material_index = 5

    # 5) plaza pavilion: glass cylinder with a flat roof disc
    px, py, pr, ph = pavilion
    ring = [(px + pr * math.cos(2 * math.pi * i / 40), py + pr * math.sin(2 * math.pi * i / 40)) for i in range(40)]
    _add_prism(bm, ring, 0.0, ph, 0)
    _add_prism(bm, ring, ph, 0.5, 6, outset=0.8)
    # ground-floor canopy over the main entrance (north side, towards the plaza)
    _add_prism(bm, poly, FLOOR_H - 0.35, 0.35, 1, outset=2.2)

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bmesh.ops.triangulate(bm, faces=[f for f in bm.faces if len(f.verts) > 4], ngon_method="EAR_CLIP")
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    for m in hero_materials():
        ob.data.materials.append(m)
    ob["hero"] = True
    ob["pv_panels"] = pv
    ob["floors"] = n_floors
    return ob
