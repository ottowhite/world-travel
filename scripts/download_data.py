"""Download CHELSA V2.1 monthly climatologies (precipitation + temperature).

Source: EnviDat "CHELSA Climatologies" dataset (DOI 10.16904/envidat.228),
hosted on EnviCloud (Switch object store). Files are 30 arc-sec (~1 km) global
GeoTIFFs of the 1981-2010 monthly climatology.

We fetch two variables:
  pr  - mean monthly precipitation amount   (~346 MB / month)
  tas - mean monthly near-surface air temp  (~149 MB / month)

12 months each => ~6 GB total. Files land in data/ (gitignored).

Usage:
    uv run scripts/download_data.py            # all months, pr + tas
    uv run scripts/download_data.py --vars tas # just temperature
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

BASE = "https://os.unil.cloud.switch.ch/chelsa02/chelsa/global/climatologies"
PERIOD = "1981-2010"
VERSION = "V.2.1"
MONTHS = range(1, 13)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vars",
        nargs="+",
        default=["pr", "tas"],
        help="CHELSA variables to fetch (default: pr tas)",
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
        for month in args.months:
            url, name = file_url(var, month)
            download(url, DATA_DIR / name)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
