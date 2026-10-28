#!/usr/bin/env python3
"""stencil.py - raster image -> bridged, cut-ready stencil layers.

Deterministic: the same image + parameters always gives the same files.

Outputs (in --out DIR):
  layer_N.svg / layer_N.dxf   one file per stencil layer (mm units)
  combined.svg / combined.dxf all layers side by side, one LightBurn color per layer
  stencil_print.pdf           true-scale pages (tiled or fit to one sheet) for hand cutting
  preview.png                 annotated preview (bridge IDs, islands, violations, paint simulation)
  report.json                 measured checks; "ok": true only if every layer passes
  params_used.json            every effective parameter - re-run with --params to reproduce

Analyze mode (proposes a color count, makes nothing to cut):
  python stencil.py IMAGE --out DIR --analyze      -> analysis.json + options.png

Usage:
  python stencil.py IMAGE --out DIR [--params params.json] [--set key=value ...]
"""
__version__ = "0.2.0"
import argparse, json, math, os, sys
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont
import shapely
import shapely.ops
import shapely.affinity
from shapely.geometry import Polygon, MultiPolygon, LineString, Point, box
from shapely.ops import unary_union
from shapely.strtree import STRtree
from skimage.measure import find_contours

# ---------------------------------------------------------------- parameters
PROFILES = {
    "co2_mylar":  dict(min_bridge_mm=1.0, min_cut_mm=0.8, min_material_mm=0.8),
    "diode_card": dict(min_bridge_mm=1.5, min_cut_mm=1.0, min_material_mm=1.2),
    "hand_cut":   dict(min_bridge_mm=3.0, min_cut_mm=1.5, min_material_mm=2.0),
}
DEFAULTS = dict(
    profile="co2_mylar",
    mode="multi",             # "multi" (color layers) | "single" (one-color threshold)
    n_colors="auto",          # multi: clusters INCLUDING background, or "auto" (proposed; see --analyze)
    n_layers=None,            # alternative to n_colors: number of PAINT layers (background added if used)
    max_auto_colors=7,        # auto: largest color count considered
    delta_e_target=6.0,       # auto: stop adding colors once mean color error (dE76) is below this
    color_mode="color",       # "color" | "gray" (posterize by lightness only, layers are grays)
    palette=None,             # multi: list of hex colors to snap to instead of k-means
    paint_chart=None,         # CSV "name,hex[,brand]": report nearest paint per layer
    snap_to_paint=False,      # with paint_chart: use the paint's color as the layer color
    # ---- image prep
    crop=None,                # [x0,y0,x1,y1] in pixels, or fractions 0-1 of the image
    remove_background=False,  # AI subject cut-out (rembg; ~176 MB model downloaded on first use)
    bg_model="u2net",
    # ---- size (pick one way; later ones win): width_mm | width/height with units | fit
    width=None,               # "10in", "10 1/2in", "254mm", "25.4cm", "1200px"
    height=None,              # same units; with width too, the design fits inside the box
    dpi=96.0,                 # converts px sizes (96 = LightBurn / Inkscape / CSS default)
    fit=None,                 # paper name ("letter", "a4", ...) or "12x24in" sheet: fit to one sheet
    orientation="auto",       # fit: "auto" | "portrait" | "landscape"
    fit_includes_margin=True, # fit: the whole sheet (design + margin) must fit
    print_margin_mm=6.35,     # fit to a PAPER size: unprintable edge (1/4 in); sheets ("12x24in") use 0
    extra_clusters=2,         # multi: over-cluster by this many, then drop edge-blend colors
    min_color_share=0.003,    # multi: colors covering less of the image are dropped
    background="auto",        # "auto" (dominant border color) | "none" | "#rrggbb"
    threshold=None,           # single: 0-255 gray threshold, None = Otsu
    invert=False,             # single: paint the light areas instead of the dark
    layering="stacked",       # "stacked": layer i cuts colors i..top (fewer islands, hides bridges)
                              # "exact": layer i cuts only its own color (+ underlap)
    layer_order="auto",       # "auto" (fewest islands) | "light_to_dark" | list of hex colors bottom->top
    inversion_penalty=1.0,    # auto: cost (in islands) of spraying a much lighter color over a darker one
    underlap_mm=0.5,          # exact mode: grow each layer under later layers
    width_mm=200.0,           # width of the design area (image) in mm
    margin_mm=25.0,           # solid border around the design
    px_per_mm=8.0,            # working raster resolution
    denoise=True,             # edge-preserving blur before color separation
    smooth_mm=0.3,            # outline smoothing (gaussian sigma)
    simplify_mm=0.03,         # vector simplification tolerance
    enforce_minimums=True,    # vector pass that removes too-thin cuts/material before bridging
    min_area_mm2=None,        # specks smaller than this are dropped (default (2*min_cut)^2)
    min_island_mm2=None,      # material islands smaller than this are cut away (default (2*min_material)^2)
    bridges=True,             # master switch; False leaves islands floating (reported, numbered)
    bridge_layers="all",      # "all" or list of layer numbers that get bridges, e.g. [1, 3]
    bridge_width_mm=None,     # default = profile min_bridge_mm
    bridges_per_island=1,     # minimum bridges per island
    bridge_angle_deg=None,    # preferred bridge direction (0 = horizontal, 90 = vertical)
    bridge_angle_weight=1.0,  # strength of the preference
    hide_bridges=True,        # prefer bridges that later layers paint over
    keepout=[],               # [[x0,y0,x1,y1], ...] mm, design coords (origin = image top-left)
    bridge_overrides={},      # {"L1-B3": {"width_mm": 2}, "L1-B5": {"delete": true},
                              #  "L2-B1": {"move_to": [x, y]}, "L1-B7": {"off": true}}
                              # delete = "not here" (re-placed elsewhere); off = island floats
    extra_bridges=[],         # [{"layer": 1, "at": [x, y], "angle_deg": 90, "width_mm": 2}]
    reg_marks=True,           # plus-shaped registration cutouts in the margin corners
    reg_size_mm=10.0,
    combined_gap_mm=10.0,
    pdf_page="auto",          # "auto" (the fit paper, else letter) or any PAPER name
    pdf_overlap_mm=15.0,
)
# Paper sizes, portrait, mm
PAPER = {
    "letter": (215.9, 279.4), "legal": (215.9, 355.6), "tabloid": (279.4, 431.8), "ledger": (279.4, 431.8),
    "arch-a": (228.6, 304.8), "arch-b": (304.8, 457.2), "arch-c": (457.2, 609.6), "arch-d": (609.6, 914.4),
    "a5": (148.0, 210.0), "a4": (210.0, 297.0), "a3": (297.0, 420.0), "a2": (420.0, 594.0),
    "a1": (594.0, 841.0), "a0": (841.0, 1189.0), "b4": (250.0, 353.0), "b3": (353.0, 500.0),
}
# LightBurn layer palette (00-07). Layers get these stroke colors; reg marks and border their own.
LB_COLORS = ["#000000", "#0000FF", "#FF0000", "#00E000", "#D0D000", "#FF8000", "#00E0E0", "#FF00FF"]
REG_COLOR, BORDER_COLOR = "#00E0E0", "#FF00FF"   # LightBurn 06 / 07
MAX_CUT_LAYERS = 6
ACI = [7, 5, 1, 3, 2, 30, 4, 6]                  # DXF color index equivalents


def load_cfg(args):
    cfg = dict(DEFAULTS)
    user = {}
    if args.params:
        with open(args.params) as f:
            user.update(json.load(f))
    for kv in args.set or []:
        k, v = kv.split("=", 1)
        try:
            user[k] = json.loads(v)
        except json.JSONDecodeError:
            user[k] = v
    prof = user.get("profile", cfg["profile"])
    if prof not in PROFILES:
        sys.exit(f"unknown profile {prof}; choose {list(PROFILES)}")
    cfg.update(PROFILES[prof])
    cfg.update(user)
    if cfg["bridge_width_mm"] is None:
        cfg["bridge_width_mm"] = cfg["min_bridge_mm"]
    if cfg["min_area_mm2"] is None:
        cfg["min_area_mm2"] = (2 * cfg["min_cut_mm"]) ** 2
    if cfg["min_island_mm2"] is None:
        cfg["min_island_mm2"] = (2 * cfg["min_material_mm"]) ** 2
    if cfg["margin_mm"] <= 0:
        sys.exit("margin_mm must be > 0 (the border is what holds a stencil together)")
    cfg["_user"] = sorted(user)
    return cfg

