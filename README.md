# CHELSA climatology viewer

Download and visualise the [CHELSA](https://chelsa-climate.org/) V2.1 monthly
climatologies (1981–2010) of **temperature** and **precipitation**, and render
them as interactive global maps with a per-month slider.

Data source: EnviDat — *CHELSA Climatologies*
([DOI 10.16904/envidat.228](https://doi.org/10.16904/envidat.228), CC0), hosted
on EnviCloud as 30 arc-sec (~1 km) global GeoTIFFs.

## Setup

This project uses [uv](https://docs.astral.sh/uv/) for Python, wrapped in a Nix
dev shell loaded via [direnv](https://direnv.net/).

```sh
direnv allow      # loads shell.nix (provides uv + curl)
uv sync           # installs rasterio, numpy, plotly
```

Without direnv/nix you can just use uv directly if it is on your PATH.

## Usage

```sh
# 1. Download the data (~6 GB: 12 months × {pr, tas}) into data/ (gitignored)
uv run scripts/download_data.py

# 2. Build the interactive map into output/
uv run scripts/visualise.py

# 3. Serve it over HTTP (file:// is blocked by most browsers)
python3 -m http.server -d output 8765
open http://localhost:8765/climatology.html
```

The page is full-screen and lets you:

- toggle between **Temperature** and **Precipitation**,
- step through the 12 months with the **slider** or the **play** button,
- **pan/zoom** — the colour bar auto-rescales to the region in view.

Handy flags:

```sh
uv run scripts/download_data.py --vars tas --months 6 7 8   # subset
uv run scripts/visualise.py --width 720                     # coarser/smaller
uv run scripts/visualise.py --width 2880                    # finer (≈ 0.125°)
```

## How it works

- `scripts/download_data.py` streams the monthly GeoTIFFs from EnviCloud,
  skipping any already fully downloaded.
- `scripts/visualise.py` does a decimated *average* read of each month down to a
  global grid (default ~0.25°), applies the CHELSA V2.1 scaling (`tas` → °C,
  `pr` → mm/month), and writes `output/climatology.html` plus per-month JSON
  frames under `output/frames/`. The page (Plotly.js heatmap + a little JS)
  fetches each month on demand, so the grid can be detailed without one huge
  HTML file. The colour bar follows the zoomed region; un-zoomed it uses a
  robust 2–98th percentile range.
