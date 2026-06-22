"""Simple global map of CHELSA monthly climatologies with a per-month slider.

Reads the downloaded GeoTIFFs (see scripts/download_data.py), downsamples each
month with a decimated read, applies the CHELSA V2.1 scaling, and builds an
interactive Plotly map (plotly.express.imshow) with a month slider + play button.

One HTML file is written per variable into output/.

Usage:
    uv run scripts/visualise.py                  # pr + tas at default resolution
    uv run scripts/visualise.py --width 480      # coarser/smaller HTML
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import plotly.express as px
import rasterio
from rasterio.enums import Resampling

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "output"

PERIOD = "1981-2010"
VERSION = "V.2.1"
MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

# Per-variable display config. `convert` maps the raw uint16 DN (after the
# rasterio scale of 0.1 is applied) to physical units.
VARIABLES = {
    "tas": {
        "title": "Mean near-surface air temperature",
        "unit": "°C",
        # CHELSA V2.1 tas: degrees Kelvin = DN * 0.1; subtract 273.15 for °C.
        "convert": lambda kelvin: kelvin - 273.15,
        "cmap": "RdBu_r",
    },
    "pr": {
        "title": "Mean monthly precipitation",
        "unit": "mm / month",
        # CHELSA V2.1 pr: kg m-2 month-1 (≈ mm/month) = DN * 0.1.
        "convert": lambda mm: mm,
        "cmap": "dense",
    },
}


def tif_path(var: str, month: int) -> Path:
    return DATA_DIR / f"CHELSA_{var}_{month:02d}_{PERIOD}_{VERSION}.tif"


def read_cube(var: str, height: int, width: int):
    """Return (cube[12,H,W] in physical units, lons[W], lats[H])."""
    convert = VARIABLES[var]["convert"]
    frames = []
    lons = lats = None
    for month in range(1, 13):
        path = tif_path(var, month)
        if not path.exists():
            raise FileNotFoundError(
                f"{path} missing — run: uv run scripts/download_data.py"
            )
        with rasterio.open(path) as ds:
            # Decimated read averages the native ~1 km grid down to (H, W).
            raw = ds.read(
                1,
                out_shape=(height, width),
                resampling=Resampling.average,
                masked=True,
            ).astype("float32")
            # ds.scales (0.1) is not applied by read(); apply it explicitly.
            scaled = raw * ds.scales[0]
            data = convert(scaled)
            frames.append(np.ma.filled(data, np.nan))
            if lons is None:
                b = ds.bounds
                lons = np.linspace(b.left, b.right, width)
                lats = np.linspace(b.top, b.bottom, height)
    return np.stack(frames), lons, lats


def build_figure(var: str, height: int, width: int):
    cfg = VARIABLES[var]
    cube, lons, lats = read_cube(var, height, width)

    # Shared colour range across all months (robust percentiles ignore outliers).
    finite = cube[np.isfinite(cube)]
    zmin, zmax = np.percentile(finite, [2, 98])

    fig = px.imshow(
        cube,
        animation_frame=0,
        x=lons,
        y=lats,
        origin="upper",
        color_continuous_scale=cfg["cmap"],
        zmin=zmin,
        zmax=zmax,
        aspect="equal",
        labels={"x": "Longitude", "y": "Latitude", "color": cfg["unit"]},
    )

    # Label the slider/frames with month names instead of 0-11.
    for step, name in zip(fig.layout.sliders[0].steps, MONTH_NAMES):
        step.label = name
    for frame, name in zip(fig.frames, MONTH_NAMES):
        frame.name = name
    fig.layout.sliders[0].currentvalue.prefix = "Month: "
    fig.layout.sliders[0].active = 0

    fig.update_layout(
        title=f"{cfg['title']} — CHELSA {PERIOD} climatology",
        margin=dict(l=40, r=20, t=60, b=40),
        coloraxis_colorbar_title=cfg["unit"],
    )
    fig.update_xaxes(constrain="domain")
    fig.update_yaxes(constrain="domain")
    return fig


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vars", nargs="+", default=["tas", "pr"])
    parser.add_argument(
        "--width", type=int, default=720, help="output grid width (default 720 ≈ 0.5°)"
    )
    args = parser.parse_args()
    # Keep ~square pixels for CHELSA's -90..84 lat / -180..180 lon coverage.
    height = round(args.width * (84 + 90) / 360)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for var in args.vars:
        print(f"Building {var} map ({args.width}x{height}) ...")
        fig = build_figure(var, height, args.width)
        out = OUT_DIR / f"{var}_monthly_climatology.html"
        fig.write_html(out, include_plotlyjs="cdn")
        print(f"  → {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    print("\nOpen the HTML file(s) above in a browser.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