# ---------------------------------------------------------------- sizing
UNIT_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4, '"': 25.4, "inch": 25.4, "inches": 25.4}

def parse_length(s, dpi):
    """'10in', '10 1/2 in', '3/4in', '254mm', '25.4cm', '1200px', or a bare number (mm) -> mm."""
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip().lower()
    import re
    m = re.fullmatch(r"([\d.]+)?(?:[\s-]*(\d+)/(\d+))?\s*(mm|cm|in|inch|inches|\"|px)?", t)
    if not m or (m.group(1) is None and m.group(2) is None):
        sys.exit(f"can't read length {s!r}; use e.g. 10in, 10 1/2in, 254mm, 25.4cm, 1200px")
    v = float(m.group(1) or 0) + (float(m.group(2)) / float(m.group(3)) if m.group(2) else 0)
    unit = m.group(4) or "mm"
    return v / dpi * 25.4 if unit == "px" else v * UNIT_MM[unit]

def parse_sheet(s, dpi):
    """paper name or 'WxH unit' -> ((w, h) mm portrait-agnostic, is_paper)."""
    k = str(s).strip().lower()
    if k in PAPER:
        return PAPER[k], True
    import re
    m = re.fullmatch(r"\s*([\d./\s-]+?)\s*[x×]\s*([\d./\s-]+?)\s*(mm|cm|in|\"|px)?\s*", k)
    if not m:
        sys.exit(f"unknown fit {s!r}; use a paper name {sorted(PAPER)} or e.g. 12x24in")
    u = m.group(3) or "mm"
    return (parse_length(m.group(1) + u, dpi), parse_length(m.group(2) + u, dpi)), False

def resolve_size(cfg, aspect):
    """Set cfg['width_mm'] (design width) from width/height/fit. aspect = image h / w."""
    dpi, M = cfg["dpi"], cfg["margin_mm"]
    how = "width_mm"
    if cfg["width"] is not None or cfg["height"] is not None:
        w = parse_length(cfg["width"], dpi) if cfg["width"] is not None else None
        h = parse_length(cfg["height"], dpi) if cfg["height"] is not None else None
        if w and h:
            cfg["width_mm"] = min(w, h / aspect)
            how = "fit inside width x height"
        else:
            cfg["width_mm"] = w if w else h / aspect
            how = "width" if w else "height"
    if cfg["fit"]:
        (pw, ph), is_paper = parse_sheet(cfg["fit"], dpi)
        pm = cfg["print_margin_mm"] if is_paper else 0.0
        extra = 2 * M if cfg["fit_includes_margin"] else 0.0
        best = None
        for o, (W, H) in (("portrait", (min(pw, ph), max(pw, ph))), ("landscape", (max(pw, ph), min(pw, ph)))):
            if cfg["orientation"] not in ("auto", o):
                continue
            aw, ah = W - 2 * pm - extra, H - 2 * pm - extra
            if aw <= 0 or ah <= 0:
                continue
            dw = min(aw, ah / aspect)
            if best is None or dw * dw * aspect > best[0] * best[0] * aspect:
                best = (dw, o)
        if best is None:
            sys.exit(f"margin_mm {M} does not fit on {cfg['fit']}; lower margin_mm")
        cfg["width_mm"], cfg["_fit_orientation"] = best
        how = f"fit to {cfg['fit']} ({best[1]})"
        if is_paper and cfg["pdf_page"] == "auto":
            cfg["pdf_page"] = str(cfg["fit"]).lower()
    if cfg["pdf_page"] == "auto":
        cfg["pdf_page"] = "letter"
    cfg["_size_from"] = how

def fmt_len(mm, dpi=96.0):
    """mm -> '9 13/16 in / 250.0 mm / 945 px@96dpi' (inches to nearest 1/16)."""
    n16 = round(mm / 25.4 * 16)
    whole, frac = divmod(n16, 16)
    from math import gcd
    if frac:
        g = gcd(frac, 16)
        f = f"{frac // g}/{16 // g}"
        ins = f"{whole} {f}" if whole else f
    else:
        ins = f"{whole}"
    return f"{ins} in / {mm:.1f} mm / {round(mm / 25.4 * dpi)} px@{dpi:g}dpi"

# ---------------------------------------------------------------- color separation
def hex2rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], np.uint8)

def rgb2hex(c):
    return "#%02x%02x%02x" % tuple(int(v) for v in c)

def to_lab(rgb):
    return cv2.cvtColor(rgb.reshape(-1, 1, 3).astype(np.uint8), cv2.COLOR_RGB2LAB).reshape(-1, 3).astype(np.float32)

def prep_image(path, cfg):
    """Open, crop, optionally cut out the subject. Returns RGBA PIL image."""
    im = Image.open(path)
    im = im.convert("RGBA")
    if cfg["crop"]:
        x0, y0, x1, y1 = cfg["crop"]
        if max(cfg["crop"]) <= 1.0:
            x0, x1 = x0 * im.width, x1 * im.width
            y0, y1 = y0 * im.height, y1 * im.height
        im = im.crop((int(x0), int(y0), int(x1), int(y1)))
    if cfg["remove_background"]:
        try:
            from rembg import remove, new_session
        except ImportError:
            sys.exit("remove_background needs: pip install --break-system-packages 'rembg[cpu]'")
        cut = remove(im.convert("RGB"), session=new_session(cfg["bg_model"]))
        a = np.minimum(np.asarray(cut)[..., 3], np.asarray(im)[..., 3])
        im = Image.fromarray(np.dstack([np.asarray(im)[..., :3], a]).astype(np.uint8), "RGBA")
        if cfg["background"] == "auto":
            cfg["background"] = "none"        # transparent area is the background now
    return im

def load_image(im, cfg):
    w_px = int(round(cfg["width_mm"] * cfg["px_per_mm"]))
    w_px = min(w_px, 8000)
    cfg["px_per_mm"] = w_px / cfg["width_mm"]
    h_px = int(round(im.height * w_px / im.width))
    im = im.resize((w_px, h_px), Image.LANCZOS)
    arr = np.asarray(im)
    rgb, alpha = arr[..., :3].copy(), arr[..., 3]
    rgb[alpha < 128] = 255
    return rgb, alpha >= 128

def true_lab(lab8):
    """OpenCV 8-bit Lab -> CIE L*a*b* (for real delta-E numbers)."""
    out = lab8.astype(np.float32).copy()
    out[..., 0] *= 100.0 / 255.0
    out[..., 1:] -= 128.0
    return out

def prep_lab(rgb, cfg):
    lab = to_lab(rgb.reshape(-1, 3))
    if cfg["color_mode"] == "gray":
        lab[:, 1:] = 128.0
    return lab

def assign(lab, centers, chunk=500000):
    out = np.empty(len(lab), np.uint8)
    for s in range(0, len(lab), chunk):
        d = ((lab[s:s + chunk, None, :] - centers[None, :, :]) ** 2).sum(-1)
        out[s:s + chunk] = d.argmin(1)
    return out

def quantize(lab, opaque, shape, cfg, n_colors):
    """Cluster colors. Returns labels (H, W), centers (8-bit Lab), centers_rgb, dropped edge colors."""
    H, W = shape
    if cfg["palette"]:
        centers_rgb = np.array([hex2rgb(h) for h in cfg["palette"]])
        centers = prep_lab(centers_rgb, cfg)
    else:
        pool = np.flatnonzero(opaque.ravel())
        if len(pool) == 0:
            sys.exit("image is fully transparent")
        rng = np.random.default_rng(0)
        idx = rng.choice(pool, size=min(len(pool), 150000), replace=False)
        crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 0.5)
        cv2.setRNGSeed(0)
        # over-cluster, then drop anti-aliasing "edge" clusters and merge back down to n_colors
        k_run = int(n_colors) + int(cfg["extra_clusters"])
        k_run = max(1, min(k_run, len(np.unique(lab[idx].round(), axis=0))))
        _, _, centers = cv2.kmeans(lab[idx], k_run, None, crit, 5, cv2.KMEANS_PP_CENTERS)
    labels = assign(lab, centers).reshape(H, W)
    labels = cv2.medianBlur(labels, 5)                       # kill single-pixel speckle
    dropped = []
    if not cfg["palette"]:
        labels, centers, dropped = consolidate(labels, centers, int(n_colors), cfg, opaque)
        centers_rgb = cv2.cvtColor(centers.reshape(-1, 1, 3).astype(np.uint8), cv2.COLOR_LAB2RGB).reshape(-1, 3)
    return labels, centers, centers_rgb, dropped

