---
name: stencil-maker
description: Turn an image into cut-ready paint stencils (one or more color layers) with automatic, adjustable bridges so there are no floating islands; outputs SVG/DXF for LightBurn, a true-scale PDF for hand cutting, and an interactive color/opacity preview.
---

# Stencil maker

The geometry comes from a **deterministic engine**: `micahcirce/stencil-maker` on GitHub (`scripts/stencil.py`). Claude's job is judgment:
- propose a color count
- pick sizes and parameters
- read the preview and decide where bridges look bad
- iterate, verify, deliver

**Claude never writes or edits path data by hand.** Claude's output is always parameters.

## 0. Setup (once per session)
1. **Get the repo.** It's private, so attach it first if this session doesn't have it: call the add_repo tool with owner `micahcirce` and repo `stencil-maker`, then use the clone command it gives. Otherwise:
   ```bash
   git clone --depth 1 https://github.com/micahcirce/stencil-maker stencil-maker 2>/dev/null || git -C stencil-maker pull -q
   pip install --break-system-packages -q -r stencil-maker/requirements.txt
   ```
2. **If the clone fails,** say so plainly and ask the person to attach the repo or upload the files. Never rewrite the engine from memory.
3. **Background removal:** install `rembg[cpu]` only when it's wanted. It downloads a model of about 170 MB on first use.
4. **Parameter reference:** `stencil-maker/README.md`, and the `DEFAULTS` block at the top of `scripts/stencil.py`.

## 1. Get the inputs (ask only for what's missing, all at once)
- **The image.**
- **One color or several?**
- **Size**, accepted in any form:
  - inches with fractions (`"10 1/2in"`), mm, cm, or px (converted using `dpi`, default 96) → `width` / `height`
  - a percentage of the source → `scale`
  - "fit it on Letter / A4 / 11x17 / a 12x24in sheet" → `fit`
  - if both width and height are given, ask whether to keep proportions (`lock_aspect`, default true) or stretch
- **How it will be cut** → `profile`:
  - CO2 laser on mylar → `co2_mylar` (default)
  - diode laser on card → `diode_card`
  - printed and cut by hand → `hand_cut`

## 2. Propose the number of colors, then let the person decide
Skip this if the person already gave a number.
```bash
python stencil-maker/scripts/stencil.py IMAGE --out OUT --analyze [size options]
```
1. Read `OUT/options.png` and `OUT/analysis.json`, then send `options.png` to the person.
2. State the proposal in one line, with the reason.
3. Point out anything the error numbers miss: small but important features (eyes, text) that appear only at a higher count, or a count that doubles the islands.
4. Ask them to confirm or pick another count. Their number goes in as `n_layers` (paint layers) or `n_colors` (paint layers plus the unpainted background); make clear which one you mean.

## 3. Make it
```bash
python stencil-maker/scripts/stencil.py IMAGE --out OUT --set n_layers=3 --set fit=letter --set profile=co2_mylar
```
Use `--params file.json` when there are more than a few settings.

## 4. Look before reporting
- Read `OUT/preview.png`:
  - one panel per layer: dark = cut, red = bridge ID (B1…), blue = floating island (I1…), magenta = problem
  - last panel: simulated painted result
- **If the background was removed,** read `OUT/cutout.png` too, and check that nothing important was dropped (pale fur, highlights).
- Read `OUT/report.json`: `ok`, `warnings`, `size` (including `aspect_stretch`), and per layer `bridges`, `floating_islands`, `paint` and `vector_vs_pixel_area_diff`.
- **If `vector_mismatch` appears or `ok` is false, the geometry isn't trustworthy.** Don't deliver it; say so.
- Judge the painted result against the image:
  - Is the subject readable?
  - Is a bridge across a face, an eye or text?
  - Is any layer redundant?

## 5. Adjust (change one or two parameters per run and say which)
| Person says / you see | Parameter |
|---|---|
| "no bridges" / "I'll place islands by hand" | `bridges=false` |
| "bridges only on layer 2" | `bridge_layers=[2]` |
| "turn off B3" / "move B5" / "B2 thicker" | `bridge_overrides={"L1-B3":{"off":true}}`, `{"move_to":[x,y]}`, `{"width_mm":2}` |
| "no bridges on the face" | `keepout=[[x0,y0,x1,y1]]` (mm from the design's top-left; use coordinates from report.json) |
| a big island flops | `bridges_per_island=2` |
| "layer 2 in red" / "I'll use navy for the base" | `layer_colors={"2":"#c2452d"}` (display only; the cut doesn't change) |
| "make layer 3 see-through" | `layer_opacity={"3":50}` (preview only; default 100) |
| "it's going on a gray wall" | `surface_color="#8a8a8a"` |
| "match my paints" | `paint_chart="file.csv"` (`name,hex[,brand]`), optionally `snap_to_paint=true`; the person can also load the chart in `preview.html` |
| "stretch it to 10 x 10 in" | `width="10in"`, `height="10in"`, `lock_aspect=false` |
| "half size" / "make it wider only" | `scale=50` / `scale=[120,100]` |
| busy background | `remove_background=true` (+ `crop="subject"`); for lost edges lower `alpha_threshold` (e.g. 60) or use `bg_grow_mm=0.5` |
| "only this part of the picture" | `crop=[x0,y0,x1,y1]` (pixels, or fractions 0–1) |
| too many bridges / speckle | fewer colors; raise `smooth_mm` (0.5–1.0), `min_area_mm2`, `min_island_mm2`; make it bigger |
| color missing or merged | raise `n_colors`, or `palette=[...]` (this changes the separation) |
| `long_bridge` warning | widen that bridge, `bridges_per_island=2`, or a different `layer_order` |
| `thin_cut` warning | usually pointed tips: they cut fine but won't pass much paint. Normally ignore it |

Bridge IDs stay the same between runs as long as the image, cleanup, keep-out and extra-bridge settings don't change. Re-read the preview after changing those.

**Choices the person makes in `preview.html`** (colors, opacity, surface) come back as JSON from its "Copy settings" button. Merge that JSON into the params and re-run, so the PDF swatches and the report match.

## 6. Verify, then deliver
```bash
python stencil-maker/scripts/verify.py OUT
```
**Done** = `report.json` says `"ok": true` **and** verify prints `ALL PASS`.

Send these with SendUserFile:
- `preview.html`, the interactive view: color pickers, opacity sliders, stencil views
- `combined.svg` (or the `layer_N.svg` files)
- `stencil_print.pdf` if the person is printing or hand cutting
- `params_used.json`, which reproduces the run exactly
- DXF only on request

Then state:
- the paint order and colors
- bridges per layer, and any floating islands
- the sheet size
- any warning worth acting on

**LightBurn:** paint layers import on LightBurn layers 00–27, registration marks on 28 and the border on 29.

**Measurements:** give every length in fractional inches plus mm, and px when asked. Gauge applies only to film thickness: 7 mil = 0.18 mm = 700 gauge.

## Limits to mention when relevant
- **Color separation is k-means posterization.** It's good on flat art and clean AI illustrations, rough on busy photos.
- **Bridges are straight.**
- **No bundled paint-brand data.** The example chart is generic.
- **Up to 28 paint layers.**
- **LightBurn import isn't confirmed on real hardware yet.** Recommend a test cut.
