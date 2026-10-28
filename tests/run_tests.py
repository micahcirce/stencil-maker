#!/usr/bin/env python3
"""Regression tests: python tests/run_tests.py  (exit 0 = all pass, ~1-2 min)

Each case runs stencil.py, then checks report.json and the independent verify.py.
Finally it checks determinism: re-running from params_used.json must give identical files."""
import json, os, subprocess, sys, tempfile, filecmp

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
PY = sys.executable

def run(args):
    r = subprocess.run([PY, os.path.join(SCRIPTS, "stencil.py")] + args, capture_output=True, text=True)
    if r.returncode:
        raise AssertionError(r.stderr[-2000:])
    return r.stdout

def verify(out):
    r = subprocess.run([PY, os.path.join(SCRIPTS, "verify.py"), out], capture_output=True, text=True)
    return r.returncode == 0, r.stdout

def report(out):
    return json.load(open(os.path.join(out, "report.json")))

def main():
    tmp = tempfile.mkdtemp(prefix="stencil-tests-")
    subprocess.run([PY, os.path.join(HERE, "make_test_images.py"), tmp], check=True, capture_output=True)
    isl, face = os.path.join(tmp, "islands.png"), os.path.join(tmp, "face.png")
    edge, subj = os.path.join(tmp, "edge.png"), os.path.join(tmp, "subject.png")
    paints = os.path.join(HERE, "..", "examples", "example_paints.csv")
    cases = [
        ("single, bridged", [isl, "--set", "mode=single", "--set", "width_mm=250"],
         lambda r: r["layers"][0]["material_pieces"] == 1 and len(r["layers"][0]["bridges"]) >= 10),
        ("single, bridges off", [isl, "--set", "mode=single", "--set", 'width="10in"', "--set", "bridges=false"],
         lambda r: len(r["layers"][0]["floating_islands"]) >= 10 and not r["layers"][0]["bridges"]),
        ("single, one bridge off", [isl, "--set", "mode=single", "--set", "width_mm=250", "--set",
                                    'bridge_overrides={"L1-B1":{"off":true}}'],
         lambda r: len(r["layers"][0]["floating_islands"]) == 1),
        ("multi auto colors, fit letter", [face, "--set", "fit=letter"],
         lambda r: r["colors"]["used"] == 4 and r["pdf_pages_per_layer"] == 1),
        ("multi 4 colors, a4, paint chart", [face, "--set", "n_colors=4", "--set", "fit=a4",
                                             "--set", f"paint_chart={json.dumps(paints)}"],
         lambda r: all(L["paint"] for L in r["layers"])),
        ("gray, 2 layers, px size", [face, "--set", "color_mode=gray", "--set", "n_layers=2",
                                     "--set", 'width="900px"'],
         lambda r: len(r["layers"]) == 2 and abs(r["design_mm"][0] - 238.1) < 0.2),
        ("per-layer bridges", [face, "--set", "n_colors=4", "--set", "bridge_layers=[2]"],
         lambda r: not r["layers"][0]["bridging"] and r["layers"][1]["bridging"]),
        ("overrides + keepout + extra", [isl, "--set", "mode=single", "--set", "width_mm=250",
                                         "--set", "profile=hand_cut", "--set", "bridges_per_island=2",
                                         "--set", 'bridge_overrides={"L1-B1":{"width_mm":5},"L1-B2":{"delete":true}}',
                                         "--set", "keepout=[[120,110,190,170]]",
                                         "--set", 'extra_bridges=[{"layer":1,"at":[230,135],"angle_deg":0}]'],
         lambda r: any(b["source"] == "extra" and "note" in b for b in r["layers"][0]["bridges"])),
        ("edge-touching paint (contour regression)", [edge, "--set", "mode=single", "--set", "width_mm=200"],
         lambda r: r["layers"][0]["vector_vs_pixel_area_diff"] < 0.01
                   and len(r["layers"][0]["bridges"]) >= 3),
        ("crop to subject", [subj, "--set", 'crop="subject"', "--set", "n_layers=2", "--set", "width_mm=150"],
         lambda r: r["image"]["used_px"][0] < 400 and r["image"]["crop_px"][0] > 200),
        ("layer colors + opacity", [face, "--set", "n_colors=4", "--set", 'layer_colors={"1":"#3a7bd5"}',
                                    "--set", "layer_opacity=[100,50,25]", "--set", 'surface_color="#ddd5c4"'],
         lambda r: r["layers"][0]["color"] == "#3a7bd5" and r["layers"][2]["opacity"] == 25
                   and r["layers"][0]["cluster_color"] != "#3a7bd5"),
        ("scale 50% (aspect locked)", [isl, "--set", "mode=single", "--set", "scale=50"],
         lambda r: abs(r["design_mm"][0] - 158.75) < 0.1 and r["size"]["aspect_stretch"] == 1.0),
        ("stretch 10x10in (aspect unlocked)", [isl, "--set", "mode=single", "--set", 'width="10in"',
                                               "--set", 'height="10in"', "--set", "lock_aspect=false"],
         lambda r: abs(r["design_mm"][1] - 254.0) < 0.2 and abs(r["size"]["aspect_stretch"] - 1.5) < 0.01),
        ("locked 10x10in box", [isl, "--set", "mode=single", "--set", 'width="10in"', "--set", 'height="10in"'],
         lambda r: abs(r["design_mm"][0] - 254.0) < 0.2 and r["size"]["aspect_stretch"] == 1.0),
    ]
    failed = 0
    for name, args, check in cases:
        out = os.path.join(tmp, name.replace(" ", "_").replace(",", ""))
        try:
            run(args + ["--out", out])
            r = report(out)
            vok, vtxt = verify(out)
            assert r["ok"], "report ok=false"
            assert vok, "verify failed:\n" + vtxt
            assert check(r), "case check failed"
            assert os.path.getsize(os.path.join(out, "preview.html")) > 1000, "preview.html missing"
            print(f"PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}: {e}")
    # analyze mode
    out = os.path.join(tmp, "analyze")
    run([face, "--analyze", "--out", out])
    a = json.load(open(os.path.join(out, "analysis.json")))
    ok = a["proposed_n_colors"] == 4 and os.path.exists(os.path.join(out, "options.png"))
    print(("PASS" if ok else "FAIL") + f"  analyze proposes 4 colors for face.png (got {a['proposed_n_colors']})")
    failed += not ok
    # determinism
    a, b = os.path.join(tmp, "det_a"), os.path.join(tmp, "det_b")
    run([face, "--set", "fit=letter", "--out", a])
    run([face, "--params", os.path.join(a, "params_used.json"), "--out", b])
    same = all(filecmp.cmp(os.path.join(a, f), os.path.join(b, f), shallow=False)
               for f in os.listdir(a) if f.endswith((".svg", ".dxf")))
    print(("PASS" if same else "FAIL") + "  rerun from params_used.json gives identical SVG/DXF")
    failed += not same
    print(f"\n{'ALL TESTS PASS' if not failed else f'{failed} FAILED'}  (outputs in {tmp})")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