def analyze(rgb, opaque, cfg):
    """Try 2..max_auto_colors colors on a reduced copy; measure color error and islands.
    Proposal = fewest colors whose mean dE76 <= delta_e_target, else the knee of the curve."""
    f = min(1.0, 500.0 / rgb.shape[1])
    small = cv2.resize(rgb, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    op = cv2.resize(opaque.astype(np.uint8), None, fx=f, fy=f, interpolation=cv2.INTER_NEAREST).astype(bool)
    sub = dict(cfg, px_per_mm=cfg["px_per_mm"] * f, palette=None)
    lab = prep_lab(small, sub)
    tl = true_lab(lab)
    rows = []
    for k in range(2, int(cfg["max_auto_colors"]) + 1):
        labels, centers, centers_rgb, dropped = quantize(lab, op, small.shape[:2], sub, k)
        de = np.sqrt(((tl - true_lab(centers)[labels.ravel()]) ** 2).sum(1))[op.ravel()]
        err, p99 = de.mean(), np.percentile(de, 99)
        bg = None if cfg["background"] == "none" else int(np.bincount(
            np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]), minlength=len(centers)).argmax())
        paint = [i for i in range(len(centers)) if i != bg and ((labels == i) & op).any()]
        isl = None
        if 0 < len(paint) <= 5:
            _, info = choose_order(labels, op, paint, centers, sub)
            isl = info["chosen_islands"]
        rows.append(dict(n_colors=k, actual_colors=len(centers), paint_layers=len(paint),
                         mean_delta_e=round(float(err), 2), p99_delta_e=round(float(p99), 1), islands_estimate=isl,
                         colors=[rgb2hex(c) for c in centers_rgb], _img=centers_rgb[labels]))
    # drop counts that collapsed to the same result as a smaller count (edge colors removed)
    uniq = []
    for r in rows:
        if not uniq or r["actual_colors"] > uniq[-1]["actual_colors"]:
            uniq.append(r)
    rows = uniq
    errs = [r["mean_delta_e"] for r in rows]
    t = cfg["delta_e_target"]
    # mean alone ignores small but distinct features (cheeks, eyes); also bound the worst 1% of pixels
    pick = next((r for r in rows if r["mean_delta_e"] <= t and r["p99_delta_e"] <= 4 * t), None)
    reason = f"fewest colors with mean dE <= {t} and 99th-percentile dE <= {4 * t}"
    if pick is None:
        span = max(errs[0] - errs[-1], 1e-6)
        pick = next((rows[i] for i in range(len(rows) - 1) if errs[i] - errs[i + 1] < 0.15 * span), rows[-1])
        reason = "knee: one more color improves error by < 15% of the total range"
    return rows, pick["n_colors"], reason

def write_options(path, rows, pick):
    th = [Image.fromarray(r["_img"].astype(np.uint8)) for r in rows]
    w, h = th[0].size
    s = min(1.0, 360 / w)
    w2, h2 = int(w * s), int(h * s)
    cols = min(3, len(th))
    rws = math.ceil(len(th) / cols)
    head = 36
    img = Image.new("RGB", (cols * (w2 + 10) + 10, rws * (h2 + head + 10) + 10), "#ffffff")
    d = ImageDraw.Draw(img)
    f = font(14)
    for n, (r, t) in enumerate(zip(rows, th)):
        cy, cx = divmod(n, cols)
        x, y = 10 + cx * (w2 + 10), 10 + cy * (h2 + head + 10)
        star = "  <- proposed" if r["n_colors"] == pick else ""
        d.text((x, y), f"{r['n_colors']} colors = {r['paint_layers']} paint layers{star}", fill="#c00000" if star else "#000000", font=f)
        isl = "?" if r["islands_estimate"] is None else r["islands_estimate"]
        d.text((x, y + 17), f"mean dE {r['mean_delta_e']}  worst1% {r['p99_delta_e']}  islands ~{isl}", fill="#444444", font=f)
        img.paste(t.resize((w2, h2), Image.NEAREST), (x, y + head))
    img.save(path)

def separate(rgb, opaque, cfg):
    """Return (layers, info). layers = list of dict(color, mask) bottom->top; mask True = paint (cut)."""
    H, W = rgb.shape[:2]
    if cfg["denoise"]:
        rgb = cv2.bilateralFilter(rgb, 9, 40, 9)
        rgb = cv2.bilateralFilter(rgb, 9, 40, 9)
    if cfg["mode"] == "single":
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        if cfg["threshold"] is None:
            t, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        else:
            t = cfg["threshold"]
        mask = gray > t if cfg["invert"] else gray <= t
        mask &= opaque
        return [dict(color="#000000", mask=mask)], dict(threshold=float(t))

    lab = prep_lab(rgb, cfg)
    labels, centers, centers_rgb, dropped = quantize(lab, opaque, (H, W), cfg, cfg["n_colors"])
    k = len(centers)
    share = np.bincount(labels[opaque], minlength=k) / max(int(opaque.sum()), 1)

    bg = None
    if cfg["background"] == "auto":
        border = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
        bg = int(np.bincount(border, minlength=k).argmax())
    elif cfg["background"] not in (None, "none"):
        bl = to_lab(hex2rgb(cfg["background"])[None])[0]
        bg = int(((centers - bl) ** 2).sum(1).argmin())
    paint_ids = [i for i in range(k) if i != bg and share[i] > 0]
    order_info = None
    if cfg["layer_order"] == "auto" and cfg["layering"] == "stacked" and len(paint_ids) > 1:
        order, order_info = choose_order(labels, opaque, paint_ids, centers, cfg)
    elif isinstance(cfg["layer_order"], list):
        want = [to_lab(hex2rgb(h)[None])[0] for h in cfg["layer_order"]]
        order = []
        for wl in want:
            j = min(paint_ids, key=lambda i: ((centers[i] - wl) ** 2).sum())
            if j not in order:
                order.append(j)
        order += [i for i in paint_ids if i not in order]
    else:
        order = sorted(paint_ids, key=lambda i: -centers[i][0])   # light -> dark
    if len(order) > MAX_CUT_LAYERS:
        sys.exit(f"{len(order)} paint layers; max {MAX_CUT_LAYERS}. Lower n_colors.")
    own = [(labels == i) & opaque for i in order]
    layers = []
    for n, i in enumerate(order):
        if cfg["layering"] == "stacked":
            m = np.logical_or.reduce(own[n:])
        else:
            m = own[n].copy()
            if n + 1 < len(own) and cfg["underlap_mm"] > 0:
                r = max(1, int(round(cfg["underlap_mm"] * cfg["px_per_mm"])))
                grow = cv2.dilate(own[n].astype(np.uint8), disk(r)).astype(bool)
                m |= grow & np.logical_or.reduce(own[n + 1:])
        layers.append(dict(color=rgb2hex(centers_rgb[i]), mask=m))
    info = dict(order_search=order_info, dropped_edge_colors=dropped, clusters=[dict(color=rgb2hex(centers_rgb[i]), share=round(float(share[i]), 4),
                               role="background" if i == bg else f"layer {order.index(i) + 1}")
                          for i in range(k)])
    return layers, info

def count_islands(mask, min_px):
    """Material pieces enclosed by cut (not connected to the outer border)."""
    mat = np.pad(~mask, 1, constant_values=True).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(mat, connectivity=4)
    outer = lab[0, 0]
    return sum(1 for j in range(1, n) if j != outer and st[j][4] >= min_px)

