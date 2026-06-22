# CLAUDE.md

## Project

Download CHELSA V2.1 monthly climatologies (1981–2010) of temperature (`tas`)
and precipitation (`pr`) from EnviDat/EnviCloud and explore them in a
full-screen, dynamically-rendered map viewer (pan/zoom/wrap, per-month slider).

## Layout

- `scripts/download_data.py` — streams monthly GeoTIFFs into `data/` (gitignored).
- `scripts/serve.py` — the viewer. A stdlib `ThreadingHTTPServer`: `GET /` serves a
  full-page `<canvas>` page; `GET /render?var&month&west&east&south&north&w&h` does a
  windowed/decimated rasterio read of the visible bbox (using the GeoTIFFs' internal
  overviews), applies CHELSA scaling + a numpy colour-map LUT, and returns raw RGBA bytes.
  Wrap on both axes = tiling the read over world copies; the colour range is a FIXED
  absolute per-variable range (`tas` −40..40 °C, `pr` 0..800 mm/month) set in each
  `VARIABLES` entry's `vmin`/`vmax` and exposed via the page config, so the colour bar
  is static for a variable and never rescales on pan/zoom. Front-end: right-drag pan,
  wheel zoom, var toggle, month slider+play, colour bar. No build step / no `output/`.
- `Makefile` — `make serve` (PORT=…) runs the viewer; `make download` fetches data.
- `shell.nix` / `.envrc` — Nix dev shell (uv + curl + nodejs_23), loaded by direnv (`use nix`).
- `.mcp.json` — Playwright MCP server, launched via `nix-shell --run "npx -y @playwright/mcp ..."`
  (needs the `nodejs_23` from `shell.nix`). Profile/cache live in `.cache/` (gitignored).
- `pyproject.toml` / `uv.lock` — deps: rasterio, numpy, plotly. Python 3.13 via uv.

## Data details (CHELSA V2.1)

- 30 arc-sec global GeoTIFFs, `uint16`, nodata `65535`, scale `0.1`.
- Coverage: lon −180..180, lat −90..84 (EPSG:4326).
- `tas`: °C = DN·0.1 − 273.15.  `pr`: mm/month = DN·0.1.
- `tas` colour map: the perceptually-uniform diverging "vik" (Fabio Crameri, via
  `cmcrameri`), with the fixed symmetric range −40..40 °C so vik's neutral midpoint
  lands exactly at 0 °C (cold blue → hot red). `pr` still uses anchor RGB stops.
  In `serve.py`, a variable specifies either `"cmap"` (named scientific colormap,
  sampled into the LUT and into ~33 client colour-bar stops) or `"stops"`.
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

- Atomic git commits with standard tags; push after each commit (note: no git
  remote configured yet, so pushes are local-only until one is added).
- Keep this file up to date as the project changes.
