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

# 2. Build the interactive maps into output/
uv run scripts/visualise.py
```

Then open `output/tas_monthly_climatology.html` and
`output/pr_monthly_climatology.html` in a browser. Drag the slider (or press
play) to step through the months.

Handy flags:

```sh
uv run scripts/download_data.py --vars tas --months 6 7 8   # subset
uv run scripts/visualise.py --width 480                     # coarser/smaller HTML
```

## How it works

- `scripts/download_data.py` streams the monthly GeoTIFFs from EnviCloud,
  skipping any already fully downloaded.
- `scripts/visualise.py` does a decimated *average* read of each month down to a
  small global grid (default ~0.5°), applies the CHELSA V2.1 scaling
  (`tas` → °C, `pr` → mm/month), and builds a `plotly.express.imshow` animation
  with a month slider.
