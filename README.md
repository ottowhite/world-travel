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
- step through the 12 months with the **slider** (a labelled notch per month) or
  the **play** button,
- **drag** to pan, **scroll** to zoom, **hover** to read the value at a point —
  the map wraps around on both axes.
  The colour bar uses a **fixed absolute range** per variable, so it never
  changes on pan/zoom: temperature is a full **ROYGBIV** ramp over −40..40 °C
  (violet = coldest, red = hottest); precipitation is a **log** scale over
  1..400 mm/month (pale = dry, deep blue = wet; ≥400 mm saturates).
- toggle the **Coastlines** overlay (Natural Earth 1:50m, bundled in `assets/`),
  on by default. While it is on, ocean (anything outside the Natural Earth land
  polygons) is painted a flat **pale blue** and only land data is shown; turn it
  off to see the raw global field (including over the sea).

## How it works

- `scripts/download_data.py` streams the monthly GeoTIFFs from EnviCloud,
  skipping any already fully downloaded.
- `scripts/serve.py` is a small HTTP server. The page is a full-screen `<canvas>`;
  on every pan/zoom it asks `GET /render?var=&month=&west=&east=&south=&north=&w=&h=`
  for the visible bbox, and the server does a windowed, decimated read of that
  region straight from the GeoTIFFs (which carry internal overviews, so reads are
  fast at any zoom), applies the CHELSA V2.1 scaling (`tas` → °C, `pr` → mm/month)
  and a colour map, and returns raw RGBA bytes. Wrap-around is handled by tiling
  the read across world copies; values are normalised against a fixed per-variable
  colour range (baked into the page config), so the same colour always means the
  same physical value regardless of zoom.
- A bundled Natural Earth 1:50m coastline GeoJSON (`assets/ne_50m_coastline.geojson`,
  served at `GET /coastline.geojson`) is fetched once, flattened to lon/lat
  polylines, and drawn over the raster — with a dark halo plus a light line so it
  stays legible over both colour maps — replicated across the same wrap copies.