def choose_order(labels, opaque, paint_ids, centers, cfg):
    """Stacked mode: any paint order reproduces the image, but the order decides how many
    islands (bridges) exist. Score = islands + penalty for spraying a much lighter color
    over a darker one (poor coverage). Evaluated on a coarse raster."""
    from itertools import permutations
    f = 2.0 / cfg["px_per_mm"]                                  # ~2 px/mm
    small = cv2.resize(labels, None, fx=f, fy=f, interpolation=cv2.INTER_NEAREST)
    op = cv2.resize(opaque.astype(np.uint8), None, fx=f, fy=f, interpolation=cv2.INTER_NEAREST).astype(bool)
    own = {i: ((small == i) & op) for i in paint_ids}
    min_px = max(1, int(cfg["min_island_mm2"] * 4))
    cache = {}
    def isl(subset):
        key = frozenset(subset)
        if key not in cache:
            cache[key] = count_islands(np.logical_or.reduce([own[i] for i in subset]), min_px)
        return cache[key]
    base = sorted(paint_ids, key=lambda i: -centers[i][0])
    cands = permutations(paint_ids) if len(paint_ids) <= 5 else [tuple(base)]
    w = cfg["inversion_penalty"]
    best = None
    for perm in cands:
        islands = sum(isl(perm[n:]) for n in range(len(perm)))
        inv = sum(1 for a in range(len(perm)) for b in range(a + 1, len(perm))
                  if centers[perm[b]][0] - centers[perm[a]][0] > 20 * 255 / 100)
        score = islands + w * inv
        key = (score, [-centers[i][0] for i in perm])
        if best is None or key < best[0]:
            best = (key, list(perm), islands, inv)
    light_dark = sum(isl(base[n:]) for n in range(len(base)))
    return best[1], dict(chosen_islands=best[2], light_to_dark_islands=light_dark,
                         lighter_over_darker_steps=best[3])

def consolidate(labels, centers, n_target, cfg, opaque):
    """Drop clusters that only exist as thin bands along edges (anti-aliasing / JPEG blends)
    or are tiny, reassign their pixels to the spatially nearest kept color, then merge the
    closest colors (Lab) until n_target remain."""
    from scipy.ndimage import distance_transform_edt
    k = len(centers)
    er = disk(max(2, cfg["px_per_mm"] * 0.25))
    total = max(int(opaque.sum()), 1)
    keep, dropped = [], []
    for i in range(k):
        m = ((labels == i) & opaque).astype(np.uint8)
        a = int(m.sum())
        surv = cv2.erode(m, er).sum() / max(a, 1)
        if a / total < cfg["min_color_share"] or surv < 0.25:
            if a:
                rgb = cv2.cvtColor(centers[i].reshape(1, 1, 3).astype(np.uint8), cv2.COLOR_LAB2RGB).reshape(3)
                dropped.append(dict(color=rgb2hex(rgb), share=round(a / total, 4),
                                    reason="edge blend" if surv < 0.25 else "tiny"))
        else:
            keep.append(i)
    if not keep:
        return labels, centers, dropped
    unassigned = ~np.isin(labels, keep)
    if unassigned.any():
        idx = distance_transform_edt(unassigned, return_distances=False, return_indices=True)
        labels = labels[idx[0], idx[1]]
    # merge nearest colors down to the requested count (pixel-weighted centers)
    groups = {i: [i] for i in keep}
    cen = {i: centers[i].astype(np.float64) for i in keep}
    cnt = {i: int((labels == i).sum()) for i in keep}
    while len(groups) > n_target:
        ids = list(groups)
        a, b = min(((x, y) for n, x in enumerate(ids) for y in ids[n + 1:]),
                   key=lambda p: ((cen[p[0]] - cen[p[1]]) ** 2).sum())
        if cnt[a] < cnt[b]:
            a, b = b, a
        cen[a] = (cen[a] * cnt[a] + cen[b] * cnt[b]) / max(cnt[a] + cnt[b], 1)
        cnt[a] += cnt[b]
        groups[a] += groups.pop(b)
    lut = np.zeros(256, np.uint8)
    new_centers = []
    for n, (g, members) in enumerate(groups.items()):
        for m in members:
            lut[m] = n
        new_centers.append(cen[g])
    return lut[labels], np.array(new_centers, np.float32), dropped

# ---------------------------------------------------------------- raster cleanup
def disk(r):
    r = max(1, int(round(r)))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))

def drop_small(mask, min_px, keep_border=False):
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    out = np.zeros_like(mask, bool)
    H, W = mask.shape
    for i in range(1, n):
        x, y, w, h, a = stats[i]
        touches = x == 0 or y == 0 or x + w == W or y + h == H
        if a >= min_px or (keep_border and touches):
            out |= lab == i
    return out

def clean_mask(mask, cfg):
    ppm = cfg["px_per_mm"]
    m = mask.astype(np.uint8)
    # close thin material gaps between cuts, then open away thin cut features
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, disk(cfg["min_material_mm"] * ppm / 2))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, disk(cfg["min_cut_mm"] * ppm / 2))
    m = m.astype(bool)
    m = drop_small(m, cfg["min_area_mm2"] * ppm * ppm)
    # tiny material islands (holes in the cut) are cut away rather than bridged
    holes = drop_small(~m, cfg["min_island_mm2"] * ppm * ppm, keep_border=True)
    return ~holes

# ---------------------------------------------------------------- vectorize
def vectorize(mask, cfg, design):
    ppm = cfg["px_per_mm"]
    f = np.pad(mask.astype(np.float32), 1)
    s = cfg["smooth_mm"] * ppm
    if s > 0:
        f = cv2.GaussianBlur(f, (0, 0), s)
    rings = []
    for c in find_contours(f, 0.5):
        if len(c) < 4:
            continue
        xy = np.column_stack([(c[:, 1] - 1) / ppm, (c[:, 0] - 1) / ppm])
        p = Polygon(xy)
        if not p.is_valid:
            p = p.buffer(0)
        if p.is_empty or p.area < 1e-4:
            continue
        rings.append((p, Point(xy[0])))
    if not rings:
        return MultiPolygon()
    polys = [r[0] if isinstance(r[0], Polygon) else max(r[0].geoms, key=lambda g: g.area) for r in rings]
    tree = STRtree(polys)
    depth, parent = [], []
    for i, (_, pt) in enumerate(rings):
        cont = [j for j in tree.query(pt) if j != i and polys[j].contains(pt)]
        depth.append(len(cont))
        parent.append(min(cont, key=lambda j: polys[j].area) if cont else None)
    shells = {i: [] for i in range(len(polys)) if depth[i] % 2 == 0}
    for i in range(len(polys)):
        if depth[i] % 2 == 1 and parent[i] in shells:
            shells[parent[i]].append(polys[i].exterior.coords)
    geoms = [Polygon(polys[i].exterior.coords, holes).buffer(0) for i, holes in shells.items()]
    g = unary_union(geoms).intersection(design)
    g = g.simplify(cfg["simplify_mm"], preserve_topology=True)
    return as_multi(g)

def as_multi(g):
    if g.is_empty:
        return MultiPolygon()
    if isinstance(g, Polygon):
        return MultiPolygon([g])
    if isinstance(g, MultiPolygon):
        return g
    return MultiPolygon([p for p in getattr(g, "geoms", []) if isinstance(p, Polygon) and p.area > 0])

def parts(g):
    if g.is_empty:
        return []
    return list(g.geoms) if hasattr(g, "geoms") else [g]

def enforce_minimums(cut, cfg, design):
    """Vector morphology: remove cut features narrower than min_cut_mm (opening), then
    fill material gaps thinner than min_material_mm (closing), drop specks."""
    if not cfg["enforce_minimums"] or cut.is_empty:
        return cut
    rm, rc = cfg["min_material_mm"] / 2, cfg["min_cut_mm"] / 2
    j = dict(join_style="mitre", mitre_limit=3.0)
    # open (drop thin cut) first, close (drop thin material) last: material strength wins at pinch points
    g = cut.buffer(-rc, **j).buffer(rc, **j)
    g = g.buffer(rm, **j).buffer(-rm, **j)
    g = g.intersection(design)
    g = unary_union([p for p in parts(g) if p.area >= cfg["min_area_mm2"]])
    return as_multi(g.simplify(cfg["simplify_mm"], preserve_topology=True))

# ---------------------------------------------------------------- bridging
def components(cut, sheet):
    mat = sheet.difference(cut)
    comps = [p for p in parts(mat) if isinstance(p, Polygon) and p.area > 1e-6]
    ext = sheet.exterior
    main = [c for c in comps if c.exterior.distance(ext) < 1e-6]
    main = max(main, key=lambda c: c.area) if main else max(comps, key=lambda c: c.area)
    return main, [c for c in comps if c is not main]

def bridge_poly(p, q, w):
    p, q = np.asarray(p, float), np.asarray(q, float)
    d = q - p
    L = np.hypot(*d)
    u = d / L if L > 0 else np.array([1.0, 0.0])
    ext = w / 2 + 0.3
    line = LineString([p - u * ext, q + u * ext])
    return line.buffer(w / 2, cap_style="flat")

