"""Download CHELSA V2.1 monthly climatologies (precipitation + temperature).

Source: EnviDat "CHELSA Climatologies" dataset (DOI 10.16904/envidat.228),
hosted on EnviCloud (Switch object store). Files are 30 arc-sec (~1 km) global
GeoTIFFs of the 1981-2010 monthly climatology.

We fetch three variables:
  pr   - mean monthly precipitation amount   (~346 MB / month)
  tas  - mean monthly near-surface air temp  (~149 MB / month)
  orog - elevation / altitude (static DEM)    (one file, no months)

The two monthly variables are 12 files each (~6 GB total). `orog` is CHELSA's
static input DEM (`dem_latlong.nc`, ~3.6 GB float32) on the SAME 30 arc-sec grid;
we stream it once and transcode it locally to a compact tiled int16 GeoTIFF with
internal overviews (a few hundred MB) so the viewer reads it as fast as the
climate layers, then drop the source NetCDF. Files land in data/ (gitignored).

The whole script is idempotent: a HEAD request gives the remote size and any file
already present at full size is skipped, so re-running only fetches what is
missing (e.g. adding `orog` to an existing pr/tas download).

Usage:
    uv run scripts/download_data.py             # all months, pr + tas + orog
    uv run scripts/download_data.py --vars tas  # just temperature
    uv run scripts/download_data.py --vars orog # just the altitude layer
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

BASE = "https://os.unil.cloud.switch.ch/chelsa02/chelsa/global/climatologies"
BASE_INPUT = "https://os.unil.cloud.switch.ch/chelsa02/chelsa/global/input/static"
PERIOD = "1981-2010"
VERSION = "V.2.1"
MONTHS = range(1, 13)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Static (month-less) layers: a source NetCDF streamed once, then transcoded to a
# tiled int16 GeoTIFF named to match the viewer's tif_path() convention.
STATIC = {
    "orog": {
        "url": f"{BASE_INPUT}/dem_latlong.nc",
        "nc": "dem_latlong.nc",
        "tif": f"CHELSA_orog_{PERIOD}_{VERSION}.tif",
    },
}


def file_url(var: str, month: int) -> tuple[str, str]:
    name = f"CHELSA_{var}_{month:02d}_{PERIOD}_{VERSION}.tif"
    return f"{BASE}/{var}/{PERIOD}/{name}", name


def download(url: str, dest: Path) -> None:
    """Stream a URL to dest, skipping if already complete."""
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req) as resp:
        remote_size = int(resp.headers.get("Content-Length", 0))

    if dest.exists() and dest.stat().st_size == remote_size and remote_size > 0:
        print(f"  ✓ {dest.name} already present ({remote_size / 1e6:.0f} MB)")
        return

    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  ↓ {dest.name} ({remote_size / 1e6:.0f} MB)")
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as fh:
        downloaded = 0
        chunk = 1 << 20  # 1 MiB
        while True:
            buf = resp.read(chunk)
            if not buf:
                break
            fh.write(buf)
            downloaded += len(buf)
            if remote_size:
                pct = 100 * downloaded / remote_size
                print(f"\r    {pct:5.1f}%  {downloaded / 1e6:6.0f} MB", end="")
        print()
    tmp.rename(dest)


def _convert_dem(src_path: Path, dest_tif: Path) -> None:
    """Transcode the float32 DEM NetCDF into a tiled int16 GeoTIFF + overviews.

    Elevation is integer metres, so int16 (nodata -32768) halves the on-disk size
    versus float32; tiling + internal overviews give the viewer the same fast
    windowed/decimated reads it gets from the climate GeoTIFFs.
    """
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import Window

    nodata = -32768
    with rasterio.open(src_path) as src:
        src_nodata = src.nodata
        profile = {
            "driver": "GTiff", "dtype": "int16", "nodata": nodata,
            "width": src.width, "height": src.height, "count": 1,
            "crs": src.crs, "transform": src.transform,
            "tiled": True, "blockxsize": 512, "blockysize": 512,
            "compress": "deflate", "predictor": 2, "zlevel": 9,
            "BIGTIFF": "IF_SAFER",
        }
        tmp = dest_tif.with_suffix(".tif.part")
        step = 512
        with rasterio.open(tmp, "w", **profile) as dst:
            for row in range(0, src.height, step):
                hh = min(step, src.height - row)
                win = Window(0, row, src.width, hh)
                a = src.read(1, window=win)
                valid = np.isfinite(a)
                if src_nodata is not None:
                    valid &= a != src_nodata
                out = np.where(valid, np.rint(a), nodata).astype("int16")
                dst.write(out, 1, window=win)
                pct = 100 * (row + hh) / src.height
                print(f"\r    transcoding {pct:5.1f}%", end="")
        print()
    # Overviews must be added on the closed dataset, reopened read/write.
    with rasterio.open(tmp, "r+") as dst:
        dst.build_overviews([2, 4, 8, 16, 32, 64], Resampling.average)
    tmp.replace(dest_tif)


def download_static(var: str) -> None:
    """Fetch + transcode a static layer; skip entirely if the GeoTIFF exists."""
    cfg = STATIC[var]
    dest_tif = DATA_DIR / cfg["tif"]
    if dest_tif.exists() and dest_tif.stat().st_size > 0:
        print(f"  ✓ {dest_tif.name} already present")
        return
    nc = DATA_DIR / cfg["nc"]
    download(cfg["url"], nc)
    print(f"  ⚙ transcoding {nc.name} → {dest_tif.name} (int16 + overviews)")
    _convert_dem(nc, dest_tif)
    nc.unlink(missing_ok=True)   # the GeoTIFF is all the viewer needs
    print(f"  ✓ {dest_tif.name} ready")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vars",
        nargs="+",
        default=["pr", "tas", "orog"],
        help="CHELSA variables to fetch (default: pr tas orog)",
    )
    parser.add_argument(
        "--months",
        nargs="+",
        type=int,
        default=list(MONTHS),
        help="Months to fetch as integers 1-12 (default: all)",
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading into {DATA_DIR}")

    for var in args.vars:
        print(f"\n{var}:")
        if var in STATIC:
            download_static(var)
            continue
        for month in args.months:
            url, name = file_url(var, month)
            download(url, DATA_DIR / name)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
