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
| `preview.png` | every layer with bridge IDs (red), floating islands (blue I1…), problems (magenta), and a simulated painted result |
| `report.json` | measured checks, `"ok": true/false`, sizes in in/mm/px, bridges, warnings |
| `params_used.json` | every effective parameter; re-run with `--params` to reproduce exactly |
| `options.png`, `analysis.json` | `--analyze` only: color-count options with error and island estimates |

**LightBurn:** each SVG stroke color imports as its own layer:
- layer cuts: 00 black, 01 blue, 02 red, 03 green, 04 yellow, 05 orange
- registration marks: 06 cyan
- sheet border: 07 magenta

Geometry is at true size; set kerf offset in LightBurn per material.

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

**Size** (pick one)
- `width_mm`
- `width` and/or `height` with units: `"10in"`, `"10 1/2in"`, `"254mm"`, `"25.4cm"`, `"1200px"` (converted using `dpi`, default 96). Giving both fits the design inside that box.
- `fit` to a paper or material sheet:
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
- `crop`: `[x0,y0,x1,y1]` in pixels, or fractions 0–1
- `remove_background` (needs rembg)
- `denoise`
- `smooth_mm`
- `min_area_mm2`, `min_island_mm2`: detail floor

**Paint**
- `paint_chart`: a CSV of `name,hex[,brand]`; reports the nearest paint per layer
- `snap_to_paint`: recolors the layers to those paints
- `examples/example_paints.csv` shows the format and is **not** real brand data

Full list with defaults: the `DEFAULTS` block at the top of `scripts/stencil.py`.

## Profiles (shop-practice starting points, not standards)
| profile | min bridge | min opening | min material |
|---|---|---|---|
| `co2_mylar` | 1.0 mm (≈3/64 in) | 0.8 mm (≈1/32 in) | 0.8 mm |
| `diode_card` | 1.5 mm (≈1/16 in) | 1.0 mm | 1.2 mm |
| `hand_cut` | 3.0 mm (≈1/8 in) | 1.5 mm | 2.0 mm |

## Known limits
- **Color separation is k-means posterization.** It works well on flat art and clean AI illustrations, and poorly on busy photos (expect many bridges).
- **Background removal can drop white or pale parts of the subject**, such as fur or highlights. Check the preview.
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
| Paint-brand matching | mechanism yes (`paint_chart`); no bundled brand data |
| Adjustable registration marks | size only (`reg_size_mm`) |
| Real-time sliders / editor | no; would be a UI on top of this engine |
| Up to 20 layers | max 6 paint layers (LightBurn color mapping) |
| Cutting service, gallery, marketplace | out of scope |

## License
MIT
