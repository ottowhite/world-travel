# CLAUDE.md

## Project

Download CHELSA V2.1 monthly climatologies (1981–2010) of temperature (`tas`)
and precipitation (`pr`), plus the static elevation/altitude DEM (`orog`), from
EnviDat/EnviCloud and explore them in a full-screen, dynamically-rendered map
viewer (pan/zoom/wrap, per-month slider).

## Layout

- `scripts/download_data.py` — streams monthly GeoTIFFs into `data/` (gitignored);
  idempotent (HEAD-size skip), so re-running fetches only what's missing. The
  static `orog` layer is CHELSA's input DEM (`dem_latlong.nc`, ~3.6 GB float32) on
  the SAME 30 arc-sec grid; it's streamed once then transcoded locally to a tiled
  **int16** GeoTIFF with internal overviews (`CHELSA_orog_1981-2010_V.2.1.tif`) and
  the source `.nc` is deleted. `--vars orog` fetches just altitude.
- `scripts/serve.py` — the viewer. A stdlib `ThreadingHTTPServer`: `GET /` serves a
  full-page `<canvas>` page; `GET /render?var&month&west&east&south&north&w&h` does a
  windowed/decimated rasterio read of the visible bbox (using the GeoTIFFs' internal
  overviews), applies CHELSA scaling, and returns an 8-bit **palette PNG** (Pillow,
  `mode="P"`, `compress_level=9`). The palette is laid out as `0` = nodata (made
  transparent via tRNS), `1..PAL_DATA_LEVELS` = data colours subsampled from the
  256-entry LUT, `255` = ocean. `PAL_DATA_LEVELS = 64` is the wire-size sweet spot:
  1.25 °C per band for `tas`, ~12 per decade for `pr` (log) — visually indistinguishable
  from the full LUT, but deflate sees ~4× fewer distinct bytes and a full-screen tile
  lands around 400 KB (vs ~19 MB raw RGBA).
  Wrap on both axes = tiling the read over world copies; the colour range is a FIXED
  absolute per-variable range set in each `VARIABLES` entry and exposed via the page
  config, so the colour bar is static for a variable and never rescales on pan/zoom.
  `tas`: ROYGBIV `stops` (violet=cold..red=hot), linear −40..40 °C. `pr`: `devon_r`
  cmap, LOG10 over 1..400 mm/month (`"log": True`; saturates ≥400). `orog`:
  hypsometric `stops` (green→tan→brown→snow), linear 0..6000 m; it's a `"static"`
  layer (one month-less GeoTIFF, `"scale": 1.0`, identity convert) so `render()`/
  `value_at()` ignore the month, and the front-end greys out the slider + play.
  Front-end: left-drag pan, wheel
  zoom, var toggle, a month slider with a labelled notch per month (datalist ticks +
  positioned `#monthTicks` labels, click a label to jump), play, colour bar, and a
  hover read-out (`GET /value?var&month&lon&lat` → JSON value at a point, throttled).
  No build step / no `output/`.
  `GET /coastline.geojson` serves the bundled coastline (read once, cached). The
  front-end fetches it once, flattens LineString/MultiLineString to lon/lat
  polylines, and in `draw()` strokes them over the raster (dark halo + light line)
  replicated across the SAME ±MAXK wrap copies the raster uses. A default-on
  "Coastlines" checkbox toggles both the overlay AND server-side OCEAN MASKING:
  when on, `/render?mask=1` rasterizes the Natural Earth land polygons
  (`assets/ne_50m_land.geojson`, via `rasterio.features.rasterize`) per wrap tile and
  paints non-land pixels pale blue (`OCEAN_RGB`); off → raw global field. Toggling
  refetches since masking is server-side.
- `assets/ne_50m_coastline.geojson` / `assets/ne_50m_land.geojson` — Natural Earth
  1:50m coastline (overlay) and land polygons (ocean mask), EPSG:4326, ~1.6 MB each,
  committed (NOT gitignored) so the viewer works offline/reproducibly.
- `Makefile` — `make serve` (PORT=…) runs the viewer; `make download` fetches data.
- `shell.nix` / `.envrc` — Nix dev shell (uv + curl + nodejs_22), loaded by direnv (`use nix`).
- `.mcp.json` — Playwright MCP server, launched via `nix-shell --run "npx -y @playwright/mcp ..."`
  (needs the `nodejs_22` from `shell.nix`). Profile/cache live in `.cache/` (gitignored).
- `pyproject.toml` / `uv.lock` — deps: rasterio, numpy, pillow, plotly. Python 3.13 via uv.

## Data details (CHELSA V2.1)

- 30 arc-sec global GeoTIFFs, `uint16`, nodata `65535`, scale `0.1`.
- Coverage: lon −180..180, lat −90..84 (EPSG:4326).
- `tas`: °C = DN·0.1 − 273.15.  `pr`: mm/month = DN·0.1.
- `tas` colour map: the perceptually-uniform diverging "vik" (Fabio Crameri, via
  `cmcrameri`), with the fixed symmetric LINEAR range −40..40 °C so vik's neutral
  midpoint lands exactly at 0 °C (cold blue → hot red).
- `pr` colour map: the perceptually-uniform sequential "devon_r" (`cmcrameri`),
  on a LOG10 range 1..2000 mm/month (dry pale → wet deep blue). Precip is strongly
  right-skewed, so log normalisation reveals mid-range continental gradation that a
  linear map flattens. The client colour bar mid label is the geometric mean
  (√(1·2000) ≈ 45 mm), not the arithmetic mean.
  In `serve.py`, a variable specifies either `"cmap"` (named scientific colormap,
  sampled into the LUT and into ~33 client colour-bar stops) or `"stops"`; an
  optional `"log": True` switches `render()` and the colour-bar labels to log space.
  `"static": True` marks a month-less layer (one file via `tif_path`), and
  `"scale"` overrides the default `0.1` DN→physical factor (`orog` uses `1.0`,
  metres). `tif_path(var, month=None)` returns the month-less name for static vars.
- `orog` (altitude): 30 arc-sec global DEM, EPSG:4326, same grid as the climate
  layers; stored as int16 metres, nodata −32768 (ocean), so it's transparent over
  sea and obeys the same coastline ocean-mask toggle.
- URL pattern:
  `https://os.unil.cloud.switch.ch/chelsa02/chelsa/global/climatologies/{var}/1981-2010/CHELSA_{var}_{MM}_1981-2010_V.2.1.tif`

## Commands

```sh
make download                       # ~6 GB into data/
make serve                          # viewer at http://localhost:8765
```

Note: `plotly` is still a declared dep but is no longer used by the viewer
(which renders via canvas); safe to drop from `pyproject.toml` if desired.

## Conventions

- Atomic git commits with standard tags; push after each commit to `origin`
  (`git@github.com:ottowhite/world-travel.git`).
- Keep this file up to date as the project changes.
