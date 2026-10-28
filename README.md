# stencil-maker

Turn an image into cut-ready paint stencils: one or more color layers, with **automatic, adjustable bridges** so no piece of the stencil floats free. Outputs **SVG and DXF for LightBurn** (and other laser/plotter software) and a **true-scale PDF** for printing and hand cutting.

```
image ──► color separation ──► cleanup to cuttable minimums ──► vectors ──► bridges ──► checks ──► SVG / DXF / PDF
          (auto color count)    (min cut, min material width)               (auto + your edits)   (report.json + verify.py)
```

## How it is built: deterministic engine + AI driver

| Part | What it does | Why it's built this way |
|---|---|---|
| `scripts/stencil.py` (engine) | All geometry: color separation, cleanup, bridges, export | Has a right answer and must be repeatable. The same image and parameters always produce byte-identical files (`params_used.json` reproduces any run). Testable. |
| `scripts/verify.py` (checker) | Re-reads the exported files and checks units, closed paths, one-piece material | Doesn't trust the engine's own bookkeeping |
| `SKILL.md` (AI skill) | Tells Claude how to drive the engine: propose a color count, read the preview, judge where bridges look bad, adjust parameters, verify, deliver | These are judgment calls. Is the subject readable? Does a bridge cross the face? The AI's output is **parameters, never path data**. |

The engine runs fine without any AI. The skill is a convenience layer.

## Install

```bash
pip install -r requirements.txt            # Python 3.10+
pip install "rembg[cpu]"                    # optional: automatic background removal
python tests/run_tests.py                   # ~30 s, should print ALL TESTS PASS
```

## Use

```bash
# 1. Let it propose a color count (writes options.png: posterized previews for 2-7 colors)
python scripts/stencil.py art.png --out out --analyze

# 2. Make the stencil (auto color count, fit everything on one Letter sheet)
python scripts/stencil.py art.png --out out --set fit=letter

# 3. Your choices
python scripts/stencil.py art.png --out out --set n_layers=3 --set 'width="11 1/2in"' --set profile=hand_cut

# 4. Check the files independently
python scripts/verify.py out
```

Put parameters in a JSON file and pass `--params my.json`; `--set key=value` overrides it. Values are JSON, so strings need quotes: `--set 'width="10in"'`.

### Outputs (in `--out`)
| File | For |
|---|---|
| `layer_N.svg`, `layer_N.dxf` | one file per stencil layer, mm units, closed paths |
| `combined.svg`, `combined.dxf` | all layers side by side (cut them all from one sheet) |
| `stencil_print.pdf` | cover page with 1 in and 50 mm calibration squares, then each layer at true scale, tiled or fit to one page |
| `preview.html` | **interactive**: per-layer color picker, opacity slider (default 100 %), show/hide, surface color, single-stencil views, load a paint chart CSV in the browser, "Copy settings" → JSON for `--params` |
| `preview.png` | every layer with bridge IDs (red), floating islands (blue I1…), problems (magenta), and a simulated painted result |
| `cutout.png` | background removal only: the subject over magenta, to check nothing important was removed |
| `report.json` | measured checks, `"ok": true/false`, sizes in in/mm/px, bridges, warnings |
| `params_used.json` | every effective parameter; re-run with `--params` to reproduce exactly |
| `options.png`, `analysis.json` | `--analyze` only: color-count options with error and island estimates |

**LightBurn:** strokes use LightBurn's exact default palette, because LightBurn assigns imported shapes to layers by exact color match:
- paint layers: layers 00–27, so up to **28 paint layers**
- registration marks: layer 28 (`#86FA88`)
- sheet border: layer 29 (`#FFDB66`)

