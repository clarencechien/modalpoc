"""Photo-project Street View panoramas onto the hero building and bake the result.

For every panorama i (equirectangular, centre column = compass heading h_i, camera at P_i):
  d  = normalize(Position - P_i)
  d' = Rz(h_i - 90deg) * d           -> Blender's Environment Texture equirect convention (centre = +X)
  c_i = pano_i(d')
  w_i = max(0, dot(N, -d)) / (dist + 6)   (front-facing, near panos win)
colour = sum(w_i c_i) / sum(w_i), falling back to the procedural material where no pano
faces the surface. The result is baked (DIFFUSE colour) into the hero's atlas.

Self-occlusion is not resolved (a facade hidden from a pano still receives its pixels),
which is acceptable for an experiment on a mostly convex building.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import bpy
from mathutils import Vector

from geo import lla_to_enu

CAM_HEIGHT = {"google": 2.6, "user": 1.7}


def load_panos(sv_dir: Path, origin, max_panos=8, centre=(0.0, 0.0), max_dist=95.0, google_only=True):
    panos = json.loads((sv_dir / "panos.json").read_text(encoding="utf-8"))
    out = []
    for p in panos:
        x, y, _ = lla_to_enu(p["lat"], p["lng"], 0.0, tuple(origin))
        d = math.hypot(x - centre[0], y - centre[1])
        if d > max_dist:
            continue
        src = "google" if "Google" in (p.get("copyright") or "") else "user"
        if google_only and src != "google":
            continue
        out.append({**p, "x": x, "y": y, "z": CAM_HEIGHT[src], "src": src, "dist": d})
    out.sort(key=lambda p: (p["src"] != "google", p["dist"]))
    return out[:max_panos]


def projection_material(name: str, panos: list[dict], sv_dir: Path, fallback_color_socket=None, base_mat=None):
    """Material whose Base Color is the weighted pano projection (see module doc)."""
    m = base_mat or bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None) or nt.nodes.new("ShaderNodeBsdfPrincipled")
    geo = nt.nodes.new("ShaderNodeNewGeometry")

    def vmath(op, a, b=None, scale=None):
        n = nt.nodes.new("ShaderNodeVectorMath"); n.operation = op
        nt.links.new(a, n.inputs[0])
        if b is not None:
            if isinstance(b, tuple):
                n.inputs[1].default_value = b
            else:
                nt.links.new(b, n.inputs[1])
        if scale is not None:
            n.inputs["Scale"].default_value = scale
        return n.outputs[0] if op not in ("DOT_PRODUCT", "LENGTH", "DISTANCE") else n.outputs["Value"]

    def math_(op, a, b=None, clamp=False):
        n = nt.nodes.new("ShaderNodeMath"); n.operation = op; n.use_clamp = clamp
        if isinstance(a, (int, float)):
            n.inputs[0].default_value = a
        else:
            nt.links.new(a, n.inputs[0])
        if b is not None:
            if isinstance(b, (int, float)):
                n.inputs[1].default_value = b
            else:
                nt.links.new(b, n.inputs[1])
        return n.outputs[0]

    weighted_sum = None
    weight_sum = None
    for p in panos:
        img = bpy.data.images.load(str(sv_dir / p["file"]), check_existing=True)
        img.colorspace_settings.name = "sRGB"
        P = (p["x"], p["y"], p["z"])
        d_raw = vmath("SUBTRACT", geo.outputs["Position"], P)               # Position - P
        dist = vmath("LENGTH", d_raw)
        d = vmath("NORMALIZE", d_raw)
        rot = nt.nodes.new("ShaderNodeVectorRotate"); rot.rotation_type = "AXIS_ANGLE"
        rot.inputs["Axis"].default_value = (0, 0, 1)
        # Blender equirect: centre = +X, u increases clockwise (Cycles: u = 0.5 - atan2(y, x)/2pi).
        # Street View: centre = heading, u increases clockwise -> rotate so the heading maps to +X.
        rot.inputs["Angle"].default_value = math.radians(p["heading"]) - math.pi / 2
        nt.links.new(d, rot.inputs["Vector"])
        env = nt.nodes.new("ShaderNodeTexEnvironment"); env.image = img; env.projection = "EQUIRECTANGULAR"
        nt.links.new(rot.outputs[0], env.inputs["Vector"])
        # weight: facing * proximity * not-too-steep (avoid the sky/car regions of the pano)
        facing = math_("MAXIMUM", vmath("DOT_PRODUCT", geo.outputs["True Normal"], vmath("SCALE", d, scale=-1.0)), 0.0)
        # sharp weights: the nearest, most frontal pano dominates (blending many panos ghosts)
        prox = math_("POWER", math_("DIVIDE", 1.0, math_("ADD", dist, 3.0)), 3.0)
        sep = nt.nodes.new("ShaderNodeSeparateXYZ"); nt.links.new(d, sep.inputs[0])
        steep = math_("LESS_THAN", math_("ABSOLUTE", sep.outputs["Z"]), 0.92)
        w = math_("MULTIPLY", math_("MULTIPLY", math_("POWER", facing, 3.0), prox), steep)
        wc = vmath("SCALE", env.outputs["Color"], w)
        weighted_sum = wc if weighted_sum is None else vmath("ADD", weighted_sum, wc)
        weight_sum = w if weight_sum is None else math_("ADD", weight_sum, w)
    colour = vmath("SCALE", weighted_sum, math_("DIVIDE", 1.0, math_("MAXIMUM", weight_sum, 1e-6)))
    # blend to the fallback where the total weight is tiny
    cover = math_("MINIMUM", math_("MULTIPLY", weight_sum, 40.0 * 27.0 * 30.0), 1.0, clamp=True)
    mix = nt.nodes.new("ShaderNodeMix"); mix.data_type = "RGBA"
    nt.links.new(cover, mix.inputs["Factor"])
    if fallback_color_socket is not None:
        nt.links.new(fallback_color_socket, mix.inputs[6])
    else:
        mix.inputs[6].default_value = (0.3, 0.3, 0.3, 1)
    nt.links.new(colour, mix.inputs[7])
    nt.links.new(mix.outputs[2], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.6
    bsdf.inputs["Metallic"].default_value = 0.0
    return m


def apply_to_hero(hero, panos: list[dict], sv_dir: Path):
    """Rewire every hero material so its Base Color comes from the pano projection,
    keeping the original procedural colour as the fallback."""
    for m in hero.data.materials:
        nt = m.node_tree
        bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is None:
            continue
        link = next((l for l in nt.links if l.to_socket == bsdf.inputs["Base Color"]), None)
        fallback = link.from_socket if link else None
        if link:
            nt.links.remove(link)
        if fallback is None:
            rgb = nt.nodes.new("ShaderNodeRGB"); rgb.outputs[0].default_value = bsdf.inputs["Base Color"].default_value
            fallback = rgb.outputs[0]
        projection_material(m.name, panos, sv_dir, fallback_color_socket=fallback, base_mat=m)
