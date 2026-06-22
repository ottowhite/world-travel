# CHELSA climatology viewer

Download and explore the [CHELSA](https://chelsa-climate.org/) V2.1 monthly
climatologies (1981–2010) of **temperature** and **precipitation** in a
full-screen, slippy-map-style viewer: pan, zoom, and step through the months.
Detail is loaded dynamically — each pan/zoom reads a crop straight from the
source GeoTIFFs at the resolution the current zoom needs.

Data source: EnviDat — *CHELSA Climatologies*
([DOI 10.16904/envidat.228](https://doi.org/10.16904/envidat.228), CC0), hosted
on EnviCloud as 30 arc-sec (~1 km) global GeoTIFFs.

## Setup

This project uses [uv](https://docs.astral.sh/uv/) for Python, wrapped in a Nix
dev shell loaded via [direnv](https://direnv.net/).

```sh
direnv allow      # loads shell.nix (provides uv + curl + nodejs)
uv sync           # installs rasterio, numpy, plotly
```

Without direnv/nix you can just use uv directly if it is on your PATH.

## Usage

```sh
make download     # ~6 GB: 12 months × {pr, tas} into data/ (gitignored)
make serve        # starts the viewer -> http://localhost:8765
```

(`make serve PORT=9000` to pick a port.) Then in the viewer:

- toggle between **Temperature** and **Precipitation**,
- step through the 12 months with the **slider** or the **play** button,
- **right-drag** to pan, **scroll** to zoom — the map wraps around on both
  axes and the colour bar rescales to the region in view.

## How it works

- `scripts/download_data.py` streams the monthly GeoTIFFs from EnviCloud,
  skipping any already fully downloaded.
- `scripts/serve.py` is a small HTTP server. The page is a full-screen `<canvas>`;
  on every pan/zoom it asks `GET /render?var=&month=&west=&east=&south=&north=&w=&h=`
  for the visible bbox, and the server does a windowed, decimated read of that
  region straight from the GeoTIFFs (which carry internal overviews, so reads are
  fast at any zoom), applies the CHELSA V2.1 scaling (`tas` → °C, `pr` → mm/month)
  and a colour map, and returns raw RGBA bytes. Wrap-around is handled by tiling
  the read across world copies; the colour range is the 2–98th percentile of the
  region currently in view.