def candidate_segments(isl, others, cfg, keep, hide, w):
    step = max(0.3, w / 2)
    bnd = isl.boundary
    n = int(min(3000, max(8, bnd.length / step)))
    pts = shapely.line_interpolate_point(bnd, np.linspace(0, 1, n, endpoint=False), normalized=True)
    lines = shapely.shortest_line(pts, others)
    L = shapely.length(lines)
    ok = L > 1e-6
    ok &= shapely.length(shapely.intersection(lines, isl)) < 1e-3
    if keep is not None:
        ok &= ~shapely.intersects(lines, keep.buffer(w / 2))
    score = L.copy()
    if cfg["bridge_angle_deg"] is not None:
        c = shapely.get_coordinates(lines).reshape(-1, 2, 2)
        ang = np.arctan2(c[:, 1, 1] - c[:, 0, 1], c[:, 1, 0] - c[:, 0, 0])
        dev = np.abs(np.sin(ang - math.radians(cfg["bridge_angle_deg"])))  # 0 = aligned (either way)
        score *= 1 + cfg["bridge_angle_weight"] * dev
    hid = np.zeros(len(L))
    if hide is not None and not hide.is_empty:
        hid = shapely.length(shapely.intersection(lines, hide)) / np.maximum(L, 1e-9)
        score *= 1 - 0.8 * hid
    return pts, lines, L, ok, score, hid

def auto_bridges(cut, sheet, cfg, layer_no, keep, hide, forced, next_id, tag="B", skip=None):
    """Apply forced specs, then add auto bridges until the material is one piece.
    Islands touching `skip` are left floating on purpose."""
    w = cfg["bridge_width_mm"]
    specs = []
    for s in forced:
        cut = cut.difference(bridge_poly(s["p"], s["q"], s["width_mm"]))
        specs.append(s)
    k = max(1, int(cfg["bridges_per_island"]))
    for _ in range(5000):
        main, islands = components(cut, sheet)
        todo = [c for c in islands if skip is None or not c.intersects(skip)]
        if not todo:
            break
        isl = min(todo, key=lambda c: c.distance(main))
        others = unary_union([main] + [c for c in islands if c is not isl])
        pts, lines, L, ok, score, hid = candidate_segments(isl, others, cfg, keep, hide, w)
        if not ok.any():                       # every option blocked by keep-outs: ignore them
            pts, lines, L, ok, score, hid = candidate_segments(isl, others, cfg, None, hide, w)
        if not ok.any():
            break
        chosen = []
        order = np.argsort(np.where(ok, score, np.inf))
        min_sep = isl.boundary.length / (2 * k)
        for i in order:
            if not ok[i] or len(chosen) >= k:
                break
            if all(pts[i].distance(pts[j]) >= min_sep for j in chosen):
                chosen.append(i)
        for i in chosen:
            c = shapely.get_coordinates(lines[i])
            s = dict(id=f"L{layer_no}-{tag}{next_id}", p=c[0].tolist(), q=c[1].tolist(), width_mm=w,
                     length_mm=round(float(L[i]), 2), hidden=round(float(hid[i]), 2), source="auto")
            next_id += 1
            specs.append(s)
            cut = cut.difference(bridge_poly(s["p"], s["q"], w))
    return cut, specs, next_id

def spec_at(cut, at, angle, w, layer_no, sid, source):
    """A bridge through point `at` across the cut region that contains it."""
    pt = Point(at)
    region = min(parts(cut), key=lambda g: g.distance(pt)) if not cut.is_empty else None
    if region is None:
        return None
    if not region.buffer(-0.05).contains(pt):          # snap just inside the nearest cut
        inner = region.buffer(-0.1)
        pt = shapely.ops.nearest_points(inner if not inner.is_empty else region, pt)[0]
    def crossing(a):
        u = np.array([math.cos(math.radians(a)), math.sin(math.radians(a))])
        far = LineString([np.array(pt.coords[0]) - 1e4 * u, np.array(pt.coords[0]) + 1e4 * u])
        seg = [g for g in parts(far.intersection(region)) if g.distance(pt) < 1e-6 and g.length > 1e-3]
        return seg[0] if seg else None
    shortest = None
    for a in range(0, 180, 3):
        s = crossing(a)
        if s is not None and (shortest is None or s.length < shortest.length):
            shortest = s
    if shortest is None:
        return None
    best, note = shortest, None
    if angle is not None:
        s = crossing(angle)
        # an angle that runs ALONG a cut would erase it; refuse and use the shortest crossing
        if s is not None and s.length <= 2.5 * shortest.length + w:
            best = s
        else:
            note = f"angle {angle} runs along the cut ({s.length if s else 0:.1f} mm); used shortest crossing"
    c = np.asarray(best.coords)
    out = dict(id=sid, p=c[0].tolist(), q=c[-1].tolist(), width_mm=w, length_mm=round(best.length, 2),
               hidden=0.0, source=source)
    if note:
        out["note"] = note
    return out

def layer_bridging_on(cfg, layer_no):
    if not cfg["bridges"]:
        return False
    bl = cfg["bridge_layers"]
    return bl == "all" or layer_no in bl

def bridge_layer(cut, sheet, cfg, layer_no, hide):
    """Returns (cut, bridge specs, skip). skip = geometry marking islands left floating on purpose
    (None = none; sheet = every island, bridging off for this layer)."""
    if not layer_bridging_on(cfg, layer_no):
        return cut, [], sheet
    keeps = [box(*r) for r in cfg["keepout"]]
    keep = unary_union(keeps) if keeps else None
    w = cfg["bridge_width_mm"]
    extras = []
    for j, e in enumerate([e for e in cfg["extra_bridges"] if e.get("layer", 1) == layer_no]):
        s = spec_at(cut, e["at"], e.get("angle_deg"), e.get("width_mm", w), layer_no, f"L{layer_no}-X{j + 1}", "extra")
        if s:
            extras.append(s)
    # pass 1: deterministic auto layout (IDs refer to this pass)
    cut1, specs1, nid = auto_bridges(cut, sheet, cfg, layer_no, keep, hide, extras, 1)
    ov = {k: v for k, v in cfg["bridge_overrides"].items() if k.startswith(f"L{layer_no}-")}
    if not ov:
        return cut1, specs1, None
    forced, new_keeps, offs = [], list(keeps), []
    for s in specs1:
        o = ov.get(s["id"], {})
        if o.get("off"):
            offs.append(Point(s["p"]).buffer(0.2))       # island end of the bridge
            continue
        if o.get("delete") or o.get("move_to"):
            mid = (np.array(s["p"]) + np.array(s["q"])) / 2
            new_keeps.append(Point(mid).buffer(max(3 * s["width_mm"], 2)))
            if o.get("move_to"):
                m = spec_at(cut, o["move_to"], o.get("angle_deg"), o.get("width_mm", s["width_mm"]),
                            layer_no, s["id"] + "m", "moved")
                if m:
                    forced.append(m)
            continue
        if s["source"] == "auto":
            s = dict(s)
            if "width_mm" in o:
                s["width_mm"], s["source"] = float(o["width_mm"]), "override"
            forced.append(s)
    forced = extras + [f for f in forced if f["source"] != "extra"]
    keep2 = unary_union(new_keeps) if new_keeps else None
    skip = unary_union(offs) if offs else None
    cut2, specs2, _ = auto_bridges(cut, sheet, cfg, layer_no, keep2, hide, forced, nid, skip=skip)
    return cut2, specs2, skip

# ---------------------------------------------------------------- validation
def thin_spots(g, min_w):
    if g.is_empty:
        return []
    r = min_w / 2 * 0.95
    lost = g.difference(g.buffer(-r, join_style="mitre", mitre_limit=3.0).buffer(r, join_style="mitre", mitre_limit=3.0))
    out = []
    for p in parts(lost):
        if p.area > (0.2 * min_w) ** 2:
            c = p.representative_point()
            out.append(dict(x=round(c.x, 1), y=round(c.y, 1), area_mm2=round(p.area, 3)))
    return out

def validate(cut, sheet, cfg, design, skip=None):
    main, islands = components(cut, sheet)
    mat = sheet.difference(cut)
    floating = [i for i in islands if skip is not None and i.intersects(skip)]
    unintended = [i for i in islands if not (skip is not None and i.intersects(skip))]
    floating.sort(key=lambda g: (round(g.centroid.y / 10), g.centroid.x))   # stable reading order
    return dict(
        material_pieces=1 + len(islands),
        floating_islands=[dict(id=f"I{n + 1}", x=round(i.centroid.x, 1), y=round(i.centroid.y, 1),
                               area_mm2=round(i.area, 2)) for n, i in enumerate(floating)],
        islands=[dict(x=round(i.centroid.x, 1), y=round(i.centroid.y, 1), area_mm2=round(i.area, 2)) for i in unintended],
        thin_cut=thin_spots(cut, cfg["min_cut_mm"]),
        thin_material=thin_spots(mat.intersection(design.buffer(1)), min(cfg["min_material_mm"], cfg["bridge_width_mm"])),
        cut_shapes=len(parts(cut)),
    )

