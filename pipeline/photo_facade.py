"""Cylindrical rectification of the curved curtain wall in the Commons photo of the Delta HQ.

The glass arc is treated as a vertical cylinder segment seen roughly from its axis direction:
  * horizontally, photo x is proportional to sin(angle) across the arc (chord projection),
  * vertically, for each photo column the glass spans from a picked TOP curve to a picked BOTTOM curve.
Output: an unwrapped facade texture whose U runs along the arc (photo left -> right) and V from the
bottom band (z_bot) to the parapet (z_top). Picks below are in the 1920x1440 rendition; they are
scaled to the actual image size.

Photo: "Delta Electronics headquarters 20110201.jpg" by Solomon203, Wikimedia Commons, CC BY-SA 3.0.
"""
from __future__ import annotations

import math
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
except ImportError:  # constants (Z_TOP/Z_BOT) are still importable inside Blender
    np = Image = None

PICK_W = 1920
# (x, y) along the top parapet of the glass block, left (east end) -> right (west end)
TOP = [(195, 748), (300, 716), (400, 692), (500, 680), (600, 673), (700, 670), (800, 672), (900, 680),
       (1000, 691), (1060, 698)]
# the lowest fully visible band (top of the ground floor)
BOT = [(195, 1232), (300, 1228), (400, 1218), (500, 1212), (600, 1207), (700, 1205), (800, 1205), (900, 1203),
       (1000, 1195), (1060, 1190)]
X_LEFT, X_RIGHT = 195, 1060
HALF_ANGLE_DEG = 48.0      # half of the arc's angular span as seen in the photo (circle fit: 60..159 deg = 99 deg)
Z_TOP, Z_BOT = 24.0, 3.4   # metres: parapet of the glass block, top of the ground floor


def _interp(curve, x):
    xs = np.array([p[0] for p in curve], float); ys = np.array([p[1] for p in curve], float)
    return float(np.interp(x, xs, ys))


def rectify(photo: Path, out: Path, width=2048, height=768, half_angle_deg=HALF_ANGLE_DEG) -> Path:
    im = Image.open(photo).convert("RGB")
    sx = im.width / PICK_W
    src = np.asarray(im, dtype=np.float32)
    H, W = src.shape[:2]
    A = math.radians(half_angle_deg)
    xl, xr = X_LEFT * sx, X_RIGHT * sx
    xm, half = (xl + xr) / 2, (xr - xl) / 2
    u = (np.arange(width) + 0.5) / width
    alpha = -A + u * 2 * A
    xs = xm + half * np.sin(alpha) / math.sin(A)          # photo column for each output column
    top = np.array([_interp(TOP, x / sx) * sx for x in xs])
    bot = np.array([_interp(BOT, x / sx) * sx for x in xs])
    v = (np.arange(height) + 0.5) / height                 # 0 = bottom band, 1 = parapet
    out_img = np.zeros((height, width, 3), np.float32)
    for j in range(height):
        ys = bot + (top - bot) * v[j]
        x0 = np.clip(np.floor(xs).astype(int), 0, W - 2); y0 = np.clip(np.floor(ys).astype(int), 0, H - 2)
        fx = (xs - x0)[:, None]; fy = (ys - y0)[:, None]
        p = (src[y0, x0] * (1 - fx) * (1 - fy) + src[y0, x0 + 1] * fx * (1 - fy)
             + src[y0 + 1, x0] * (1 - fx) * fy + src[y0 + 1, x0 + 1] * fx * fy)
        out_img[height - 1 - j] = p                      # image row 0 = top (v = 1)
    Image.fromarray(np.clip(out_img, 0, 255).astype(np.uint8)).save(out, quality=92)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--photo", type=Path, default=Path(__file__).parent / "data" / "delta_hq_commons_2011.jpg")
    ap.add_argument("--out", type=Path, default=Path("build/facade_arc.jpg"))
    a = ap.parse_args()
    print(rectify(a.photo, a.out))
