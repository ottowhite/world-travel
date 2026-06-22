# CLAUDE.md

## Project

Download CHELSA V2.1 monthly climatologies (1981–2010) of temperature (`tas`)
and precipitation (`pr`) from EnviDat/EnviCloud and visualise them as
interactive global Plotly maps with a per-month slider.

## Layout

- `scripts/download_data.py` — streams monthly GeoTIFFs into `data/` (gitignored).
- `scripts/visualise.py` — decimated-average read → CHELSA scaling → `plotly.express.imshow`
  animation; writes one HTML per variable into `output/` (gitignored).
- `shell.nix` / `.envrc` — Nix dev shell (uv + curl + nodejs_23), loaded by direnv (`use nix`).
- `.mcp.json` — Playwright MCP server, launched via `nix-shell --run "npx -y @playwright/mcp ..."`
  (needs the `nodejs_23` from `shell.nix`). Profile/cache live in `.cache/` (gitignored).
- `pyproject.toml` / `uv.lock` — deps: rasterio, numpy, plotly. Python 3.13 via uv.

## Data details (CHELSA V2.1)

- 30 arc-sec global GeoTIFFs, `uint16`, nodata `65535`, scale `0.1`.
- Coverage: lon −180..180, lat −90..84 (EPSG:4326).
- `tas`: °C = DN·0.1 − 273.15.  `pr`: mm/month = DN·0.1.
- URL pattern:
  `https://os.unil.cloud.switch.ch/chelsa02/chelsa/global/climatologies/{var}/1981-2010/CHELSA_{var}_{MM}_1981-2010_V.2.1.tif`

## Commands

```sh
uv run scripts/download_data.py     # ~6 GB
uv run scripts/visualise.py         # → output/*.html
```

## Conventions

- Atomic git commits with standard tags; push after each commit (note: no git
  remote configured yet, so pushes are local-only until one is added).
- Keep this file up to date as the project changes.