def reg_marks(sheet_b, cfg):
    if not cfg["reg_marks"]:
        return MultiPolygon()
    x0, y0, x1, y1 = sheet_b
    m = cfg["margin_mm"] / 2
    a = min(cfg["reg_size_mm"], cfg["margin_mm"] * 0.6) / 2
    t = max(cfg["min_cut_mm"], 1.0) / 2
    out = []
    for cx, cy in [(x0 + m, y0 + m), (x1 - m, y0 + m), (x0 + m, y1 - m), (x1 - m, y1 - m)]:
        out.append(unary_union([box(cx - a, cy - t, cx + a, cy + t), box(cx - t, cy - a, cx + t, cy + a)]))
    return as_multi(unary_union(out))

# ---------------------------------------------------------------- writers
def ring_paths(g, dx=0.0, dy=0.0):
    out = []
    for p in parts(g):
        for r in [p.exterior] + list(p.interiors):
            c = np.asarray(r.coords)[:-1]
            out.append(c + [dx, dy])
    return out

def svg_path(rings):
    s = []
    for c in rings:
        s.append("M" + " L".join(f"{x:.3f},{y:.3f}" for x, y in c) + " Z")
    return " ".join(s)

def write_svg(path, W, H, groups):
    """groups: list of (name, color, rings, closed_rect_or_None)."""
    L = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.3f}mm" height="{H:.3f}mm" viewBox="0 0 {W:.3f} {H:.3f}">']
    for name, color, rings in groups:
        if rings:
            L.append(f'<g id="{name}"><path d="{svg_path(rings)}" fill="none" stroke="{color}" stroke-width="0.1"/></g>')
    L.append("</svg>")
    with open(path, "w") as f:
        f.write("\n".join(L))

def write_dxf(path, H, groups):
    import ezdxf
    ezdxf.options.write_fixed_meta_data_for_testing = True   # no timestamps/GUIDs: reruns are identical
    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 4          # millimeters
    doc.header["$MEASUREMENT"] = 1
    for k in ("$TDCREATE", "$TDUPDATE", "$TDUCREATE", "$TDUUPDATE"):   # fixed: reruns give identical files
        doc.header[k] = 2451545.0
    doc.header["$FINGERPRINTGUID"] = "{00000000-0000-0000-0000-000000000001}"
    doc.header["$VERSIONGUID"] = "{00000000-0000-0000-0000-000000000002}"
    msp = doc.modelspace()
    for name, aci, rings in groups:
        if name not in doc.layers:
            doc.layers.add(name, color=aci)
        for c in rings:
            msp.add_lwpolyline([(x, H - y) for x, y in c], close=True, dxfattribs={"layer": name})
    doc.saveas(path)

def write_pdf(path, layers, SW, SH, cfg, sheet_off):
    from reportlab.pdfgen import canvas
    k = 72 / 25.4
    pw_mm, ph_mm = PAPER[cfg["pdf_page"]]
    PORT = (pw_mm * k, ph_mm * k)
    edge, foot = max(cfg["print_margin_mm"], 4.0), 0.0   # label is drawn inside the sheet margin
    ov = cfg["pdf_overlap_mm"]
    def tiling(PW, PH):
        aw, ah = PW / k - 2 * edge, PH / k - 2 * edge - foot
        nx = max(1, math.ceil((SW - ov) / (aw - ov))) if SW > aw + 0.01 else 1
        ny = max(1, math.ceil((SH - ov) / (ah - ov))) if SH > ah + 0.01 else 1
        return nx * ny, (PW, PH), aw, ah, nx, ny
    _, (PW, PH), aw, ah, nx, ny = min(tiling(*PORT), tiling(PORT[1], PORT[0]), key=lambda t: t[0])
    c = canvas.Canvas(path, pagesize=PORT)
    c.setPageSize(PORT)
    PWc, PHc = PORT
    # cover page: calibration + instructions
    c.setFont("Helvetica-Bold", 16)
    c.drawString(edge * k, PHc - 20 * k, "Stencil - print at 100% / Actual size (no 'fit to page')")
    c.setFont("Helvetica", 10)
    y = PHc - 30 * k
    lines = [f"Sheet {fmt_len(SW, cfg['dpi'])}  x  {fmt_len(SH, cfg['dpi'])}",
             f"{len(layers)} layer(s), {nx * ny} {cfg['pdf_page']} {'landscape' if PW > PH else 'portrait'} "
             f"page(s) per layer.",
             "Gray = cut out (paint goes through). White = keep. Red = bridge (do not cut).",
             "Blue I1, I2... = floating island: cut it out, keep it, place it by hand (spray adhesive).",
             "Check the squares below with a ruler before cutting anything.",
             "Joining pages: cut each page along its dashed line(s), lay that cut edge exactly on the",
             "thin gray frame line of the neighboring page (left / above), check the art lines up, tape."]
    for l in lines:
        c.drawString(edge * k, y, l); y -= 14
    y -= 10
    c.setLineWidth(0.5)
    c.rect(edge * k, y - 25.4 * k, 25.4 * k, 25.4 * k); c.drawString(edge * k, y - 25.4 * k - 12, "1 in")
    c.rect((edge + 40) * k, y - 50 * k, 50 * k, 50 * k); c.drawString((edge + 40) * k, y - 50 * k - 12, "50 mm")
    y -= 50 * k + 30
    for i, Ly in enumerate(layers):
        c.setFillColor(Ly["color"]); c.rect(edge * k, y - 2, 12, 12, fill=1, stroke=1)
        c.setFillColor("#000000"); c.drawString(edge * k + 18, y, f"Layer {i + 1}: {Ly['color']}  (paint order {i + 1})")
        y -= 16
    c.showPage()
    for i, Ly in enumerate(layers):
        for ty in range(ny):
            for tx in range(nx):
                c.setPageSize((PW, PH))
                ox, oy = tx * (aw - ov), ty * (ah - ov)
                X0, Y0 = edge * k, PH - edge * k
                def tp(x, y_):
                    return X0 + (x - ox) * k, Y0 - (y_ - oy) * k
                c.saveState()
                p = c.beginPath(); p.rect(X0, Y0 - ah * k, aw * k, ah * k)
                c.clipPath(p, stroke=0, fill=0)
                def draw(g, fill, stroke, lw):
                    for poly in parts(g):
                        path = c.beginPath()
                        for r in [poly.exterior] + list(poly.interiors):
                            pts = [tp(x, y_) for x, y_ in np.asarray(r.coords)]
                            path.moveTo(*pts[0])
                            for q in pts[1:]:
                                path.lineTo(*q)
                            path.close()
                        c.setFillColor(fill); c.setStrokeColor(stroke); c.setLineWidth(lw)
                        c.drawPath(path, fill=1, stroke=1, fillMode=0)
                draw(Ly["cut"], "#c8c8c8", "#000000", 0.4)
                draw(Ly["reg"], "#c8c8c8", "#000000", 0.4)
                for b in Ly["bridge_marks"]:
                    draw(b, "#ff6060", "#ff6060", 0.1)
                c.setFillColor("#0050ff"); c.setFont("Helvetica-Bold", 8)
                for fi in Ly.get("floating", []):
                    c.drawCentredString(*tp(fi["x"] + sheet_off, fi["y"] + sheet_off), fi["id"])
                c.setFillColor("#000000")
                c.setStrokeColor("#000000"); c.setLineWidth(0.8)
                x0, y0 = tp(0, 0); x1, y1 = tp(SW, SH)
                c.rect(x0, y1, x1 - x0, y0 - y1)
                c.setDash(3, 3); c.setStrokeColor("#888888"); c.setLineWidth(0.4)
                if tx > 0:
                    c.line(*tp(ox + ov, oy), *tp(ox + ov, oy + ah))
                if ty > 0:
                    c.line(*tp(ox, oy + ov), *tp(ox + aw, oy + ov))
                c.restoreState()
                if nx * ny > 1:                                   # tile frame = alignment edge
                    c.setStrokeColor("#aaaaaa"); c.setLineWidth(0.3); c.setDash()
                    c.rect(X0, Y0 - ah * k, aw * k, ah * k)
                    c.setStrokeColor("#000000")
                c.setFont("Helvetica", 7)
                c.setFillColor("#555555")
                c.drawString(edge * k + 3, Y0 - ah * k + 3,
                             f"Layer {i + 1}/{len(layers)} {Ly['color']} - page row {ty + 1}/{ny}, col {tx + 1}/{nx}"
                             f" - print at 100%")
                c.setFillColor("#000000")
                c.showPage()
    c.save()
    return nx * ny

