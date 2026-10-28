---
name: stencil-maker
description: Turn an image into cut-ready paint stencils (one or more color layers) with automatic, adjustable bridges so there are no floating islands; outputs SVG/DXF for LightBurn and a true-scale PDF for hand cutting.
---

# Stencil maker

The geometry comes from a **deterministic engine** (`scripts/stencil.py` in the GitHub repo). Claude's job is judgment:
- propose a color count
- pick sizes and parameters
- read the preview and decide where bridges look bad
- iterate, verify, deliver

**Claude never writes or edits path data by hand.** Claude's output is always parameters.

## 0. Setup (once per session)
```bash
git clone --depth 1 https://github.com/OWNER/stencil-maker stencil-maker 2>/dev/null || git -C stencil-maker pull -q
pip install --break-system-packages -q -r stencil-maker/requirements.txt
```
- **If the clone fails** (network blocked, or the repo is private and not attached), say so plainly and ask the person to attach the repo or the files. Don't rewrite the engine from memory.
- **Background removal:** install `rembg[cpu]` only if it's wanted. It downloads a ~176 MB model on first use.
- **Parameter reference:** `stencil-maker/README.md`, and the `DEFAULTS` block at the top of `scripts/stencil.py`.

## 1. Get the inputs (ask only for what's missing, all at once)
- **The image.**
- **One color or several?**
- **Size**, accepted in any form:
  - inches with fractions (`"10 1/2in"`), mm, cm, or px (converted using `dpi`, default 96) → `width` / `height`
  - "fit it on Letter / A4 / 11x17 / a 12x24in sheet" → `fit`
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
2. State the proposal in one line. For example: "4 colors = 3 paint layers: worst-pixel error drops sharply at 4, and 5 adds ~2× the islands for little gain."
3. Point out anything the error numbers miss: a small but important feature (eyes, text) that appears only at a higher count, or a count that doubles the islands.
4. Ask them to confirm or pick another count. Their number goes in as `n_layers` (paint layers) or `n_colors` (paint layers plus background); make clear which one you mean.

## 3. Make it
```bash
python stencil-maker/scripts/stencil.py IMAGE --out OUT --set n_layers=3 --set fit=letter --set profile=co2_mylar
```
Use `--params file.json` when there are more than a few settings.

## 4. Look before reporting
- Read `OUT/preview.png`:
  - one panel per layer: dark = cut, red = bridge ID (B1…), blue = floating island (I1…), magenta = problem
  - last panel: simulated painted result
- Read `OUT/report.json`: `ok`, `warnings`, `size`, the per-layer `bridges`, `floating_islands` and `paint`.
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
| too many bridges / speckle | fewer colors; raise `smooth_mm` (0.5–1.0), `min_area_mm2`, `min_island_mm2`; make it bigger |
| color missing or merged | raise `n_colors`, or `palette=[...]` |
| redundant layer warning | lower `n_colors` |
| "black and white shades" | `color_mode="gray"` |
| busy photo background | `remove_background=true` (check that it didn't eat pale parts of the subject) or `crop` |
| "match my paints" | `paint_chart="file.csv"` (`name,hex[,brand]`), optionally `snap_to_paint=true` |
| `long_bridge` warning | widen that bridge, `bridges_per_island=2`, or a different `layer_order` |
| `thin_cut` warning | usually pointed tips: they cut fine but won't pass much paint. Normally ignore it |

Bridge IDs stay the same between runs as long as the image, cleanup, keep-out and extra-bridge settings don't change. Re-read the preview after changing those.

## 6. Verify, then deliver
```bash
python stencil-maker/scripts/verify.py OUT
```
**Done** = `report.json` says `"ok": true` **and** verify prints `ALL PASS`.

Send these with SendUserFile:
- `combined.svg` (or the `layer_N.svg` files)
- `stencil_print.pdf` if the person is printing or hand cutting
- `preview.png`
- `params_used.json`, which reproduces the run exactly
- DXF only on request

Then state:
- the paint order and colors
- bridges per layer, and any floating islands
- the sheet size
- any warning worth acting on

**Measurements:** give every length in fractional inches plus mm, and px when asked. Gauge applies only to film thickness: 7 mil = 0.18 mm = 700 gauge.

## Limits to mention when relevant
- **Color separation is k-means posterization.** It's good on flat art and clean AI illustrations, rough on busy photos.
- **Bridges are straight.**
- **No bundled paint-brand data.** The example chart is generic.
- **Max 6 paint layers.**
- **LightBurn import isn't confirmed on real hardware yet.** Recommend a test cut.