T1/T2 are LightBurn tool layers that don't cut, so they're not used. Geometry is at true size; set kerf offset in LightBurn per material. ([palette reference](https://docs.lightburnsoftware.com/LayerColors.html))

## Main parameters

**Colors and layers**
- `n_colors`: `"auto"` (default; proposed by the error curve), or a number that *includes* the unpainted background color
- `n_layers`: the same choice counted as paint layers, e.g. `n_layers=3`
- `mode`: `single` uses one gray threshold (`threshold`, `invert`)
- `color_mode`: `gray` gives value-only layers
- `palette`: force exact colors
- `background`: `auto`, `none` or a hex color
- `layering`: `stacked` (default; each layer also cuts every color above it, so later layers cover misregistration and many bridges) or `exact`
- `layer_order`: `auto` (fewest islands, with a penalty for light paint over dark), `light_to_dark` or a list of colors

**Size and scale** (later ones override earlier ones)
- `width_mm`
- `scale`: % of the source image at `dpi`, e.g. `50`. Use `[x%, y%]` to scale the axes separately (aspect unlocked).
- `width` and/or `height` with units: `"10in"`, `"10 1/2in"`, `"254mm"`, `"25.4cm"`, `"1200px"` (converted using `dpi`, default 96)
  - With both, `lock_aspect: true` (default) fits the design inside that box.
  - `lock_aspect: false` stretches it to exactly that size.
- `report.json` → `size.aspect_stretch` shows the distortion (1.0 = none).
- `fit` to a paper or material sheet (with `lock_aspect: false` it stretches to fill):
  - US paper: `letter`, `legal`, `tabloid`/`ledger`, `arch-a` … `arch-d`
  - ISO paper: `a5` … `a0`, `b4`, `b3`
  - material sheet: e.g. `"12x24in"` or `"300x600mm"`
- `orientation`: `auto`, `portrait` or `landscape`
- `fit_includes_margin` (default true)
- `print_margin_mm`: the printer's unprintable edge, 6.35 mm (1/4 in); applies to paper sizes only

**Cutting minimums**
- `profile`: `co2_mylar` (default), `diode_card` or `hand_cut`
- or set `min_cut_mm`, `min_material_mm` and `bridge_width_mm` directly
- `margin_mm`: solid border, default 25

**Bridges**
- `bridges`: `true`/`false`, a master switch. Off leaves islands floating, numbered for hand placement.
- `bridge_layers`: `"all"` or e.g. `[1, 3]`
- `bridge_width_mm`, `bridges_per_island`
- `bridge_angle_deg` + `bridge_angle_weight`
- `hide_bridges`: prefer spots a later layer paints over
- `keepout`: `[[x0,y0,x1,y1], …]` mm from the design's top-left
- `bridge_overrides`, keyed by preview ID:
  - `{"L1-B3": {"width_mm": 2}}` makes that bridge wider
  - `{"delete": true}` means not here; it is re-placed elsewhere
  - `{"move_to": [x, y]}`
  - `{"off": true}` leaves that island floating
- `extra_bridges`: `[{"layer": 1, "at": [x, y], "angle_deg": 90, "width_mm": 2}]`

**Image prep**
- `crop`: `[x0,y0,x1,y1]` in pixels, or fractions 0–1, or `"subject"` to trim to the subject after background removal (or on a transparent PNG); `crop_pad` sets the margin
- `remove_background` (needs rembg)
  - `bg_model`: default `isnet-general-use`, which kept pale fur that `u2net` dropped in testing
  - `alpha_threshold`: 0–255; lower keeps wispy edges
  - `bg_fill_holes`: fills holes enclosed by the subject
  - `bg_grow_mm`: + grows, − shrinks the subject outline
  - check `cutout.png` every time
- `denoise`
- `smooth_mm`
- `min_area_mm2`, `min_island_mm2`: detail floor

**Display colors and paint** (previews, PDF swatches and the report only; they never change the cut)
- `layer_colors`: `["#hex", …]` in layer order, or `{"2": "#hex"}`
- `layer_opacity`: `[100, 60, …]` or `{"3": 40}`, in %, default 100
- `surface_color`: the wall or substrate under the paint
- `paint_chart`: a CSV of `name,hex[,brand]`; reports the nearest paint per layer (ΔE76). It can also be loaded later inside `preview.html`.
- `snap_to_paint`: recolors the layers to those paints
- `examples/example_paints.csv` shows the format and is **not** real brand data
- `palette` is different: it forces the *separation* onto exact colors, so it changes the cut

Full list with defaults: the `DEFAULTS` block at the top of `scripts/stencil.py`.

## Profiles (shop-practice starting points, not standards)
| profile | min bridge | min opening | min material |
|---|---|---|---|
| `co2_mylar` | 1.0 mm (≈3/64 in) | 0.8 mm (≈1/32 in) | 0.8 mm |
| `diode_card` | 1.5 mm (≈1/16 in) | 1.0 mm | 1.2 mm |
| `hand_cut` | 3.0 mm (≈1/8 in) | 1.5 mm | 2.0 mm |

## Known limits
- **Color separation is k-means posterization.** It works well on flat art and clean AI illustrations, and poorly on busy photos (expect many bridges).
- **Background removal can still drop pale parts of the subject.** Check `cutout.png`.
- **Bridges are straight.** There's no curved bridge, and no "split island across two layers" mode yet.
- **Paths are polylines,** not Béziers.
- **No built-in paint-brand charts.** Screen colors only approximate real paint, so bring your own chart.
- **Cutting isn't verified yet.** LightBurn import (stroke color → layer, mm scale) hasn't been tested on real hardware. Do a test cut.

## Roadmap ideas (compared with Stencil Lab)
| Stencil Lab feature | Status here |
|---|---|
| Choose layer count | yes (`n_colors` / `n_layers`, plus auto proposal) |
| Background remover | yes, optional (`remove_background`) |
| Color or greyscale output | yes (`color_mode`) |
| Paint-brand matching | yes, with your chart (`paint_chart`, or load it in `preview.html`); no bundled brand data |
| Change layer colors live | yes: `preview.html` color pickers and opacity sliders |
| Adjustable registration marks | size only (`reg_size_mm`) |
| Real-time geometry sliders | no: color and opacity are live, but geometry changes need a re-run |
| Up to 20 layers | up to 28 paint layers (LightBurn layers 00–27) |
| Cutting service, gallery, marketplace | out of scope |

## Self-checks (why `"ok": true` means something)
- **Connectivity:** every layer is one piece, apart from islands you deliberately left floating.
- **Width:** no material is thinner than the minimum.
- **Vector check:** the vector cut area matches the pixel mask within 3% (`vector_vs_pixel_area_diff`). This catches geometry bugs the other checks can't. It found one in v0.2, where paint touching the image edge came out inside-out.
- **File check:** `verify.py` re-reads the files from disk.
- **Repeatability:** re-running from `params_used.json` must give byte-identical SVG/DXF; the test suite checks this.

## Changelog
- **0.3.0**
  - Full LightBurn palette: 28 paint layers, with registration marks and border on layers 28/29.
  - Interactive `preview.html`: colors, opacity, paint chart.
  - `layer_colors`, `layer_opacity`, `surface_color`.
  - `scale` and `lock_aspect`.
  - `crop="subject"`.
  - Better background removal: isnet model, hole fill, grow/shrink, `cutout.png`.
  - **Fixed:** paint touching the image edge could be vectorized inside-out. Added the vector-vs-pixel self-check and a regression test.
- **0.2.0**
  - Automatic color-count proposal, paper fit, imperial/metric/px sizing, bridge on/off switches, grayscale, crop, background removal, paint chart.
- **0.1.0**
  - First version.

## License
MIT