def font(sz):
    try:
        return ImageFont.load_default(size=sz)
    except TypeError:
        return ImageFont.load_default()

def raster(g, img, d, s, ox, oy, fill, hole):
    for p in parts(g):
        d.polygon([(ox + x * s, oy + y * s) for x, y in p.exterior.coords], fill=fill)
        for r in p.interiors:
            d.polygon([(ox + x * s, oy + y * s) for x, y in r.coords], fill=hole)

def write_preview(path, layers, SW, SH, val, off):
    s = min(4.0, 900 / max(SW, SH))
    pw, ph = int(SW * s), int(SH * s)
    n = len(layers) + 1
    cols = min(n, 3)
    rows = math.ceil(n / cols)
    pad, head = 12, 26
    img = Image.new("RGB", (cols * (pw + pad) + pad, rows * (ph + pad + head) + pad), "#ffffff")
    d = ImageDraw.Draw(img)
    f, fs = font(16), font(11)
    for i, Ly in enumerate(layers):
        r, cl = divmod(i, cols)
        ox, oy = pad + cl * (pw + pad), pad + r * (ph + pad + head) + head
        v = val[i]
        okc = "#007a00" if not v["islands"] else "#c00000"
        fl = f"  floating={len(v['floating_islands'])}" if v["floating_islands"] else ""
        d.text((ox, oy - head + 4), f"Layer {i + 1} {Ly['color']}  pieces={v['material_pieces']}{fl}  "
               f"bridges={len(Ly['bridges'])}  thin={len(v['thin_cut']) + len(v['thin_material'])}", fill=okc, font=f)
        d.rectangle([ox, oy, ox + pw, oy + ph], fill="#e9dcc0", outline="#000000")
        raster(Ly["cut"], img, d, s, ox, oy, "#222222", "#e9dcc0")
        raster(Ly["reg"], img, d, s, ox, oy, "#222222", "#e9dcc0")
        for b in Ly["bridge_marks"]:
            raster(b, img, d, s, ox, oy, "#ff2020", "#ff2020")
        for b in Ly["bridges"]:
            mx = (b["p"][0] + b["q"][0]) / 2 + off; my = (b["p"][1] + b["q"][1]) / 2 + off
            d.text((ox + mx * s + 4, oy + my * s - 6), b["id"].split("-")[1], fill="#d00000", font=fs)
        for spot in v["thin_cut"] + v["thin_material"] + v["islands"]:
            cx, cy = ox + (spot["x"] + off) * s, oy + (spot["y"] + off) * s
            d.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], outline="#ff00ff", width=2)
        for fi in v["floating_islands"]:
            cx, cy = ox + (fi["x"] + off) * s, oy + (fi["y"] + off) * s
            d.text((cx - 6, cy - 6), fi["id"], fill="#0050ff", font=fs)
    # paint simulation
    r, cl = divmod(len(layers), cols)
    ox, oy = pad + cl * (pw + pad), pad + r * (ph + pad + head) + head
    d.text((ox, oy - head + 4), "Painted result (simulated)", fill="#000000", font=f)
    d.rectangle([ox, oy, ox + pw, oy + ph], fill="#ffffff", outline="#000000")
    for Ly in layers:
        raster(Ly["cut"], img, d, s, ox, oy, Ly["color"], "#ffffff")
    img.save(path)

# ---------------------------------------------------------------- main
def load_paint_chart(path):
    import csv
    out = []
    with open(path, newline="") as f:
        for row in csv.reader(f):
            if not row or row[0].strip().lower() in ("name", "#") or row[0].startswith("#"):
                continue
            name, hx = row[0].strip(), row[1].strip()
            if not hx.startswith("#"):
                hx = "#" + hx
            out.append(dict(name=name, hex=hx.lower(), brand=row[2].strip() if len(row) > 2 else ""))
    if not out:
        sys.exit(f"paint_chart {path}: no rows (expected name,hex[,brand])")
    return out

def nearest_paint(hexcol, chart):
    tl = true_lab(to_lab(hex2rgb(hexcol)[None]))[0]
    best = None
    for p in chart:
        d = float(np.sqrt(((true_lab(to_lab(hex2rgb(p["hex"])[None]))[0] - tl) ** 2).sum()))
        if best is None or d < best[0]:
            best = (d, p)
    return dict(best[1], delta_e=round(best[0], 1))

