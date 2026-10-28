#!/usr/bin/env python3
"""verify.py OUT_DIR - re-check the exported files without using stencil.py's geometry.

Reads each layer_N.svg and layer_N.dxf from disk, rebuilds the cut regions (even-odd),
and checks: mm units, closed paths, one connected material piece, SVG/DXF agreement.
Islands the user deliberately left floating (bridges off) are allowed: their count is
read from report.json, and the file must contain exactly that many extra pieces.
Exit code 0 = all pass."""
import sys, os, re, glob, json
import ezdxf
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

def evenodd(rings):
    g = None
    for r in rings:
        p = Polygon(r).buffer(0)
        g = p if g is None else g.symmetric_difference(p)
    return g

def svg_rings(path):
    s = open(path).read()
    W = float(re.search(r'width="([\d.]+)mm"', s).group(1))
    H = float(re.search(r'height="([\d.]+)mm"', s).group(1))
    groups = {}
    for gid, d in re.findall(r'<g id="([^"]+)"><path d="([^"]+)"', s):
        rings = []
        for sub in d.split("Z"):
            pts = re.findall(r"(-?[\d.]+),(-?[\d.]+)", sub)
            if len(pts) >= 3:
                rings.append([(float(x), float(y)) for x, y in pts])
        groups[gid] = rings
    return W, H, groups

def main(out):
    ok = True
    allowed = {}
    rp = os.path.join(out, "report.json")
    if os.path.exists(rp):
        for L in json.load(open(rp)).get("layers", []):
            allowed[str(L["layer"])] = len(L.get("floating_islands", []))
    for svg in sorted(glob.glob(os.path.join(out, "layer_*.svg"))):
        n = re.search(r"layer_(\d+)", svg).group(1)
        W, H, groups = svg_rings(svg)
        cut_rings = groups.get(f"layer{n}_cut", [])
        reg_rings = groups.get("registration", [])
        cut = unary_union([evenodd(cut_rings), evenodd(reg_rings)]) if reg_rings else evenodd(cut_rings)
        sheet = box(0, 0, W, H)
        mat = sheet.difference(cut)
        pieces = len(mat.geoms) if hasattr(mat, "geoms") else 1
        outside = cut.difference(sheet.buffer(-0.01)).area
        doc = ezdxf.readfile(svg[:-4] + ".dxf")
        units = doc.header.get("$INSUNITS")
        polys = list(doc.modelspace().query("LWPOLYLINE"))
        open_polys = sum(1 for p in polys if not p.closed)
        n_svg = sum(len(r) for r in groups.values())
        expect = 1 + allowed.get(n, 0)
        line_ok = pieces == expect and outside < 1e-6 and units == 4 and open_polys == 0 and len(polys) == n_svg
        ok &= line_ok
        print(f"{os.path.basename(svg)}: {W:.1f}x{H:.1f} mm  material_pieces={pieces} (expected {expect})  cut_area={cut.area:.0f} mm2  "
              f"dxf_units={'mm' if units == 4 else units} dxf_polylines={len(polys)} svg_rings={n_svg} "
              f"open={open_polys}  {'PASS' if line_ok else 'FAIL'}")
    print("ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
