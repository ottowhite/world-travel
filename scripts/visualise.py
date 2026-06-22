"""Interactive global map of CHELSA monthly climatologies.

Reads the downloaded GeoTIFFs (see scripts/download_data.py), downsamples each
month with a decimated-average read, applies the CHELSA V2.1 scaling, and emits
a single full-screen HTML page (Plotly.js + a little JS) plus per-month JSON
sidecar files. The page lets you:

  * switch between temperature (tas) and precipitation (pr),
  * step through the 12 months with a slider or play button,
  * pan/zoom — the colour bar auto-rescales to whatever region is in view.

Each month is fetched on demand, so the grid can be detailed without producing a
single enormous HTML file.

Output: output/climatology.html  +  output/frames/{var}_{m}.json  +  output/meta.json

Usage:
    uv run scripts/visualise.py                  # 0.25° (≈ width 1440)
    uv run scripts/visualise.py --width 720      # coarser/smaller
    uv run scripts/visualise.py --width 2880     # finer (≈ 0.125°, larger)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np
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

# Per-variable config. `convert` maps the raw uint16 DN (after rasterio's 0.1
# scale is applied) to physical units. `cmap`/`reverse` pick a Plotly.js named
# colour scale.
VARIABLES = {
    "tas": {
        "label": "Temperature",
        "title": "Mean near-surface air temperature",
        "unit": "°C",
        # CHELSA V2.1 tas: Kelvin = DN * 0.1; subtract 273.15 for °C.
        "convert": lambda kelvin: kelvin - 273.15,
        "cmap": "RdBu",
        "reverse": True,   # low = blue (cold), high = red (hot)
        "decimals": 1,
    },
    "pr": {
        "label": "Precipitation",
        "title": "Mean monthly precipitation",
        "unit": "mm / month",
        # CHELSA V2.1 pr: kg m-2 month-1 (≈ mm/month) = DN * 0.1.
        "convert": lambda mm: mm,
        "cmap": "YlGnBu",
        "reverse": True,   # low = pale (dry), high = deep blue (wet)
        "decimals": 0,
    },
}


def tif_path(var: str, month: int) -> Path:
    return DATA_DIR / f"CHELSA_{var}_{month:02d}_{PERIOD}_{VERSION}.tif"


def read_cube(var: str, height: int, width: int):
    """Return (cube[12,H,W] in physical units, lons[W], lats[H]).

    Rows run north→south (row 0 = lat ~84), matching the source GeoTIFF.
    """
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
            raw = ds.read(
                1,
                out_shape=(height, width),
                resampling=Resampling.average,
                masked=True,
            ).astype("float32")
            scaled = raw * ds.scales[0]  # read() does not apply the 0.1 scale
            frames.append(np.ma.filled(convert(scaled), np.nan))
            if lons is None:
                b = ds.bounds
                lons = np.linspace(b.left, b.right, width)
                lats = np.linspace(b.top, b.bottom, height)
    return np.stack(frames), lons, lats


def _frame_to_json(frame: np.ndarray, decimals: int):
    """A 2D frame -> nested lists, rounded, with NaN -> None (JSON null)."""
    rounded = np.round(frame, decimals)
    out = rounded.astype(object)
    out[~np.isfinite(rounded)] = None
    return out.tolist()


def build(width: int, height: int):
    frames_dir = OUT_DIR / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)

    meta = {"months": MONTH_NAMES, "order": list(VARIABLES), "vars": {}}
    for var, cfg in VARIABLES.items():
        print(f"  {var}: reading + downsampling 12 months ...")
        cube, lons, lats = read_cube(var, height, width)
        # Flip rows so row 0 = south (lat -90); paired with an ascending lat
        # axis this renders north-up (the earlier version came out flipped).
        cube = cube[:, ::-1, :]
        if "lons" not in meta:
            meta["lons"] = [round(float(v), 4) for v in lons]
            meta["lats"] = [round(float(v), 4) for v in lats[::-1]]

        finite = cube[np.isfinite(cube)]
        cmin, cmax = (float(v) for v in np.percentile(finite, [2, 98]))
        meta["vars"][var] = {
            "label": cfg["label"],
            "title": f"{cfg['title']} — CHELSA {PERIOD} climatology",
            "unit": cfg["unit"],
            "cmap": cfg["cmap"],
            "reverse": cfg["reverse"],
            "cmin": round(cmin, cfg["decimals"]),
            "cmax": round(cmax, cfg["decimals"]),
        }
        for m in range(12):
            fp = frames_dir / f"{var}_{m}.json"
            fp.write_text(json.dumps(_frame_to_json(cube[m], cfg["decimals"])))

    (OUT_DIR / "meta.json").write_text(json.dumps(meta))
    (OUT_DIR / "climatology.html").write_text(HTML, encoding="utf-8")


# ── Self-contained page; loads meta.json then fetches frames on demand. ──────
HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>CHELSA monthly climatology</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<style>
  html, body { margin: 0; height: 100%; font-family: system-ui, sans-serif; }
  #app { display: flex; flex-direction: column; height: 100vh; }
  #bar { display: flex; align-items: center; gap: 14px; padding: 8px 14px;
         background: #f5f5f7; border-bottom: 1px solid #ddd; flex: 0 0 auto; }
  #bar .group { display: flex; align-items: center; gap: 6px; }
  #bar button { padding: 5px 12px; border: 1px solid #bbb; border-radius: 6px;
                background: #fff; cursor: pointer; font-size: 14px; }
  #bar button.active { background: #2b6cb0; color: #fff; border-color: #2b6cb0; }
  #month { width: 320px; }
  #monthLabel { min-width: 34px; font-variant-numeric: tabular-nums; font-weight: 600; }
  #status { color: #888; font-size: 13px; }
  #map { flex: 1 1 auto; min-height: 0; }
  .sep { width: 1px; align-self: stretch; background: #ddd; }
</style>
</head>
<body>
<div id="app">
  <div id="bar">
    <div class="group" id="varButtons"><strong>Variable:</strong></div>
    <div class="sep"></div>
    <div class="group">
      <button id="play">▶ Play</button>
      <input id="month" type="range" min="0" max="11" step="1" value="0"/>
      <span id="monthLabel">Jan</span>
    </div>
    <span id="status"></span>
  </div>
  <div id="map"></div>
</div>
<script>
const gd = document.getElementById('map');
const status = document.getElementById('status');
let META, curVar, curMonth = 0, timer = null;
const cache = new Map();

async function getFrame(v, m) {
  const key = v + '_' + m;
  if (!cache.has(key)) {
    status.textContent = 'loading ' + META.months[m] + ' …';
    const r = await fetch('frames/' + key + '.json');
    cache.set(key, await r.json());
    status.textContent = '';
  }
  return cache.get(key);
}

function layout(v) {
  return {
    title: { text: v.title, x: 0.5, font: { size: 16 } },
    margin: { l: 48, r: 10, t: 40, b: 36 },
    xaxis: { title: { text: 'Longitude' }, range: [-180, 180], constrain: 'domain' },
    yaxis: { title: { text: 'Latitude' }, range: [-90, 84],
             scaleanchor: 'x', scaleratio: 1, constrain: 'domain' },
  };
}

async function init() {
  META = await (await fetch('meta.json')).json();
  curVar = META.order[0];
  const v = META.vars[curVar];
  const z = await getFrame(curVar, curMonth);
  const trace = {
    type: 'heatmap', x: META.lons, y: META.lats, z,
    colorscale: v.cmap, reversescale: v.reverse, zsmooth: 'best',
    colorbar: { title: { text: v.unit, side: 'right' } },
    hovertemplate: 'lon %{x:.1f}°, lat %{y:.1f}°<br>%{z} ' + v.unit + '<extra></extra>',
  };
  await Plotly.newPlot(gd, [trace], layout(v), { responsive: true, scrollZoom: true });
  rescaleColor();
  gd.on('plotly_relayout', rescaleColor);

  META.order.forEach(key => {
    const b = document.createElement('button');
    b.textContent = META.vars[key].label;
    b.dataset.var = key;
    if (key === curVar) b.classList.add('active');
    b.addEventListener('click', () => setVar(key));
    varBox.appendChild(b);
  });
}

// ── Colour bar follows the region currently in view ───────────────────────
function currentRanges() {
  const xa = gd._fullLayout.xaxis, ya = gd._fullLayout.yaxis;
  const xr = xa.autorange ? null : [Math.min(...xa.range), Math.max(...xa.range)];
  const yr = ya.autorange ? null : [Math.min(...ya.range), Math.max(...ya.range)];
  return [xr, yr];
}

function rescaleColor() {
  const v = META.vars[curVar];
  const z = cache.get(curVar + '_' + curMonth);
  if (!z) return;
  const [xr, yr] = currentRanges();
  if (!xr && !yr) {                              // whole world: robust 2–98 pct
    Plotly.restyle(gd, { zmin: [v.cmin], zmax: [v.cmax] });
    return;
  }
  let lo = Infinity, hi = -Infinity;             // zoomed: exact in-view range
  for (let j = 0; j < META.lats.length; j++) {
    if (yr && (META.lats[j] < yr[0] || META.lats[j] > yr[1])) continue;
    const row = z[j];
    for (let i = 0; i < META.lons.length; i++) {
      if (xr && (META.lons[i] < xr[0] || META.lons[i] > xr[1])) continue;
      const val = row[i];
      if (val === null) continue;
      if (val < lo) lo = val;
      if (val > hi) hi = val;
    }
  }
  if (lo > hi) { lo = v.cmin; hi = v.cmax; }   // nothing in view -> defaults
  if (lo === hi) hi = lo + 1;                  // avoid a zero-width scale
  Plotly.restyle(gd, { zmin: [lo], zmax: [hi] });
}

// ── Month controls ────────────────────────────────────────────────────────
const monthSlider = document.getElementById('month');
const monthLabel = document.getElementById('monthLabel');
async function setMonth(m) {
  curMonth = ((m % 12) + 12) % 12;
  monthSlider.value = curMonth;
  monthLabel.textContent = META.months[curMonth];
  const z = await getFrame(curVar, curMonth);
  await Plotly.restyle(gd, { z: [z] });
  rescaleColor();
}
monthSlider.addEventListener('input', e => setMonth(+e.target.value));

const playBtn = document.getElementById('play');
function stop() { if (timer) clearInterval(timer); timer = null; playBtn.textContent = '▶ Play'; }
playBtn.addEventListener('click', () => {
  if (timer) { stop(); return; }
  playBtn.textContent = '⏸ Pause';
  timer = setInterval(() => setMonth(curMonth + 1), 900);
});

// ── Variable controls ─────────────────────────────────────────────────────
const varBox = document.getElementById('varButtons');
async function setVar(key) {
  curVar = key;
  const v = META.vars[key];
  varBox.querySelectorAll('button').forEach(b =>
    b.classList.toggle('active', b.dataset.var === key));
  const z = await getFrame(curVar, curMonth);
  await Plotly.restyle(gd, {
    z: [z], colorscale: [v.cmap], reversescale: [v.reverse],
    'colorbar.title.text': [v.unit],
    hovertemplate: ['lon %{x:.1f}°, lat %{y:.1f}°<br>%{z} ' + v.unit + '<extra></extra>'],
  });
  await Plotly.relayout(gd, { 'title.text': v.title });
  rescaleColor();
}

init();
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--width", type=int, default=1440,
        help="output grid width in pixels (default 1440 ≈ 0.25°)",
    )
    args = parser.parse_args()
    # Keep ~square pixels for CHELSA's -90..84 lat / -180..180 lon coverage.
    height = round(args.width * (84 + 90) / 360)

    print(f"Building at {args.width}x{height} ...")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    build(args.width, height)

    total = sum(f.stat().st_size for f in (OUT_DIR / "frames").iterdir())
    print(f"  → {OUT_DIR/'climatology.html'}  (frames total {total/1e6:.0f} MB)")
    print("\nServe it (file:// is blocked by some browsers/MCP):")
    print(f"    python3 -m http.server -d {OUT_DIR} 8765")
    print("    open http://localhost:8765/climatology.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