def public_cfg(cfg):
    keys = list(DEFAULTS) + ["min_bridge_mm", "min_cut_mm", "min_material_mm", "width_mm"]
    out = {k: cfg[k] for k in keys if k in cfg}
    out["n_layers"] = None            # n_colors below is the resolved value
    return out

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("--out", required=True)
    ap.add_argument("--params")
    ap.add_argument("--set", action="append", help="key=json_value, overrides params file")
    ap.add_argument("--analyze", action="store_true", help="propose a color count; writes analysis.json + options.png")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()
    cfg = load_cfg(args)
    os.makedirs(args.out, exist_ok=True)

    im = prep_image(args.image, cfg)       # may switch background to "none" (cut-out subject)
    if cfg["n_layers"] is not None:
        cfg["n_colors"] = int(cfg["n_layers"]) + (0 if cfg["background"] == "none" else 1)
    resolve_size(cfg, im.height / im.width)
    rgb, opaque = load_image(im, cfg)
    if cfg["denoise"]:
        rgb = cv2.bilateralFilter(rgb, 9, 40, 9)
        rgb = cv2.bilateralFilter(rgb, 9, 40, 9)
    H_px, W_px = rgb.shape[:2]
    DW, DH = W_px / cfg["px_per_mm"], H_px / cfg["px_per_mm"]
    M = cfg["margin_mm"]
    design = box(0, 0, DW, DH)
    sheet = box(-M, -M, DW + M, DH + M)
    SW, SH = DW + 2 * M, DH + 2 * M

    color_info = None
    if cfg["mode"] == "multi":
        if cfg["palette"]:
            cfg["n_colors"] = len(cfg["palette"])
            color_info = dict(source="palette", used=cfg["n_colors"])
        elif cfg["n_colors"] == "auto" or args.analyze:
            rows, pick, reason = analyze(rgb, opaque, cfg)
            if args.analyze:
                write_options(os.path.join(args.out, "options.png"), rows, pick)
                with open(os.path.join(args.out, "analysis.json"), "w") as f:
                    json.dump(dict(proposed_n_colors=pick, reason=reason,
                                   note="n_colors includes the unpainted background color; paint layers shown per row",
                                   design=fmt_len(DW, cfg["dpi"]) + "  x  " + fmt_len(DH, cfg["dpi"]),
                                   options=[{k: v for k, v in r.items() if k != "_img"} for r in rows]), f, indent=1)
                print(f"proposed n_colors={pick} ({reason})")
                for r in rows:
                    print(f"  {r['n_colors']} colors / {r['paint_layers']} paint layers: mean dE {r['mean_delta_e']}, p99 {r['p99_delta_e']}, "
                          f"islands ~{r['islands_estimate']}  {' '.join(r['colors'])}")
                return
            cfg["n_colors"] = pick
            color_info = dict(source="auto", used=pick, reason=reason,
                              options=[{k: r[k] for k in ("n_colors", "paint_layers", "mean_delta_e", "islands_estimate")}
                                       for r in rows])
        else:
            cfg["n_colors"] = int(cfg["n_colors"])
            color_info = dict(source="user", used=cfg["n_colors"])
    elif args.analyze:
        sys.exit("--analyze applies to mode=multi")

    raw_layers, sep_info = separate(rgb, opaque, dict(cfg, denoise=False))
    chart = load_paint_chart(cfg["paint_chart"]) if cfg["paint_chart"] else None
    paints = []
    for L in raw_layers:
        p = nearest_paint(L["color"], chart) if chart else None
        paints.append(p)
        if p and cfg["snap_to_paint"]:
            L["color"] = p["hex"]
    warnings = []
    cuts = []
    ppm = cfg["px_per_mm"]
    for i, L in enumerate(raw_layers):
        cm = clean_mask(L["mask"], cfg)
        cuts.append(enforce_minimums(vectorize(cm, cfg, design), cfg, design))
        # report detail the cleanup threw away (too thin / too small to cut)
        lost = (L["mask"] & ~cm).astype(np.uint8)
        lost = cv2.morphologyEx(lost, cv2.MORPH_OPEN, disk(1))
        n, _, st, cen = cv2.connectedComponentsWithStats(lost)
        big = [(cen[j], st[j][4]) for j in range(1, n) if st[j][4] > (1.5 * ppm) ** 2]
        if big:
            warnings.append(dict(layer=i + 1, type="detail_removed", count=len(big),
                                 where=[[round(float(c[0]) / ppm, 1), round(float(c[1]) / ppm, 1)] for c, _ in big[:10]],
                                 hint="features narrower than min_cut_mm or thinner material than min_material_mm "
                                      "were dropped; enlarge the design or lower the minimums if they matter"))
    for i in range(len(cuts) - 1):
        a, b = cuts[i], cuts[i + 1]
        if a.area > 0 and a.symmetric_difference(b).area / a.area < 0.03:
            warnings.append(dict(layer=i + 1, type="redundant_layer",
                                 hint=f"layer {i + 1} is ~identical to layer {i + 2}; lower n_colors or set palette"))

    out_layers, vals = [], []
    for i, (L, cut) in enumerate(zip(raw_layers, cuts)):
        hide = unary_union(cuts[i + 1:]) if cfg["hide_bridges"] and i + 1 < len(cuts) else None
        bcut, specs, skip = bridge_layer(cut, sheet, cfg, i + 1, hide)
        # re-apply minimums around the new bridges; if that splits a piece off through a
        # too-thin neck, bridge it properly ("R" repair bridges). Deliberately floating islands stay.
        rid = 1
        for _ in range(3):
            tidy = enforce_minimums(bcut, cfg, design)
            bad = [c for c in components(tidy, sheet)[1] if skip is None or not c.intersects(skip)]
            if not bad:
                bcut = tidy
                break
            tidy, more, rid = auto_bridges(tidy, sheet, cfg, i + 1, None, hide, [], rid, tag="R", skip=skip)
            specs += more
            bcut = tidy
        marks = [cut.intersection(bridge_poly(s["p"], s["q"], s["width_mm"])) for s in specs]
        reg = reg_marks((-M, -M, DW + M, DH + M), cfg)
        v = validate(bcut, sheet, cfg, design, skip)
        vals.append(v)
        for s in specs:
            if s["length_mm"] > 15 * s["width_mm"]:
                warnings.append(dict(layer=i + 1, type="long_bridge", id=s["id"], length_mm=s["length_mm"],
                                     hint="long thin bridges flex and lift under spray; widen it, add "
                                          "bridges_per_island=2, or paint that region with a lower layer"))
        if v["floating_islands"]:
            warnings.append(dict(layer=i + 1, type="floating_islands", count=len(v["floating_islands"]),
                                 hint="left unbridged on purpose: cut out, keep, and place by hand "
                                      "(spray adhesive / transfer tape), or turn bridges back on"))
        out_layers.append(dict(color=L["color"], cut=shapely.affinity.translate(bcut, M, M),
                               reg=shapely.affinity.translate(reg, M, M),
                               bridge_marks=[shapely.affinity.translate(m, M, M) for m in marks],
                               bridges=specs, floating=v["floating_islands"]))

    border = [np.array([[0, 0], [SW, 0], [SW, SH], [0, SH]])]
    combined_svg, combined_dxf, gap = [], [], cfg["combined_gap_mm"]
    for i, Ly in enumerate(out_layers):
        cr, rr = ring_paths(Ly["cut"]), ring_paths(Ly["reg"])
        write_svg(os.path.join(args.out, f"layer_{i + 1}.svg"), SW, SH,
                  [(f"layer{i + 1}_cut", LB_COLORS[i], cr), ("registration", REG_COLOR, rr), ("border", BORDER_COLOR, border)])
        write_dxf(os.path.join(args.out, f"layer_{i + 1}.dxf"), SH,
                  [(f"LAYER{i + 1}_CUT", ACI[i], cr), ("REGISTRATION", ACI[6], rr), ("BORDER", ACI[7], border)])
        dx = i * (SW + gap)
        sh = lambda rs: [r + [dx, 0] for r in rs]
        combined_svg += [(f"layer{i + 1}_cut", LB_COLORS[i], sh(cr)), (f"layer{i + 1}_reg", REG_COLOR, sh(rr)),
                         (f"layer{i + 1}_border", BORDER_COLOR, sh(border))]
        combined_dxf += [(f"LAYER{i + 1}_CUT", ACI[i], sh(cr)), ("REGISTRATION", ACI[6], sh(rr)), ("BORDER", ACI[7], sh(border))]
    CW = len(out_layers) * SW + (len(out_layers) - 1) * gap
    write_svg(os.path.join(args.out, "combined.svg"), CW, SH, combined_svg)
    write_dxf(os.path.join(args.out, "combined.dxf"), SH, combined_dxf)
    pages = write_pdf(os.path.join(args.out, "stencil_print.pdf"), out_layers, SW, SH, cfg, M)
    write_preview(os.path.join(args.out, "preview.png"), out_layers, SW, SH, vals, M)

    # structural failures block "ok"; thin cuts only paint poorly, so they are warnings.
    # floating islands are fine only when they were left floating on purpose.
    ok = all(not v["islands"] and not v["thin_material"] for v in vals)
    for i, v in enumerate(vals):
        if v["thin_cut"]:
            warnings.append(dict(layer=i + 1, type="thin_cut", count=len(v["thin_cut"]),
                                 hint="openings narrower than min_cut_mm (often pointed tips) - they cut fine "
                                      "but may not pass paint; usually ignorable"))
    report = dict(
        ok=ok, version=__version__,
        size=dict(design=f"{fmt_len(DW, cfg['dpi'])}  x  {fmt_len(DH, cfg['dpi'])}",
                  sheet=f"{fmt_len(SW, cfg['dpi'])}  x  {fmt_len(SH, cfg['dpi'])}",
                  from_=cfg["_size_from"], pdf_page=cfg["pdf_page"]),
        sheet_mm=[round(SW, 2), round(SH, 2)], design_mm=[round(DW, 2), round(DH, 2)],
        coordinates="bridge/island/violation x,y are mm from the design (image) top-left; files add margin_mm",
        colors=color_info,
        settings={k: cfg[k] for k in ("profile", "mode", "color_mode", "layering", "min_cut_mm", "min_material_mm",
                                      "bridges", "bridge_layers", "bridge_width_mm", "bridges_per_island", "margin_mm")},
        warnings=warnings, separation=sep_info, pdf_pages_per_layer=pages,
        layers=[dict(layer=i + 1, color=Ly["color"], lightburn_color=LB_COLORS[i], paint=paints[i],
                     bridging=layer_bridging_on(cfg, i + 1),
                     bridges=[{k: (np.round(v, 2).tolist() if k in ("p", "q") else v) for k, v in b.items()} for b in Ly["bridges"]],
                     **vals[i]) for i, Ly in enumerate(out_layers)],
    )
    with open(os.path.join(args.out, "report.json"), "w") as f:
        json.dump(report, f, indent=1)
    with open(os.path.join(args.out, "params_used.json"), "w") as f:
        json.dump(public_cfg(cfg), f, indent=1)
    print(f"ok={ok} design={report['size']['design']}")
    print(f"   sheet={report['size']['sheet']}  ({cfg['_size_from']}, pdf {cfg['pdf_page']} x{pages}/layer)")
    if color_info:
        print(f"   colors: {color_info['used']} ({color_info['source']})")
    for i, Ly in enumerate(out_layers):
        v = vals[i]
        pn = f" paint={paints[i]['name']} dE{paints[i]['delta_e']}" if paints[i] else ""
        print(f"  L{i + 1} {Ly['color']}: pieces={v['material_pieces']} floating={len(v['floating_islands'])} "
              f"bridges={len(Ly['bridges'])} cut_shapes={v['cut_shapes']} thin_cut={len(v['thin_cut'])} "
              f"thin_material={len(v['thin_material'])}{pn}")
    for w in warnings:
        print(f"  WARN L{w['layer']} {w['type']}: " + {k: v for k, v in w.items() if k not in ('layer', 'type')}.__repr__())

if __name__ == "__main__":
    main()
